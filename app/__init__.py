"""IMAX Alert Flask application factory."""
import logging
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)
csrf = CSRFProtect()
logger = logging.getLogger(__name__)


def create_app(config_name="default"):
    """
    Create and configure the Flask application.

    Initialises extensions, runs DB migrations, seeds required data, registers
    blueprints, and returns the ready-to-use app instance.  Accepts a
    *config_name* string matching a key in ``config.config``
    (``'development'``, ``'production'``, ``'testing'``).
    """
    from config import config

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config[config_name])

    # Trust one layer of proxy headers (Cloudflare / NPM) so rate-limiting and
    # IP logging use the real client IP, not the proxy address.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    limiter.init_app(app)
    csrf.init_app(app)

    # Flask-Login
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()
        if not app.config.get("SKIP_MIGRATIONS", False):
            _run_migrations()
        _enable_wal_mode(app)
        _seed_roles_and_admin()
        _seed_lookup_tables()
        _seed_default_settings()
        # Always run to fill in any fields (e.g. website URLs) that were
        # blank in the CSV at install time but have since been added.
        if not app.config.get("SKIP_CSV_SEED", False):
            _upsert_theaters_from_csv(app)
        _load_settings_into_config(app)
        _migrate_legacy_alert_movies()

    from app.routes import main_bp, api_bp
    from app.auth import auth_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    def _in_user_tz(dt, tz_name="UTC", fmt="%b %d, %Y %I:%M %p"):
        if dt is None:
            return "–"
        try:
            tz = ZoneInfo(tz_name or "UTC")
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("UTC")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(tz).strftime(fmt)

    app.jinja_env.filters["in_user_tz"] = _in_user_tz

    # Exempt the JSON API blueprint from CSRF — fetch() calls use JSON bodies
    # which browsers cannot send cross-origin without CORS pre-flight, so the
    # risk CSRF tokens protect against doesn't apply to these endpoints.
    csrf.exempt(api_bp)

    # Inject a cache-busting fingerprint into every template context.
    # Uses style.css mtime so any CSS change immediately invalidates the
    # browser cache without a manual version bump.
    import os as _os
    _css_path = _os.path.join(app.static_folder, "css", "style.css")

    @app.context_processor
    def _static_fingerprint():
        try:
            v = int(_os.path.getmtime(_css_path))
        except OSError:
            v = 0
        return {"static_v": v}

    @app.context_processor
    def _session_timeout_ctx():
        minutes = app.config.get("SESSION_TIMEOUT_MINUTES")
        if minutes is None:
            try:
                from app.models import Settings
                row = Settings.query.filter_by(key="session_timeout_minutes").first()
                minutes = int(row.value) if row and row.value else 60
            except Exception:  # noqa: BLE001
                minutes = 60
            app.config["SESSION_TIMEOUT_MINUTES"] = minutes
        return {"session_timeout_minutes": minutes}

    from app.cli import register_cli
    register_cli(app)

    return app


@login_manager.user_loader
def load_user(user_id):
    """Return the User for *user_id*, used by Flask-Login to reload the session."""
    from app.models import User
    return User.query.get(int(user_id))


def _enable_wal_mode(app):
    """Enable SQLite WAL journal mode for better concurrency."""
    db_url = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if "sqlite" not in db_url:
        return
    try:
        with db.engine.connect() as conn:
            conn.execute(db.text("PRAGMA journal_mode=WAL"))
            conn.execute(db.text("PRAGMA busy_timeout=5000"))
        logger.info("SQLite WAL mode enabled.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not enable WAL mode: %s", exc)


