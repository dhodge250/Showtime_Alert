"""IMAX Alert Flask application factory."""
import logging
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

    secret_key = (app.config.get("SECRET_KEY") or "").strip()
    if config_name == "production" and (
        not secret_key or secret_key == "dev-secret-key-change-in-production"
    ):
        raise RuntimeError(
            "SECRET_KEY environment variable must be set to a non-empty, non-default value in production — "
            "refusing to start with an insecure key."
        )

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

    # Exempt the JSON API blueprint from CSRF. Handlers parse request bodies
    # with request.get_json(silent=True) (no force=True), so Flask only parses
    # bodies whose Content-Type is application/json — and browsers cannot send
    # that cross-origin without a CORS pre-flight. A cross-origin page sending
    # a non-JSON body (e.g. text/plain) gets an empty dict, not a parsed
    # payload, so the CSRF protection these tokens provide isn't needed here.
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
    Upsert theaters from seeds/imax_theaters.csv (startup seed).

    Thin wrapper around app.theater_csv._upsert_theater_row with
    preserve_existing=True: website/zip_code are only filled in when the
    existing row has none, and is_active is never modified on an existing row.

    Returns a summary dict: {"inserted": N, "updated": N, "skipped": N, "errors": []}.
    """
    import csv
    import os

    from app.theater_csv import _CSV_SEED_PATH, _upsert_theater_row

    csv_path = str(_CSV_SEED_PATH)
    if not os.path.exists(csv_path):
        logger.warning("CSV upsert skipped: file not found at %s.", csv_path)
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}

    logger.info("CSV theater upsert started: %s", csv_path)
    inserted = updated = skipped = 0
    errors: list = []
    warnings: list = []
    processed = 0

    with app.app_context():
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    status = _upsert_theater_row(
                        row, preserve_existing=True, source="csv",
                        errors=errors, warnings=warnings,
                    )
                    if status == "inserted":
                        inserted += 1
                    elif status == "updated":
                        updated += 1
                    else:
                        skipped += 1

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