def _run_migrations():
    """
    Apply incremental schema changes idempotently on every startup.
    db.create_all() only creates missing *tables*; it never alters existing ones.
    """
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(db.engine)

    # List of (col_name, table_name, alter_sql, optional_backfill_sql)
    migrations = [
        # Original migrations
        (
            "country", "theaters",
            "ALTER TABLE theaters ADD COLUMN country VARCHAR(100)",
            "UPDATE theaters SET country = 'United States' WHERE country IS NULL",
        ),
        (
            "screen_dims", "theaters",
            "ALTER TABLE theaters ADD COLUMN screen_dims VARCHAR(200)",
            None,
        ),
        # Phase 1: Theater FK columns
        ("chain_id", "theaters", "ALTER TABLE theaters ADD COLUMN chain_id INTEGER", None),
        ("country_id", "theaters", "ALTER TABLE theaters ADD COLUMN country_id INTEGER", None),
        ("region_id", "theaters", "ALTER TABLE theaters ADD COLUMN region_id INTEGER", None),
        ("city_id", "theaters", "ALTER TABLE theaters ADD COLUMN city_id INTEGER", None),
        (
            "aspect_ratio_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN aspect_ratio_id INTEGER", None,
        ),
        (
            "projector_type_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN projector_type_id INTEGER", None,
        ),
        (
            "audio_system_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN audio_system_id INTEGER", None,
        ),
        ("screen_width_m", "theaters", "ALTER TABLE theaters ADD COLUMN screen_width_m REAL", None),
        ("screen_height_m", "theaters", "ALTER TABLE theaters ADD COLUMN screen_height_m REAL", None),
        # Phase 1: User new columns
        (
            "password_hash", "users",
            "ALTER TABLE users ADD COLUMN password_hash VARCHAR(256)", None,
        ),
        ("role_id", "users", "ALTER TABLE users ADD COLUMN role_id INTEGER", None),
        (
            "is_active", "users",
            "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1", None,
        ),
        (
            "measurement_unit", "users",
            "ALTER TABLE users ADD COLUMN measurement_unit VARCHAR(10) DEFAULT 'metric'",
            None,
        ),
        (
            "location_address", "users",
            "ALTER TABLE users ADD COLUMN location_address VARCHAR(500)", None,
        ),
        # Phase 8: Movie new columns
        ("poster_url", "movies", "ALTER TABLE movies ADD COLUMN poster_url VARCHAR(500)", None),
        ("tmdb_id", "movies", "ALTER TABLE movies ADD COLUMN tmdb_id INTEGER", None),
        # Phase CSV: new Theater columns
        (
            "continent_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN continent_id INTEGER", None,
        ),
        (
            "digital_projector_ar_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN digital_projector_ar_id INTEGER", None,
        ),
        (
            "film_projector_type_id", "theaters",
            "ALTER TABLE theaters ADD COLUMN film_projector_type_id INTEGER", None,
        ),
        (
            "film_projector_type", "theaters",
            "ALTER TABLE theaters ADD COLUMN film_projector_type VARCHAR(100)", None,
        ),
        (
            "commercial_films", "theaters",
            "ALTER TABLE theaters ADD COLUMN commercial_films VARCHAR(20)", None,
        ),
        # Alert notification cap
        (
            "max_notifications", "alert_preferences",
            "ALTER TABLE alert_preferences ADD COLUMN max_notifications INTEGER", None,
        ),
        (
            "notifications_fired", "alert_preferences",
            "ALTER TABLE alert_preferences ADD COLUMN notifications_fired INTEGER DEFAULT 0",
            None,
        ),
        # Notification batch tracking
        (
            "notified_showtime_ids", "notifications",
            "ALTER TABLE notifications ADD COLUMN notified_showtime_ids TEXT", None,
        ),
        # Force password change on first login
        (
            "force_password_change", "users",
            "ALTER TABLE users ADD COLUMN force_password_change INTEGER DEFAULT 0", None,
        ),
        # Stable upsert key for CSV sync
        # SQLite cannot add a UNIQUE column via ALTER TABLE; add plain column
        # and create the unique index separately as the backfill step.
        (
            "venue_key", "theaters",
            "ALTER TABLE theaters ADD COLUMN venue_key VARCHAR(100)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_theaters_venue_key ON theaters(venue_key)",
        ),
        # User timezone preference (IANA string)
        (
            "timezone", "users",
            "ALTER TABLE users ADD COLUMN timezone VARCHAR(100) DEFAULT 'UTC'",
            None,
        ),
        # Target date filter for alerts (fires only on a specific date when set)
        (
            "target_date", "alert_preferences",
            "ALTER TABLE alert_preferences ADD COLUMN target_date DATE",
            None,
        ),
        # Buffer (days) around target_date — fires within [target_date ± buffer]
        (
            "target_date_buffer", "alert_preferences",
            "ALTER TABLE alert_preferences ADD COLUMN target_date_buffer INTEGER",
            None,
        ),
        # Password reset token (hashed) and expiry — for self-service forgot-password flow
        (
            "reset_token", "users",
            "ALTER TABLE users ADD COLUMN reset_token VARCHAR(256)",
            None,
        ),
        (
            "reset_token_expiry", "users",
            "ALTER TABLE users ADD COLUMN reset_token_expiry DATETIME",
            None,
        ),
        # MFA / TOTP support
        (
            "mfa_secret", "users",
            "ALTER TABLE users ADD COLUMN mfa_secret VARCHAR(64)",
            None,
        ),
        (
            "mfa_enabled", "users",
            "ALTER TABLE users ADD COLUMN mfa_enabled INTEGER DEFAULT 0",
            None,
        ),
        (
            "mfa_recovery_codes", "users",
            "ALTER TABLE users ADD COLUMN mfa_recovery_codes TEXT",
            None,
        ),
        (
            "radius_km", "alert_preferences",
            "ALTER TABLE alert_preferences ADD COLUMN radius_km REAL",
            None,
        ),
        (
            "last_login_at", "users",
            "ALTER TABLE users ADD COLUMN last_login_at DATETIME",
            None,
        ),
        (
            "description", "theaters",
            "ALTER TABLE theaters ADD COLUMN description TEXT",
            None,
        ),
        (
            "amenities", "theaters",
            "ALTER TABLE theaters ADD COLUMN amenities TEXT",
            None,
        ),
        (
            "seating_capacity", "theaters",
            "ALTER TABLE theaters ADD COLUMN seating_capacity INTEGER",
            None,
        ),
        # On-demand showtime fetch
        (
            "on_demand", "showtimes",
            "ALTER TABLE showtimes ADD COLUMN on_demand BOOLEAN DEFAULT 0",
            "UPDATE showtimes SET on_demand = 0 WHERE on_demand IS NULL",
        ),
        (
            "on_demand_fetched_at", "theaters",
            "ALTER TABLE theaters ADD COLUMN on_demand_fetched_at DATETIME",
            None,
        ),
        (
            "last_scraped_at", "theaters",
            "ALTER TABLE theaters ADD COLUMN last_scraped_at DATETIME",
            None,
        ),
        # Browse schedule preferred run time (stored in user's configured timezone)
        (
            "preferred_hour", "browse_schedules",
            "ALTER TABLE browse_schedules ADD COLUMN preferred_hour INTEGER DEFAULT 8",
            None,
        ),
        # Browse schedule preferred day of week for Weekly frequency (0=Mon … 6=Sun)
        (
            "preferred_day_of_week", "browse_schedules",
            "ALTER TABLE browse_schedules ADD COLUMN preferred_day_of_week INTEGER",
            None,
        ),
        # Browse-schedule showtimes: visible on theater/movie pages but not the Dashboard.
        (
            "browse_only", "showtimes",
            "ALTER TABLE showtimes ADD COLUMN browse_only BOOLEAN NOT NULL DEFAULT 0",
            "UPDATE showtimes SET browse_only = 0 WHERE browse_only IS NULL",
        ),
    ]

    for col_name, table_name, alter_sql, backfill_sql in migrations:
        try:
            existing_cols = [c["name"] for c in inspector.get_columns(table_name)]
            if col_name not in existing_cols:
                with db.engine.connect() as conn:
                    conn.execute(db.text(alter_sql))
                    if backfill_sql:
                        conn.execute(db.text(backfill_sql))
                    conn.commit()
                logger.info("Migration applied: added column '%s' to '%s'.", col_name, table_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Schema migration error for %s.%s (non-fatal): %s", table_name, col_name, exc)

    # Idempotent data normalisations — safe to run on every startup.
    _data_cleanups = [
        # Collapse "IMAX 2D", "IMAX 4K", "IMAX with Laser" etc. → "IMAX".
        # Only "IMAX 3D" keeps its suffix (the 3D format is meaningful to users).
        (
            "UPDATE showtimes SET format_type = 'IMAX' "
            "WHERE format_type LIKE 'IMAX %' AND format_type NOT LIKE '%3D%'"
        ),
        # Strip trailing " Showtimes" from AMC and similar scrapers.
        # e.g. "RealD 3D Showtimes" → "RealD 3D", "Fan Faves Showtimes" → "Fan Faves"
        (
            "UPDATE showtimes "
            "SET format_type = TRIM(SUBSTR(format_type, 1, LENGTH(format_type) - LENGTH(' Showtimes'))) "
            "WHERE format_type LIKE '% Showtimes'"
        ),
        # Strip trailing " Format" (Cineplex) e.g. "Standard Format" → "Standard"
        (
            "UPDATE showtimes "
            "SET format_type = TRIM(SUBSTR(format_type, 1, LENGTH(format_type) - LENGTH(' Format'))) "
            "WHERE format_type LIKE '% Format'"
        ),
        # Programming categories that are not screen technologies → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type IN ("
            "  'Fan Faves', 'AMC Artisan Films', 'Thrills & Chills',"
            "  'Early Access', 'Stars & Strollers', 'Party Space', 'CC'"
            ")"
        ),
        # Language variants (subtitles / dubbed / spoken markers) → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type LIKE '%Subtitles%' "
            "   OR format_type LIKE '%Dubbed%' "
            "   OR format_type LIKE '%Spoken%'"
        ),
        # Accessibility options → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type LIKE '%Open Caption%' "
            "   OR format_type LIKE '%Audio Descri%' "
            "   OR format_type LIKE '%Closed Caption%'"
        ),
        # VIP age-restricted (not a screen technology) → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type LIKE 'VIP %' OR format_type LIKE 'VIP%+'"
        ),
        # Party rentals / baby screenings → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type LIKE 'Party Space%'"
        ),
        # "2D" / "Regular" (Cinemark) → Standard
        (
            "UPDATE showtimes SET format_type = 'Standard' "
            "WHERE format_type IN ('2D', 'Regular', 'Standard')"
        ),
    ]
    for cleanup_sql in _data_cleanups:
        try:
            with db.engine.connect() as conn:
                conn.execute(db.text(cleanup_sql))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Data cleanup error (non-fatal): %s", exc)


def _seed_roles_and_admin():
    """Ensure the three default roles exist and there is at least one admin user."""
    from werkzeug.security import generate_password_hash

    from app.models import Role, User

    # Seed roles
    role_defs = [
        ("admin", "Full access to all features and settings"),
        ("editor", "Can add/edit theater and lookup data; cannot manage users"),
        ("user", "Read-only access; can manage own alerts and notification preferences"),
    ]
    for role_name, role_desc in role_defs:
        if not Role.query.filter_by(name=role_name).first():
            db.session.add(Role(name=role_name, description=role_desc))
    db.session.flush()

    # Seed default admin account if no users exist
    if User.query.count() == 0:
        admin_role = Role.query.filter_by(name="admin").first()
        admin = User(
            name="Admin",
            email="admin",
            is_active=True,
            role_id=admin_role.id if admin_role else None,
            notify_email=False,
            notify_sms=False,
            measurement_unit="metric",
            force_password_change=True,
        )
        admin.set_password("admin")
        db.session.add(admin)
        db.session.flush()
        logger.info("Default admin user created (email='admin', password='admin').")

    db.session.commit()


def _seed_lookup_tables():
    """
    Populate FK lookup tables from existing Theater string columns.
    Runs once per startup; safe to re-run (idempotent via get-or-create).
    Skips if all theaters already have chain_id set.
    """
    from app.lookup_helpers import (
        get_or_create_aspect_ratio,
        get_or_create_audio_system,
        get_or_create_chain,
        get_or_create_city,
        get_or_create_country,
        get_or_create_projector_type,
        get_or_create_region,
        parse_screen_dims,
    )
    from app.models import Theater

    # Check if migration has already run for this batch
    total = Theater.query.count()
    if total == 0:
        return
    already_migrated = Theater.query.filter(Theater.aspect_ratio_id.isnot(None)).count()
    if already_migrated == total:
        logger.debug("Lookup table seed: all theaters already have FK columns set.")
        return

    logger.info("Seeding lookup tables from existing theater data (%d theaters)…", total)
    theaters = Theater.query.all()
    updated = 0
    from sqlalchemy.exc import IntegrityError
    for t in theaters:
        changed = False

        # Chain
        if t.chain_id is None and t.chain:
            chain_obj = get_or_create_chain(t.chain)
            if chain_obj:
                t.chain_id = chain_obj.id
                changed = True

        # Country
        country_obj = None
        if t.country_id is None and t.country:
            country_obj = get_or_create_country(t.country)
            if country_obj:
                t.country_id = country_obj.id
                changed = True
        elif t.country_id:
            from app.models import Country
            country_obj = Country.query.get(t.country_id)

        # Region
        region_obj = None
        if t.region_id is None and t.state:
            region_obj = get_or_create_region(t.state, country_obj)
            if region_obj:
                t.region_id = region_obj.id
                changed = True
        elif t.region_id:
            from app.models import Region
            region_obj = Region.query.get(t.region_id)

        # City
        if t.city_id is None and t.city and country_obj:
            city_obj = get_or_create_city(t.city, country_obj, region_obj)
            if city_obj:
                t.city_id = city_obj.id
                changed = True

        # Aspect Ratio
        if t.aspect_ratio_id is None and t.screen_size:
            ar_obj = get_or_create_aspect_ratio(t.screen_size)
            if ar_obj:
                t.aspect_ratio_id = ar_obj.id
                changed = True

        # Projector Type
        if t.projector_type_id is None and t.projector_type:
            pt_obj = get_or_create_projector_type(t.projector_type)
            if pt_obj:
                t.projector_type_id = pt_obj.id
                changed = True

        # Audio System
        if t.audio_system_id is None and t.audio_system:
            as_obj = get_or_create_audio_system(t.audio_system)
            if as_obj:
                t.audio_system_id = as_obj.id
                changed = True

        # Screen dimensions — parse from legacy screen_dims string
        if t.screen_width_m is None and t.screen_dims:
            w, h = parse_screen_dims(t.screen_dims)
            if w is not None:
                t.screen_width_m = w
                t.screen_height_m = h
                changed = True

        if changed:
            updated += 1
            # Commit every 50 theaters so the identity map stays consistent
            # and SQLite can resolve duplicate-check queries correctly.
            try:
                if updated % 50 == 0:
                    db.session.commit()
            except IntegrityError:
                db.session.rollback()
                logger.warning("IntegrityError during batch commit at theater %d — skipping batch.", t.id)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.warning("IntegrityError on final seed commit — some FK columns may not have been set.")
    logger.info("Lookup table seed complete: %d theaters updated.", updated)


def _seed_default_settings():
    """Ensure default Settings rows exist."""
    from app.models import Settings

    defaults = [
        ("tmdb_api_key", ""),
        ("app_measurement_unit", "metric"),
        # Email (SMTP)
        ("mail_server", "smtp.gmail.com"),
        ("mail_port", "587"),
        ("mail_use_tls", "true"),
        ("mail_username", ""),
        ("mail_password", ""),
        ("mail_from", ""),
        # SMS (Twilio)
        ("twilio_account_sid", ""),
        ("twilio_auth_token", ""),
        ("twilio_from_number", ""),
        # Maintenance
        ("cleanup_interval_hours", "24"),
        ("log_retention_days", "30"),
        # Alert processor
        ("alert_interval_minutes", "15"),
        # Session security
        ("session_timeout_minutes", "60"),
        # On-demand showtime fetch cooldown (hours)
        ("on_demand_fetch_cooldown_hours", "24"),
        # Scraper coordinator: minimum minutes between scrapes of the same theater
        ("scrape_cooldown_minutes", "30"),
        # Scraper coordinator: max simultaneous Playwright-based scrapers
        ("playwright_concurrency", "2"),
        # Scraper coordinator: max simultaneous plain-HTTP scrapers
        ("http_concurrency", "5"),
        # Browse schedule: how often the runner job checks for due schedules (minutes)
        ("browse_schedule_check_minutes", "30"),
    ]
    for key, default_value in defaults:
        if not Settings.query.filter_by(key=key).first():
            db.session.add(Settings(key=key, value=default_value))
    db.session.commit()


def _load_settings_into_config(app):
    """
    Copy notification credentials from the Settings table into app.config so
    notifications.py can read them via app.config without knowing about the DB.
    Called once at startup; the admin_settings POST route also calls this after saving.
    """
    from app.models import Settings

    mapping = {
        "mail_server": "MAIL_SERVER",
        "mail_port": "MAIL_PORT",
        "mail_use_tls": "MAIL_USE_TLS",
        "mail_username": "MAIL_USERNAME",
        "mail_password": "MAIL_PASSWORD",
        "mail_from": "MAIL_FROM",
        "twilio_account_sid": "TWILIO_ACCOUNT_SID",
        "twilio_auth_token": "TWILIO_AUTH_TOKEN",
        "twilio_from_number": "TWILIO_FROM_NUMBER",
    }
    rows = {s.key: s.value for s in Settings.query.all()}
    for db_key, cfg_key in mapping.items():
        val = rows.get(db_key, "")
        if db_key == "mail_port":
            try:
                app.config[cfg_key] = int(val) if val else 587
            except ValueError:
                app.config[cfg_key] = 587
        elif db_key == "mail_use_tls":
            app.config[cfg_key] = str(val).lower() in ("true", "1", "yes")
        else:
            app.config[cfg_key] = val or ""
    logger.debug("Notification settings loaded into app.config.")


def _migrate_legacy_alert_movies():
    """
    Back-fill AlertMovie rows for pre-existing AlertPreference rows that still
    have a movie_id set in the old single-movie column.

    For each such row:
      - Create an AlertMovie(alert_id, movie_id, alert_sent, alert_sent_at) if
        one doesn't already exist.
      - Clear AlertPreference.movie_id (set to None) so the row conforms to the
        new schema going forward.

    Safe to re-run: the AlertMovie unique constraint prevents duplicates.
    """
    from app.models import AlertMovie, AlertPreference

    prefs = AlertPreference.query.filter(AlertPreference.movie_id.isnot(None)).all()
    if not prefs:
        return

    migrated = 0
    for pref in prefs:
        existing = AlertMovie.query.filter_by(
            alert_id=pref.id, movie_id=pref.movie_id
        ).first()
        if not existing:
            am = AlertMovie(
                alert_id=pref.id,
                movie_id=pref.movie_id,
                alert_sent=pref.alert_sent,
                alert_sent_at=pref.alert_sent_at,
            )
            db.session.add(am)
            migrated += 1
        pref.movie_id = None  # clear legacy column

    db.session.commit()
    if migrated:
        logger.info("Migrated %d legacy AlertPreference.movie_id → AlertMovie rows.", migrated)


def _upsert_theaters_from_csv(app):
    """
    Upsert theaters from seeds/imax_theaters.csv.

    Match priority per row:
      1. venue_key  — if the CSV row has a non-empty Venue Key and a Theater with
                      that key exists, update that row.
      2. name       — case-insensitive fallback for rows without a key (legacy).
      3. no match   — insert a new Theater row.

    Fields updated when non-empty in the CSV: venue_key, chain/chain_id,
    website, audio_system/audio_system_id, address, zip_code (only when DB
    row has none), phone, and all screen/projector/dimension fields.

    Fields never overwritten: is_active, latitude, longitude,
    phone (preserved if CSV is blank), image_url.

    Returns a summary dict: {"inserted": N, "updated": N, "skipped": N, "errors": []}.
    """
    import csv
    import os
    import re

    from sqlalchemy import func

    from app.lookup_helpers import (
        get_or_create_aspect_ratio,
        get_or_create_audio_system,
        get_or_create_chain,
        get_or_create_city,
        get_or_create_continent,
        get_or_create_country,
        get_or_create_projector_type,
        get_or_create_region,
        parse_screen_dims,
    )
    from app.models import Theater

    csv_path = os.path.join(os.path.dirname(__file__), "..", "seeds", "imax_theaters.csv")
    csv_path = os.path.abspath(csv_path)
    if not os.path.exists(csv_path):
        logger.warning("CSV upsert skipped: file not found at %s.", csv_path)
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}

    def _normalise_ar(raw: str) -> str:
        """Fix '2.30:01' → '2.30:1'."""
        if not raw:
            return raw
        return re.sub(r":0+(\d)$", r":\1", raw.strip())

    logger.info("CSV theater upsert started: %s", csv_path)
    inserted = updated = skipped = 0
    errors = []
    processed = 0

    with app.app_context():
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    location_name = (row.get("Location Name") or "").strip()
                    if not location_name:
                        skipped += 1
                        continue

                    # --- Parse all CSV fields ---
                    continent_name  = (row.get("Region") or "").strip()
                    country_name    = (row.get("Country") or "").strip()
                    state_name      = (row.get("State/Province") or "").strip()
                    city_name       = (row.get("City") or "").strip()
                    screen_ar_raw   = _normalise_ar(row.get("Screen AR") or "")
                    digital_proj    = (row.get("Digital Projector") or "").strip()
                    digital_ar_raw  = _normalise_ar(
                        row.get(" max AR (Digital)") or row.get("max AR (Digital)") or ""
                    )
                    film_proj_raw   = (row.get("Film Projector") or "").strip()
                    screen_dims_str = (row.get("Screen Dimensions") or "").strip()
                    commercial      = (row.get("Commercial Films Shown") or "").strip() or None
                    venue_key       = (row.get("Venue Key") or "").strip() or None
                    chain_name      = (row.get("Chain") or "").strip() or None
                    website_url     = (row.get("Website") or "").strip() or None
                    audio_sys_name  = (row.get("Audio System") or "").strip() or None
                    address         = (row.get("Address") or "").strip() or None
                    postal_code     = (row.get("Postal Code") or "").strip() or None
                    phone           = (row.get("Phone") or "").strip() or None

                    # --- Resolve FK objects ---
                    continent_obj  = get_or_create_continent(continent_name) if continent_name else None
                    country_obj    = get_or_create_country(country_name) if country_name else None
                    region_obj     = (
                        get_or_create_region(state_name, country_obj)
                        if state_name and country_obj else None
                    )
                    city_obj       = (
                        get_or_create_city(city_name, country_obj, region_obj)
                        if city_name and country_obj else None
                    )
                    ar_obj         = get_or_create_aspect_ratio(screen_ar_raw) if screen_ar_raw else None
                    dig_proj_obj   = get_or_create_projector_type(digital_proj) if digital_proj else None
                    dig_ar_obj     = get_or_create_aspect_ratio(digital_ar_raw) if digital_ar_raw else None
                    film_pt_obj    = get_or_create_projector_type(film_proj_raw) if film_proj_raw else None
                    chain_root     = None
                    if website_url:
                        _p = urllib.parse.urlparse(website_url)
                        chain_root = f"{_p.scheme}://{_p.netloc}" if _p.netloc else None
                    chain_obj      = get_or_create_chain(chain_name, website=chain_root or "") if chain_name else None
                    audio_sys_obj  = get_or_create_audio_system(audio_sys_name) if audio_sys_name else None
                    w_m, h_m       = parse_screen_dims(screen_dims_str) if screen_dims_str else (None, None)

                    # --- Find existing theater ---
                    t = None
                    if venue_key:
                        t = Theater.query.filter_by(venue_key=venue_key).first()
                    if t is None:
                        # Exact match first so Unicode names (e.g. Turkish İ) that
                        # SQLite lower() can't fold still match correctly after the
                        # first upsert stores them verbatim.
                        t = Theater.query.filter_by(name=location_name).first()
                    if t is None:
                        t = Theater.query.filter(
                            func.lower(Theater.name) == location_name.lower()
                        ).first()

                    if t is None:
                        # Insert
                        t = Theater(
                            name=location_name,
                            is_active=True,
                            crawl_source="csv",
                        )
                        db.session.add(t)
                        inserted += 1
                    else:
                        updated += 1

                    # --- Apply CSV fields (always update non-empty values) ---
                    t.name = location_name
                    if venue_key:
                        t.venue_key = venue_key
                    t.country     = country_name or t.country
                    t.state       = state_name or t.state
                    t.city        = city_name or t.city
                    t.screen_size = screen_ar_raw or t.screen_size
                    t.projector_type = digital_proj or t.projector_type
                    t.screen_dims = screen_dims_str or t.screen_dims
                    if chain_name:
                        t.chain    = chain_name
                        t.chain_id = chain_obj.id if chain_obj else t.chain_id
                    if website_url and not t.website:
                        t.website = website_url
                    if audio_sys_name:
                        t.audio_system    = audio_sys_name
                        t.audio_system_id = audio_sys_obj.id if audio_sys_obj else t.audio_system_id
                    if address:
                        t.address = address
                    if postal_code and not t.zip_code:
                        t.zip_code = postal_code
                    if phone:
                        t.phone = phone
                    t.country_id            = country_obj.id if country_obj else t.country_id
                    t.region_id             = region_obj.id if region_obj else t.region_id
                    t.city_id               = city_obj.id if city_obj else t.city_id
                    t.aspect_ratio_id       = ar_obj.id if ar_obj else t.aspect_ratio_id
                    t.projector_type_id     = dig_proj_obj.id if dig_proj_obj else t.projector_type_id
                    t.continent_id          = continent_obj.id if continent_obj else t.continent_id
                    t.digital_projector_ar_id = dig_ar_obj.id if dig_ar_obj else t.digital_projector_ar_id
                    t.film_projector_type_id  = film_pt_obj.id if film_pt_obj else t.film_projector_type_id
                    t.film_projector_type   = film_proj_raw or t.film_projector_type
                    if commercial is not None:
                        t.commercial_films = commercial
                    if w_m is not None:
                        t.screen_width_m  = w_m
                    if h_m is not None:
                        t.screen_height_m = h_m
                    t.crawl_source = "csv"

                    processed += 1
                    if processed % 50 == 0:
                        db.session.flush()

                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Row '{row.get('Location Name')}': {exc}")
                    logger.warning("CSV upsert row error: %s", exc)

        try:
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            logger.error("CSV upsert final commit failed: %s", exc)
            return {"inserted": 0, "updated": 0, "skipped": skipped, "errors": [str(exc)]}

    logger.info(
        "CSV theater upsert complete: %d inserted, %d updated, %d skipped, %d errors.",
        inserted, updated, skipped, len(errors),
    )
    for e in errors:
        logger.warning("CSV upsert error: %s", e)
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}
