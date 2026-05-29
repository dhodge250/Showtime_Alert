"""Flask routes for IMAX Alert application."""
import logging
import threading
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app import db
from app.auth import require_role
from app.models import (
    AlertMovie,
    AlertPreference,
    AspectRatio,
    AudioSystem,
    Chain,
    City,
    Continent,
    Country,
    Movie,
    Notification,
    ProjectorType,
    Region,
    Role,
    Settings,
    Showtime,
    Theater,
    User,
)

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)
api_bp = Blueprint("api", __name__)

# Track whether a crawl is currently running (in-process flag)
_crawl_running = False
_crawl_last_summary: dict = {}


def _get_geocode_status() -> dict:
    """Thin wrapper so the view can call this without importing venue_crawler at module level."""
    try:
        from app.venue_crawler import get_geocode_status
        return get_geocode_status()
    except Exception:  # noqa: BLE001
        return {"running": False, "started_at": None, "finished_at": None,
                "total": 0, "processed": 0, "geocoded": 0, "failed": 0, "errors": []}


def _get_setting_int(key: str, default: int) -> int:
    """Read an integer setting from the Settings table, falling back to default."""
    try:
        s = Settings.query.filter_by(key=key).first()
        return int(s.value) if s and s.value else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Helper: current user's measurement unit (falls back to metric)
# ---------------------------------------------------------------------------

def _current_unit():
    """Return the current user's preferred measurement unit, defaulting to metric."""
    if current_user.is_authenticated:
        return current_user.measurement_unit or "metric"
    return "metric"


# ---------------------------------------------------------------------------
# UI Routes — general
# ---------------------------------------------------------------------------

@main_bp.route("/")
@login_required
def index():
    """Dashboard."""
    theaters = Theater.query.filter_by(is_active=True).all()
    movies = Movie.query.order_by(Movie.title).all()
    recent_showtimes = (
        Showtime.query.filter(Showtime.tickets_available.is_(True))
        .order_by(Showtime.first_seen.desc())
        .all()
    )
    # Admin sees all alerts; users see only their own
    if current_user.role_name == "admin":
        alerts = AlertPreference.query.filter_by(is_active=True).all()
        users = User.query.all()
    else:
        alerts = AlertPreference.query.filter_by(
            is_active=True, user_id=current_user.id
        ).all()
        users = [current_user]

    # Distinct movies being watched across all active alerts
    tracked_movie_ids = {
        am.movie_id
        for a in alerts
        for am in a.alert_movies.all()
    }

    return render_template(
        "index.html",
        theaters=theaters,
        movies=movies,
        recent_showtimes=recent_showtimes,
        alerts=alerts,
        users=users,
        unit=_current_unit(),
        tracked_movie_count=len(tracked_movie_ids),
    )


@main_bp.route("/theaters")
@login_required
def theaters():
    """Theater listing with map."""
    theaters_list = Theater.query.filter_by(is_active=True).order_by(
        Theater.country, Theater.state, Theater.city, Theater.name
    ).all()
    theaters_json = [t.to_dict() for t in theaters_list]
    aspect_ratios = AspectRatio.query.order_by(AspectRatio.label).all()
    projector_types = ProjectorType.query.order_by(ProjectorType.name).all()
    continents = Continent.query.order_by(Continent.name).all()
    return render_template(
        "theaters.html",
        theaters=theaters_list,
        theaters_json=theaters_json,
        aspect_ratios=aspect_ratios,
        projector_types=projector_types,
        continents=continents,
        unit=_current_unit(),
    )


@main_bp.route("/theaters/<int:theater_id>")
@login_required
def theater_detail(theater_id):
    """Theater detail page."""
    theater = Theater.query.get_or_404(theater_id)
    showtimes = (
        Showtime.query.filter_by(theater_id=theater_id)
        .filter(Showtime.show_datetime >= datetime.now(timezone.utc))
        .order_by(Showtime.show_datetime)
        .all()
    )
    return render_template(
        "theater_detail.html",
        theater=theater,
        showtimes=showtimes,
        unit=_current_unit(),
    )


@main_bp.route("/alerts")
@login_required
def alerts():
    """Alert management page."""
    theaters_list = Theater.query.filter_by(is_active=True).all()
    movies_list = Movie.query.order_by(Movie.title).all()

    if current_user.role_name == "admin":
        users_list = User.query.all()
        prefs = AlertPreference.query.order_by(AlertPreference.created_at.desc()).all()
    else:
        users_list = [current_user]
        prefs = AlertPreference.query.filter_by(
            user_id=current_user.id
        ).order_by(AlertPreference.created_at.desc()).all()

    return render_template(
        "alerts.html",
        theaters=theaters_list,
        theaters_json=[{"id": t.id, "name": t.name, "city": t.city or "", "state": t.state or ""} for t in theaters_list],
        movies=movies_list,
        users=users_list,
        preferences=prefs,
    )


@main_bp.route("/alerts/<int:pref_id>")
@login_required
def alert_detail(pref_id):
    """Alert detail page — notification history, matching showtimes, actions."""
    pref = AlertPreference.query.get_or_404(pref_id)
    if current_user.role_name != "admin" and pref.user_id != current_user.id:
        abort(403)

    # Matching showtimes — scope by AlertMovie movie IDs (or any-movie)
    q = Showtime.query
    if pref.theater_id:
        q = q.filter_by(theater_id=pref.theater_id)
    if not pref.is_any_movie:
        movie_ids = [am.movie_id for am in pref.alert_movies.all()]
        if movie_ids:
            q = q.filter(Showtime.movie_id.in_(movie_ids))
    showtimes = q.order_by(Showtime.show_datetime).all()

    notifs = pref.notifications.all()
    alert_movies = pref.alert_movies.all()

    return render_template(
        "alert_detail.html",
        pref=pref,
        alert_movies=alert_movies,
        showtimes=showtimes,
        notifs=notifs,
    )


@main_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """User profile page — view info + update notification prefs and unit preference."""
    user = current_user._get_current_object()
    saved = False

    if request.method == "POST":
        # Only allow updating notification prefs and measurement unit from this page
        user.notify_email = request.form.get("notify_email") == "on"
        user.notify_sms = request.form.get("notify_sms") == "on"
        unit = request.form.get("measurement_unit", "metric")
        if unit in ("metric", "imperial"):
            user.measurement_unit = unit
        db.session.commit()
        saved = True

    return render_template("profile.html", user=user, saved=saved, unit=user.measurement_unit or "metric")


# ---------------------------------------------------------------------------
# Admin: Theater management
# ---------------------------------------------------------------------------

@main_bp.route("/admin/theaters")
@require_role("admin", "editor")
def admin_theaters():
    """Admin: list all theaters (active + inactive)."""
    theaters_list = Theater.query.order_by(
        Theater.country, Theater.state, Theater.city, Theater.name
    ).all()
    countries = Country.query.order_by(Country.name).all()
    chains = Chain.query.order_by(Chain.name).all()
    aspect_ratios = AspectRatio.query.order_by(AspectRatio.label).all()
    projector_types = ProjectorType.query.order_by(ProjectorType.name).all()
    audio_systems = AudioSystem.query.order_by(AudioSystem.name).all()
    regions = Region.query.order_by(Region.name).all()
    cities = City.query.order_by(City.name).all()
    continents = Continent.query.order_by(Continent.name).all()
    return render_template(
        "admin_theaters.html",
        theaters=theaters_list,
        countries=countries,
        chains=chains,
        aspect_ratios=aspect_ratios,
        projector_types=projector_types,
        audio_systems=audio_systems,
        regions=regions,
        cities=cities,
        continents=continents,
        crawl_running=_crawl_running,
        last_summary=_crawl_last_summary,
        geocode_status=_get_geocode_status(),
    )


@main_bp.route("/admin/theaters/new", methods=["GET", "POST"])
@require_role("admin", "editor")
def admin_theater_new():
    """Admin: create a new theater."""
    if request.method == "POST":
        theater = Theater(
            name=request.form["name"].strip(),
            address=request.form.get("address", "").strip(),
            zip_code=request.form.get("zip_code", "").strip(),
            latitude=_parse_float(request.form.get("latitude")),
            longitude=_parse_float(request.form.get("longitude")),
            website=request.form.get("website", "").strip(),
            phone=request.form.get("phone", "").strip(),
            is_active=request.form.get("is_active") == "on",
            crawl_source="manual",
            last_crawled_at=datetime.now(timezone.utc),
        )
        _apply_lookup_fields(theater, request.form)
        db.session.add(theater)
        db.session.commit()
        return redirect(url_for("main.admin_theaters"))

    return render_template(
        "admin_theater_edit.html",
        theater=None,
        is_new=True,
        **_get_lookup_lists(),
        unit=_current_unit(),
    )


@main_bp.route("/admin/theaters/<int:theater_id>/edit", methods=["GET", "POST"])
@require_role("admin", "editor")
def admin_theater_edit(theater_id):
    """Admin: edit an existing theater."""
    theater = Theater.query.get_or_404(theater_id)

    if request.method == "POST":
        theater.name = request.form.get("name", theater.name).strip()
        theater.address = request.form.get("address", theater.address or "").strip()
        theater.zip_code = request.form.get("zip_code", theater.zip_code or "").strip()
        theater.website = request.form.get("website", theater.website or "").strip()
        theater.phone = request.form.get("phone", theater.phone or "").strip()
        # is_active is managed exclusively by Deactivate/Reactivate routes
        theater.latitude = _parse_float(request.form.get("latitude"))
        theater.longitude = _parse_float(request.form.get("longitude"))
        _apply_lookup_fields(theater, request.form)
        db.session.commit()
        return redirect(url_for("main.admin_theater_edit", theater_id=theater.id, saved=1))

    return render_template(
        "admin_theater_edit.html",
        theater=theater,
        is_new=False,
        **_get_lookup_lists(),
        unit=_current_unit(),
    )


@main_bp.route("/admin/theaters/<int:theater_id>/delete", methods=["POST"])
@require_role("admin", "editor")
def admin_theater_delete(theater_id):
    """Admin: soft-delete (deactivate) a theater."""
    theater = Theater.query.get_or_404(theater_id)
    theater.is_active = False
    db.session.commit()
    return redirect(url_for("main.admin_theater_edit", theater_id=theater_id))


@main_bp.route("/admin/theaters/<int:theater_id>/reactivate", methods=["POST"])
@require_role("admin", "editor")
def admin_theater_reactivate(theater_id):
    """Admin: reactivate a previously deactivated theater."""
    theater = Theater.query.get_or_404(theater_id)
    theater.is_active = True
    db.session.commit()
    return redirect(url_for("main.admin_theater_edit", theater_id=theater_id))


# ---------------------------------------------------------------------------
# Admin: User management
# ---------------------------------------------------------------------------

@main_bp.route("/admin/users")
@require_role("admin")
def admin_users():
    """Admin: list all users."""
    users_list = User.query.order_by(User.name).all()
    roles = Role.query.order_by(Role.name).all()
    return render_template("admin_users.html", users=users_list, roles=roles)


@main_bp.route("/admin/users/new", methods=["GET", "POST"])
@require_role("admin")
def admin_user_new():
    """Admin: create a new user."""
    roles = Role.query.order_by(Role.name).all()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        if User.query.filter_by(email=email).first():
            error = f"Email/username '{email}' is already taken."
        else:
            role_id = request.form.get("role_id", type=int)
            user = User(
                name=request.form.get("name", "").strip(),
                email=email,
                phone=request.form.get("phone", "").strip(),
                role_id=role_id,
                is_active=request.form.get("is_active") == "on",
                notify_email=request.form.get("notify_email") == "on",
                notify_sms=request.form.get("notify_sms") == "on",
                measurement_unit=request.form.get("measurement_unit", "metric"),
            )
            password = request.form.get("password", "")
            if password:
                user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return redirect(url_for("main.admin_users"))

    return render_template("admin_user_edit.html", user=None, is_new=True, roles=roles, error=error)


@main_bp.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@require_role("admin")
def admin_user_edit(user_id):
    """Admin: edit an existing user."""
    user = User.query.get_or_404(user_id)
    roles = Role.query.order_by(Role.name).all()
    error = None

    if request.method == "POST":
        new_email    = request.form.get("email", "").strip()
        new_password = request.form.get("password", "").strip()

        # Validate before touching the DB
        existing = User.query.filter_by(email=new_email).first()
        if existing and existing.id != user.id:
            error = f"Email/username '{new_email}' is already taken."
        elif new_password:
            admin_pw = request.form.get("admin_current_password", "").strip()
            if not admin_pw or not current_user.check_password(admin_pw):
                error = "Your current password is incorrect. Password was not changed."

        if not error:
            user.name = request.form.get("name", user.name).strip()
            user.email = new_email
            user.phone = request.form.get("phone", user.phone or "").strip()
            new_role_id = request.form.get("role_id", type=int)
            if new_role_id is not None:
                user.role_id = new_role_id
            user.is_active = request.form.get("is_active") == "on"
            user.notify_email = request.form.get("notify_email") == "on"
            user.notify_sms = request.form.get("notify_sms") == "on"
            user.measurement_unit = request.form.get("measurement_unit", "metric")
            # Location
            lat = _parse_float(request.form.get("location_lat"))
            lon = _parse_float(request.form.get("location_lon"))
            if lat is not None:
                user.location_lat = lat
            if lon is not None:
                user.location_lon = lon
            user.location_name = request.form.get(
                "location_name", user.location_name or ""
            ).strip()
            user.location_address = request.form.get(
                "location_address", user.location_address or ""
            ).strip()
            if new_password:
                user.set_password(new_password)
            db.session.commit()
            return redirect(url_for("main.admin_users"))

    return render_template("admin_user_edit.html", user=user, is_new=False, roles=roles, error=error)


@main_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@require_role("admin")
def admin_user_delete(user_id):
    """Admin: soft-delete (deactivate) a user."""
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        abort(400)  # Cannot deactivate yourself
    user.is_active = False
    db.session.commit()
    return redirect(url_for("main.admin_users"))


# ---------------------------------------------------------------------------
# Admin: Settings
# ---------------------------------------------------------------------------

@main_bp.route("/admin/settings", methods=["GET", "POST"])
@require_role("admin")
def admin_settings():
    """Admin: application settings (external connections, etc.)."""
    if request.method == "POST":
        # --- Simple string/enum keys ---
        for key in (
            "tmdb_api_key", "app_measurement_unit",
            # Email
            "mail_server", "mail_port", "mail_use_tls",
            "mail_username", "mail_password", "mail_from",
            # SMS
            "twilio_account_sid", "twilio_auth_token", "twilio_from_number",
        ):
            val = request.form.get(key, "").strip()
            setting = Settings.query.filter_by(key=key).first()
            if setting:
                setting.value = val
            else:
                db.session.add(Settings(key=key, value=val))

        # --- Schedule keys (integers, validated with sensible bounds) ---
        old_scraper = _get_setting_int("scraper_interval_minutes", 30)
        old_alert   = _get_setting_int("alert_interval_minutes", 15)
        old_crawl   = _get_setting_int("venue_crawl_interval_days", 7)
        old_cleanup = _get_setting_int("cleanup_interval_hours", 24)

        try:
            new_scraper = max(1, min(1440, int(request.form.get("scraper_interval_minutes", old_scraper))))
        except (ValueError, TypeError):
            new_scraper = old_scraper
        try:
            new_alert = max(1, min(1440, int(request.form.get("alert_interval_minutes", old_alert))))
        except (ValueError, TypeError):
            new_alert = old_alert
        try:
            new_crawl = max(1, min(365, int(request.form.get("venue_crawl_interval_days", old_crawl))))
        except (ValueError, TypeError):
            new_crawl = old_crawl
        try:
            new_cleanup = max(1, min(168, int(request.form.get("cleanup_interval_hours", old_cleanup))))
        except (ValueError, TypeError):
            new_cleanup = old_cleanup

        for key, val in (
            ("scraper_interval_minutes", str(new_scraper)),
            ("alert_interval_minutes",   str(new_alert)),
            ("venue_crawl_interval_days", str(new_crawl)),
            ("cleanup_interval_hours",   str(new_cleanup)),
        ):
            setting = Settings.query.filter_by(key=key).first()
            if setting:
                setting.value = val
            else:
                db.session.add(Settings(key=key, value=val))

        db.session.commit()

        # Reload notification credentials into app.config immediately (no restart needed)
        try:
            from app import _load_settings_into_config
            _load_settings_into_config(current_app._get_current_object())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not reload settings into config: %s", exc)

        # Reschedule live if the values changed
        if new_scraper != old_scraper or new_alert != old_alert or new_crawl != old_crawl or new_cleanup != old_cleanup:
            try:
                from app.scheduler import reschedule_jobs
                reschedule_jobs(new_scraper, new_crawl, new_cleanup, new_alert)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not reschedule jobs: %s", exc)

        return redirect(url_for("main.admin_settings"))

    settings_dict = {s.key: s.value for s in Settings.query.all()}
    # Pass scheduler status so the template can show next-run times
    from app.scheduler import get_scheduler_status
    scheduler_status = get_scheduler_status()
    return render_template(
        "admin_settings.html",
        settings=settings_dict,
        scheduler_status=scheduler_status,
    )


@api_bp.route("/admin/smtp/test", methods=["POST"])
@require_role("admin")
def api_test_smtp():
    """Send a test email using the credentials supplied in the request body.

    Accepts JSON with the same keys as the settings form (mail_server, mail_port,
    mail_use_tls, mail_username, mail_password, mail_from).  The test email is
    delivered to the currently logged-in admin's email address.
    """
    from app.notifications import send_email

    data = request.get_json(force=True) or {}
    config = {
        "MAIL_SERVER":   data.get("mail_server", "smtp.gmail.com"),
        "MAIL_PORT":     int(data.get("mail_port") or 587),
        "MAIL_USE_TLS":  str(data.get("mail_use_tls", "true")).lower() in ("true", "1", "yes"),
        "MAIL_USERNAME": data.get("mail_username", ""),
        "MAIL_PASSWORD": data.get("mail_password", ""),
        "MAIL_FROM":     data.get("mail_from", ""),
    }

    to_address = current_user.email
    logger.info(
        "SMTP test requested by %s — server=%s port=%s ssl=%s recipient=%s",
        current_user.email,
        config["MAIL_SERVER"],
        config["MAIL_PORT"],
        config["MAIL_PORT"] == 465 or config["MAIL_USE_TLS"],
        to_address,
    )

    if not to_address or "@" not in to_address:
        logger.warning("SMTP test aborted: admin account %r has no valid email address.", current_user.email)
        return jsonify({
            "success": False,
            "message": "Your admin account has no email address set. "
                       "Add one under Admin → Users before testing.",
        }), 400

    ok, err = send_email(
        config,
        to_address,
        "IMAX Alert — SMTP Test",
        "<p>SMTP is configured correctly. This is a test email from IMAX Alert.</p>",
        "SMTP is configured correctly. This is a test email from IMAX Alert.",
    )
    if ok:
        logger.info("SMTP test succeeded — email delivered to %s.", to_address)
        return jsonify({"success": True, "message": f"Test email sent to {to_address}."})
    logger.error("SMTP test failed: %s", err)
    return jsonify({
        "success": False,
        "message": err or "Send failed — check credentials and server settings.",
    })


@main_bp.route("/admin/lookup")
@require_role("admin")
def admin_lookup():
    """Redirect /admin/lookup to the first lookup sub-page."""
    return redirect(url_for("main.admin_lookup_aspect_ratios"))


@main_bp.route("/admin/lookup/aspect-ratios")
@require_role("admin")
def admin_lookup_aspect_ratios():
    """Admin: aspect ratio lookup table management page."""
    return render_template(
        "admin_lookup_aspect_ratios.html",
        rows=AspectRatio.query.order_by(AspectRatio.label).all(),
    )


@main_bp.route("/admin/lookup/projector-types")
@require_role("admin")
def admin_lookup_projector_types():
    """Admin: projector type lookup table management page."""
    return render_template(
        "admin_lookup_projector_types.html",
        rows=ProjectorType.query.order_by(ProjectorType.name).all(),
    )


@main_bp.route("/admin/lookup/audio-systems")
@require_role("admin")
def admin_lookup_audio_systems():
    """Admin: audio system lookup table management page."""
    return render_template(
        "admin_lookup_audio_systems.html",
        rows=AudioSystem.query.order_by(AudioSystem.name).all(),
    )


@main_bp.route("/admin/lookup/chains")
@require_role("admin")
def admin_lookup_chains():
    """Admin: theater chain lookup table management page."""
    return render_template(
        "admin_lookup_chains.html",
        rows=Chain.query.order_by(Chain.name).all(),
    )


@main_bp.route("/admin/lookup/countries")
@require_role("admin")
def admin_lookup_countries():
    """Admin: country lookup table management page."""
    return render_template(
        "admin_lookup_countries.html",
        rows=Country.query.order_by(Country.name).all(),
    )


@main_bp.route("/admin/lookup/regions")
@require_role("admin")
def admin_lookup_regions():
    """Admin: region/state lookup table management page."""
    return render_template(
        "admin_lookup_regions.html",
        rows=Region.query.order_by(Region.name).all(),
        countries=Country.query.order_by(Country.name).all(),
    )


@main_bp.route("/admin/lookup/cities")
@require_role("admin")
def admin_lookup_cities():
    """Admin: city lookup table management page."""
    return render_template(
        "admin_lookup_cities.html",
        rows=City.query.order_by(City.name).all(),
        countries=Country.query.order_by(Country.name).all(),
    )


@main_bp.route("/admin/lookup/continents")
@require_role("admin")
def admin_lookup_continents():
    """Admin: continent lookup table management page."""
    return render_template(
        "admin_lookup_continents.html",
        rows=Continent.query.order_by(Continent.name).all(),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_float(val):
    """Parse *val* to float, returning None if empty or not numeric."""
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def _get_lookup_lists():
    """Return all lookup table rows for populating admin edit forms."""
    return {
        "chains":         Chain.query.order_by(Chain.name).all(),
        "countries":      Country.query.order_by(Country.name).all(),
        "aspect_ratios":  AspectRatio.query.order_by(AspectRatio.label).all(),
        "projector_types": ProjectorType.query.order_by(ProjectorType.name).all(),
        "audio_systems":  AudioSystem.query.order_by(AudioSystem.name).all(),
    }


def _apply_lookup_fields(theater: Theater, form):
    """
    Set the Theater's FK columns + legacy string columns from a form submission.
    Handles integer IDs submitted from <select> dropdowns, plus "add-new" text
    values that arrive as  __new__<tablename>  hidden fields.
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

    def _get_or_new(field, model_class, get_or_create_fn, *extra_args):
        """Resolve a select field to an FK id; handle '__new__' submissions."""
        val = form.get(field, "").strip()
        new_val = form.get(f"__new__{field}", "").strip()
        if new_val:
            obj = get_or_create_fn(new_val, *extra_args)
        elif val and val.isdigit():
            obj = model_class.query.get(int(val))
        else:
            obj = None
        return obj

    # Chain
    chain_obj = _get_or_new("chain_id", Chain, get_or_create_chain)
    if chain_obj:
        theater.chain_id = chain_obj.id
        theater.chain = chain_obj.name
    elif not form.get("chain_id"):
        theater.chain_id = None
        theater.chain = ""

    # Country
    country_obj = _get_or_new("country_id", Country, get_or_create_country)
    if country_obj:
        theater.country_id = country_obj.id
        theater.country = country_obj.name
    elif not form.get("country_id"):
        theater.country_id = None
        theater.country = ""

    # Region (depends on country)
    region_obj = None
    region_val = form.get("region_id", "").strip()
    new_region = form.get("__new__region_id", "").strip()
    if new_region and country_obj:
        region_obj = get_or_create_region(new_region, country_obj)
    elif region_val and region_val.isdigit():
        region_obj = Region.query.get(int(region_val))
    if region_obj:
        theater.region_id = region_obj.id
        theater.state = region_obj.name
    else:
        theater.region_id = None
        theater.state = ""

    # City (depends on country + region)
    city_val = form.get("city_id", "").strip()
    new_city = form.get("__new__city_id", "").strip()
    city_obj = None
    if new_city and country_obj:
        city_obj = get_or_create_city(new_city, country_obj, region_obj)
    elif city_val and city_val.isdigit():
        city_obj = City.query.get(int(city_val))
    if city_obj:
        theater.city_id = city_obj.id
        theater.city = city_obj.name
    else:
        theater.city_id = None
        theater.city = ""

    # Aspect Ratio
    ar_obj = _get_or_new("aspect_ratio_id", AspectRatio, get_or_create_aspect_ratio)
    if ar_obj:
        theater.aspect_ratio_id = ar_obj.id
        theater.screen_size = ar_obj.label
    else:
        theater.aspect_ratio_id = None
        theater.screen_size = ""

    # Projector Type
    pt_obj = _get_or_new("projector_type_id", ProjectorType, get_or_create_projector_type)
    if pt_obj:
        theater.projector_type_id = pt_obj.id
        theater.projector_type = pt_obj.name
    else:
        theater.projector_type_id = None
        theater.projector_type = ""

    # Audio System
    as_obj = _get_or_new("audio_system_id", AudioSystem, get_or_create_audio_system)
    if as_obj:
        theater.audio_system_id = as_obj.id
        theater.audio_system = as_obj.name
    else:
        theater.audio_system_id = None
        theater.audio_system = ""

    # Screen dimensions
    width_raw = form.get("screen_width", "").strip()
    height_raw = form.get("screen_height", "").strip()
    unit = form.get("dim_unit", "metric")
    w = _parse_float(width_raw)
    h = _parse_float(height_raw)
    if w is not None and h is not None:
        if unit == "imperial":
            w = round(w / 3.28084, 4)
            h = round(h / 3.28084, 4)
        theater.screen_width_m  = w
        theater.screen_height_m = h
        # Also update legacy string
        theater.screen_dims = f"{w:.2f}m\u00d7{h:.2f}m"


# ---------------------------------------------------------------------------
# API: Theaters
# ---------------------------------------------------------------------------

@api_bp.route("/theaters")
@login_required
def api_theaters():
    """Return a list of all active theaters."""
    theaters = Theater.query.filter_by(is_active=True).all()
    return jsonify([t.to_dict() for t in theaters])


@api_bp.route("/theaters/<int:theater_id>")
@login_required
def api_theater(theater_id):
    """Return a single theater by ID."""
    theater = Theater.query.get_or_404(theater_id)
    return jsonify(theater.to_dict())


@api_bp.route("/theaters/<int:theater_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_theater(theater_id):
    """Inline-edit a single FK field on a theater row."""
    theater = Theater.query.get_or_404(theater_id)
    data = request.get_json(force=True) or {}

    _ALLOWED = {
        # key: (Model, display_key, label_fn, legacy_col)
        "chain_id":                 (Chain,        "chain_name",              lambda o: o.name,  "chain"),
        "country_id":               (Country,      "country_name",            lambda o: o.name,  "country"),
        "region_id":                (Region,       "region_name",             lambda o: o.name,  "state"),
        "city_id":                  (City,         "city_name",               lambda o: o.name,  "city"),
        "aspect_ratio_id":          (AspectRatio,  "aspect_ratio_label",      lambda o: o.label, "screen_size"),
        "projector_type_id":        (ProjectorType,"projector_type_name",     lambda o: o.name,  "projector_type"),
        "audio_system_id":          (AudioSystem,  "audio_system_name",       lambda o: o.name,  "audio_system"),
        "continent_id":             (Continent,    "continent_name",          lambda o: o.name,  None),
        "digital_projector_ar_id":  (AspectRatio,  "digital_projector_ar",    lambda o: o.label, None),
        "film_projector_type_id":   (ProjectorType,"film_projector_type_name",lambda o: o.name,  None),
    }

    updated = {}
    for key, value in data.items():
        if key not in _ALLOWED:
            return jsonify({"error": f"Unknown field: {key}"}), 400
        model_cls, display_key, label_fn, legacy_col = _ALLOWED[key]
        if value in (None, "", 0):
            setattr(theater, key, None)
            if legacy_col:
                setattr(theater, legacy_col, None)
            updated[display_key] = None
        else:
            obj = model_cls.query.get(int(value))
            if not obj:
                return jsonify({"error": f"Invalid {key}: {value}"}), 400
            setattr(theater, key, obj.id)
            label = label_fn(obj)
            if legacy_col:
                setattr(theater, legacy_col, label)
            updated[display_key] = label

    db.session.commit()
    return jsonify({"id": theater.id, **updated})


# ---------------------------------------------------------------------------
# API: Movies
# ---------------------------------------------------------------------------

@api_bp.route("/movies")
@login_required
def api_movies():
    """Return all movies ordered by title."""
    movies = Movie.query.order_by(Movie.title).all()
    return jsonify([m.to_dict() for m in movies])


@api_bp.route("/movies/search")
@login_required
def api_movies_search():
    """Search movies — local DB first, TMDB fallback for movies not yet in DB.

    Local DB results always carry a local ``id`` so callers (e.g. the Clear
    Showtimes modal) can use it as a filter key.  TMDB results only appear when
    nothing matched locally; they carry ``tmdb_id`` but no ``id``.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    # Always search local DB first — movies with showtimes live here.
    local_movies = Movie.query.filter(Movie.title.ilike(f"%{q}%")).order_by(Movie.title).limit(20).all()
    if local_movies:
        return jsonify([m.to_dict() for m in local_movies])

    # No local match — fall back to TMDB so users can search upcoming films.
    try:
        from app.tmdb import is_configured, search_movies
        if is_configured():
            return jsonify(search_movies(q))
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB search failed: %s", exc)

    return jsonify([])


# ---------------------------------------------------------------------------
# API: Showtimes
# ---------------------------------------------------------------------------

@api_bp.route("/showtimes")
@login_required
def api_showtimes():
    """Return upcoming showtimes, optionally filtered by theater and/or movie."""
    theater_id = request.args.get("theater_id", type=int)
    movie_id = request.args.get("movie_id", type=int)
    query = Showtime.query.filter(Showtime.show_datetime >= datetime.now(timezone.utc))
    if theater_id:
        query = query.filter_by(theater_id=theater_id)
    if movie_id:
        query = query.filter_by(movie_id=movie_id)
    showtimes = query.order_by(Showtime.show_datetime).all()
    return jsonify([s.to_dict() for s in showtimes])


def _build_showtime_filter_query():
    """Build a Showtime query from common filter request args.

    Accepted query params:
      theater_id  (int)   – filter to a single theater
      movie_id    (int)   – filter to a single movie
      before      (str)   – ISO-8601 datetime; only rows with show_datetime < before

    Returns (query, error_response).  error_response is None on success, or a
    Flask response tuple (json, status) when a param is malformed.
    """
    theater_id = request.args.get("theater_id", type=int)
    movie_id = request.args.get("movie_id", type=int)
    before_str = request.args.get("before", type=str)

    q = Showtime.query
    if theater_id:
        q = q.filter_by(theater_id=theater_id)
    if movie_id:
        q = q.filter_by(movie_id=movie_id)
    if before_str:
        # URL decoding may turn '+' into space; restore it so fromisoformat
        # can parse timezone offsets like '+00:00'.
        before_str = before_str.replace(' ', '+')
        try:
            before_dt = datetime.fromisoformat(before_str)
        except ValueError:
            return None, (jsonify({"error": f"Invalid 'before' value: {before_str!r}"}), 400)
        q = q.filter(Showtime.show_datetime < before_dt)

    return q, None


@api_bp.route("/showtimes/count")
@login_required
def api_showtimes_count():
    """Return the number of showtimes matching optional filters (read-only)."""
    q, err = _build_showtime_filter_query()
    if err:
        return err
    return jsonify({"count": q.count()})


@api_bp.route("/showtimes", methods=["DELETE"])
@require_role("admin")
def api_clear_showtimes():
    """Bulk-delete showtimes matching optional filters.

    With no filters, deletes ALL showtimes.  The confirmation step in the UI
    is the only guard — this endpoint is intentionally unrestricted by design
    (admin-only via @require_role).
    """
    q, err = _build_showtime_filter_query()
    if err:
        return err

    rows = q.all()
    count = len(rows)
    for row in rows:
        db.session.delete(row)
    db.session.commit()
    logger.info("Admin cleared %d showtime(s) via API (filters: %s)", count, request.args)

    from app.scraper import cleanup_orphaned_movies
    orphaned = cleanup_orphaned_movies()
    return jsonify({"deleted": count, "orphaned_movies_removed": orphaned})


# ---------------------------------------------------------------------------
# API: Users
# ---------------------------------------------------------------------------

@api_bp.route("/users", methods=["GET"])
@require_role("admin")
def api_users():
    """Return all users (admin only)."""
    users = User.query.all()
    return jsonify([u.to_dict() for u in users])


@api_bp.route("/users", methods=["POST"])
@require_role("admin")
def api_create_user():
    """Create a new user (admin only)."""
    data = request.get_json(force=True)
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    existing = User.query.filter_by(email=data.get("email")).first()
    if existing:
        return jsonify({"error": "email already registered"}), 409

    user = User(
        name=data["name"],
        email=data.get("email"),
        phone=data.get("phone"),
        location_lat=data.get("location_lat"),
        location_lon=data.get("location_lon"),
        location_name=data.get("location_name"),
        notify_email=data.get("notify_email", True),
        notify_sms=data.get("notify_sms", False),
    )
    if data.get("password"):
        user.set_password(data["password"])
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201


@api_bp.route("/users/<int:user_id>", methods=["PUT"])
@login_required
def api_update_user(user_id):
    """Update a user's profile. Users may only update themselves; admins may update anyone."""
    # Users can only update themselves; admins can update anyone
    if current_user.role_name != "admin" and current_user.id != user_id:
        abort(403)
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True)
    allowed = ("name", "email", "phone", "location_lat", "location_lon",
               "location_name", "location_address", "notify_email", "notify_sms",
               "measurement_unit")
    for field in allowed:
        if field in data:
            setattr(user, field, data[field])
    db.session.commit()
    return jsonify(user.to_dict())


# ---------------------------------------------------------------------------
# API: Alert Preferences
# ---------------------------------------------------------------------------

@api_bp.route("/alerts", methods=["GET"])
@login_required
def api_alerts():
    """Return alert preferences. Admins see all; users see only their own."""
    if current_user.role_name == "admin":
        prefs = AlertPreference.query.order_by(AlertPreference.created_at.desc()).all()
    else:
        prefs = AlertPreference.query.filter_by(
            user_id=current_user.id
        ).order_by(AlertPreference.created_at.desc()).all()
    return jsonify([p.to_dict() for p in prefs])


@api_bp.route("/alerts", methods=["POST"])
@login_required
def api_create_alert():
    data = request.get_json(force=True)
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id is required"}), 400

    # Users can only create alerts for themselves
    if current_user.role_name != "admin" and data["user_id"] != current_user.id:
        abort(403)

    user = User.query.get(data["user_id"])
    if not user:
        return jsonify({"error": "user not found"}), 404

    theater_id = data.get("theater_id") or None

    # ── Resolve movies ────────────────────────────────────────────────────
    # Accept both plural (new) and singular (backward compat) fields.
    raw_movie_ids  = data.get("movie_ids") or (
        [data["movie_id"]] if data.get("movie_id") else []
    )
    raw_tmdb_ids   = data.get("tmdb_ids") or (
        [data["tmdb_id"]] if data.get("tmdb_id") else []
    )

    resolved_movies: list[Movie] = []

    # Resolve DB movie IDs
    for mid in raw_movie_ids:
        if not mid:
            continue
        m = Movie.query.get(mid)
        if not m:
            return jsonify({"error": f"movie id={mid} not found"}), 404
        resolved_movies.append(m)

    # Resolve TMDB IDs (get-or-create)
    for tmdb_id in raw_tmdb_ids:
        if not tmdb_id:
            continue
        movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
        if not movie:
            try:
                from app.tmdb import get_movie_details
                from datetime import date as _date
                details = get_movie_details(tmdb_id)
                raw_date = details.get("release_date")
                try:
                    parsed_date = _date.fromisoformat(raw_date) if raw_date else None
                except ValueError:
                    parsed_date = None
                movie = Movie(
                    title=details.get("title", "Unknown"),
                    description=details.get("overview", ""),
                    poster_url=details.get("poster_url", ""),
                    tmdb_id=tmdb_id,
                    release_date=parsed_date,
                    runtime_minutes=details.get("runtime"),
                    rating=details.get("rating", ""),
                )
                db.session.add(movie)
                db.session.flush()
            except Exception as exc:  # noqa: BLE001
                logger.error("TMDB movie lookup failed: %s", exc)
                return jsonify({"error": "TMDB movie lookup failed"}), 500
        resolved_movies.append(movie)

    # Deduplicate (preserve order)
    seen_ids: set = set()
    unique_movies: list[Movie] = []
    for m in resolved_movies:
        if m.id not in seen_ids:
            seen_ids.add(m.id)
            unique_movies.append(m)
    resolved_movies = unique_movies

    # ── Duplicate / conflict check ────────────────────────────────────────
    # A user cannot have the same (movie, theater) in any active alert unless
    # that specific movie's AlertMovie row has already fired (alert_sent=True).
    conflicting_titles: list[str] = []
    for m in resolved_movies:
        conflict = (
            AlertMovie.query
            .join(AlertPreference)
            .filter(
                AlertPreference.user_id == user.id,
                AlertPreference.theater_id == theater_id,
                AlertPreference.is_active == True,  # noqa: E712
                AlertMovie.movie_id == m.id,
                AlertMovie.alert_sent == False,  # noqa: E712
            )
            .first()
        )
        if conflict:
            conflicting_titles.append(m.title)

    if conflicting_titles:
        return jsonify({
            "error": "duplicate_alert",
            "message": (
                "An active alert already exists for: "
                + ", ".join(conflicting_titles)
                + ". Reset or remove the existing alert first."
            ),
            "conflicting_movies": conflicting_titles,
        }), 409

    # ── Check for existing any-movie alert for same theater ───────────────
    # (Only if no specific movies were provided — i.e. new alert is also any-movie)
    if not resolved_movies:
        existing_any = AlertPreference.query.filter_by(
            user_id=user.id,
            theater_id=theater_id,
            is_active=True,
            alert_sent=False,
        ).filter(
            ~AlertPreference.alert_movies.any()  # type: ignore[attr-defined]
        ).first()
        if existing_any:
            return jsonify({
                "error": "alert already exists",
                "alert": existing_any.to_dict(),
            }), 409

    # ── Create AlertPreference + AlertMovie rows ──────────────────────────
    raw_max = data.get("max_notifications")
    try:
        max_notifications = int(raw_max) if raw_max is not None and raw_max != "" else None
        if max_notifications is not None and max_notifications < 1:
            max_notifications = None
    except (ValueError, TypeError):
        max_notifications = None

    pref = AlertPreference(
        user_id=user.id,
        theater_id=theater_id,
        max_notifications=max_notifications,
    )
    db.session.add(pref)
    db.session.flush()  # get pref.id

    for m in resolved_movies:
        am = AlertMovie(alert_id=pref.id, movie_id=m.id)
        db.session.add(am)

    db.session.commit()

    resp_data = pref.to_dict()

    # Warn if the selected theater has no website
    if theater_id:
        t = Theater.query.get(theater_id)
        if t and not (t.website or "").strip():
            resp_data["warning"] = (
                f"'{t.name}' has no website configured. "
                "Showtimes cannot be scraped for this theater until a website is added "
                "in Admin \u2192 Theaters."
            )

    return jsonify(resp_data), 201


@api_bp.route("/alerts/<int:pref_id>", methods=["DELETE"])
@login_required
def api_delete_alert(pref_id):
    """Soft-delete (deactivate) an alert preference."""
    pref = AlertPreference.query.get_or_404(pref_id)
    if current_user.role_name != "admin" and pref.user_id != current_user.id:
        abort(403)
    pref.is_active = False
    db.session.commit()
    return jsonify({"deleted": True, "id": pref_id})


@api_bp.route("/alerts/<int:pref_id>", methods=["GET"])
@login_required
def api_get_alert(pref_id):
    """Return full detail for a single AlertPreference including notification history."""
    pref = AlertPreference.query.get_or_404(pref_id)
    if current_user.role_name != "admin" and pref.user_id != current_user.id:
        abort(403)

    # Matching showtimes
    q = Showtime.query
    if pref.theater_id:
        q = q.filter_by(theater_id=pref.theater_id)
    if not pref.is_any_movie:
        movie_ids = [am.movie_id for am in pref.alert_movies.all()]
        if movie_ids:
            q = q.filter(Showtime.movie_id.in_(movie_ids))
    showtimes = q.order_by(Showtime.show_datetime).all()

    notifs = pref.notifications.all()

    data = pref.to_dict()
    data["notifications"] = [
        {
            "id": n.id,
            "method": n.method,
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            "success": n.success,
            "error_message": n.error_message,
            "message": n.message,
        }
        for n in notifs
    ]
    data["showtimes"] = [s.to_dict() for s in showtimes]
    return jsonify(data)


@api_bp.route("/alerts/<int:pref_id>/reset", methods=["PATCH"])
@login_required
def api_reset_alert(pref_id):
    """Reset a fired alert so it can trigger again on the next scrape."""
    pref = AlertPreference.query.get_or_404(pref_id)
    if current_user.role_name != "admin" and pref.user_id != current_user.id:
        abort(403)
    for am in pref.alert_movies.all():
        am.alert_sent = False
        am.alert_sent_at = None
    pref.alert_sent = False
    pref.alert_sent_at = None
    pref.is_active = True
    db.session.commit()
    return jsonify(pref.to_dict())


@api_bp.route("/alerts/<int:pref_id>/movies/<int:movie_id>/reset", methods=["PATCH"])
@login_required
def api_reset_alert_movie(pref_id, movie_id):
    """Reset a single AlertMovie row so that specific movie can trigger again."""
    pref = AlertPreference.query.get_or_404(pref_id)
    if current_user.role_name != "admin" and pref.user_id != current_user.id:
        abort(403)
    am = AlertMovie.query.filter_by(alert_id=pref_id, movie_id=movie_id).first_or_404()
    am.alert_sent = False
    am.alert_sent_at = None
    # If the parent pref was fully closed, re-open it
    pref.alert_sent = False
    pref.alert_sent_at = None
    pref.is_active = True
    db.session.commit()
    return jsonify(am.to_dict())


# ---------------------------------------------------------------------------
# API: Lookup tables (Phases 1–4)
# ---------------------------------------------------------------------------

@api_bp.route("/lookup/chains", methods=["GET"])
@login_required
def api_lookup_chains():
    """Return all theater chains ordered by name."""
    return jsonify([c.to_dict() for c in Chain.query.order_by(Chain.name).all()])


@api_bp.route("/lookup/chains", methods=["POST"])
@require_role("admin", "editor")
def api_create_chain():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if Chain.query.filter(db.func.lower(Chain.name) == name.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = Chain(name=name, website=data.get("website", ""))
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/chains/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_chain(obj_id):
    obj = Chain.query.get_or_404(obj_id)
    if Theater.query.filter_by(chain_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/countries", methods=["GET"])
@login_required
def api_lookup_countries():
    """Return all countries ordered by name."""
    return jsonify([c.to_dict() for c in Country.query.order_by(Country.name).all()])


@api_bp.route("/lookup/countries", methods=["POST"])
@require_role("admin", "editor")
def api_create_country():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if Country.query.filter(db.func.lower(Country.name) == name.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = Country(name=name)
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/countries/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_country(obj_id):
    obj = Country.query.get_or_404(obj_id)
    if Theater.query.filter_by(country_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/regions", methods=["GET"])
@login_required
def api_lookup_regions():
    """Return regions, optionally filtered by country_id."""
    country_id = request.args.get("country_id", type=int)
    q = Region.query.order_by(Region.name)
    if country_id:
        q = q.filter_by(country_id=country_id)
    return jsonify([r.to_dict() for r in q.all()])


@api_bp.route("/lookup/regions", methods=["POST"])
@require_role("admin", "editor")
def api_create_region():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    country_id = data.get("country_id")
    if not name or not country_id:
        return jsonify({"error": "name and country_id are required"}), 400
    country = Country.query.get_or_404(country_id)
    from app.lookup_helpers import get_or_create_region
    obj = get_or_create_region(name, country)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/regions/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_region(obj_id):
    obj = Region.query.get_or_404(obj_id)
    if Theater.query.filter_by(region_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/cities", methods=["GET"])
@login_required
def api_lookup_cities():
    """Return cities, optionally filtered by country_id and/or region_id."""
    country_id = request.args.get("country_id", type=int)
    region_id = request.args.get("region_id", type=int)
    q = City.query.order_by(City.name)
    if country_id:
        q = q.filter_by(country_id=country_id)
    if region_id:
        q = q.filter_by(region_id=region_id)
    return jsonify([c.to_dict() for c in q.all()])


@api_bp.route("/lookup/cities", methods=["POST"])
@require_role("admin", "editor")
def api_create_city():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    country_id = data.get("country_id")
    if not name or not country_id:
        return jsonify({"error": "name and country_id are required"}), 400
    country = Country.query.get_or_404(country_id)
    region = Region.query.get(data["region_id"]) if data.get("region_id") else None
    from app.lookup_helpers import get_or_create_city
    obj = get_or_create_city(name, country, region)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/cities/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_city(obj_id):
    obj = City.query.get_or_404(obj_id)
    if Theater.query.filter_by(city_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/aspect-ratios", methods=["GET"])
@login_required
def api_lookup_aspect_ratios():
    """Return all aspect ratios ordered by label."""
    return jsonify(
        [a.to_dict() for a in AspectRatio.query.order_by(AspectRatio.label).all()]
    )


@api_bp.route("/lookup/aspect-ratios", methods=["POST"])
@require_role("admin", "editor")
def api_create_aspect_ratio():
    data = request.get_json(force=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label is required"}), 400
    if AspectRatio.query.filter(db.func.lower(AspectRatio.label) == label.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = AspectRatio(label=label, description=data.get("description", ""))
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/aspect-ratios/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_aspect_ratio(obj_id):
    obj = AspectRatio.query.get_or_404(obj_id)
    if Theater.query.filter_by(aspect_ratio_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/projector-types", methods=["GET"])
@login_required
def api_lookup_projector_types():
    """Return all projector types ordered by name."""
    return jsonify(
        [p.to_dict() for p in ProjectorType.query.order_by(ProjectorType.name).all()]
    )


@api_bp.route("/lookup/projector-types", methods=["POST"])
@require_role("admin", "editor")
def api_create_projector_type():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if ProjectorType.query.filter(db.func.lower(ProjectorType.name) == name.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = ProjectorType(name=name)
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/projector-types/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_projector_type(obj_id):
    obj = ProjectorType.query.get_or_404(obj_id)
    if Theater.query.filter_by(projector_type_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


@api_bp.route("/lookup/audio-systems", methods=["GET"])
@login_required
def api_lookup_audio_systems():
    """Return all audio systems ordered by name."""
    return jsonify(
        [a.to_dict() for a in AudioSystem.query.order_by(AudioSystem.name).all()]
    )


@api_bp.route("/lookup/audio-systems", methods=["POST"])
@require_role("admin", "editor")
def api_create_audio_system():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if AudioSystem.query.filter(db.func.lower(AudioSystem.name) == name.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = AudioSystem(name=name)
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/audio-systems/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_audio_system(obj_id):
    obj = AudioSystem.query.get_or_404(obj_id)
    if Theater.query.filter_by(audio_system_id=obj_id).first():
        return jsonify({"error": "In use by one or more theaters"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": True})


# ---------------------------------------------------------------------------
# API: Lookup PATCH (rename/edit)
# ---------------------------------------------------------------------------

@api_bp.route("/lookup/aspect-ratios/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_aspect_ratio(obj_id):
    obj  = AspectRatio.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "label" in data:
        label = (data["label"] or "").strip()
        if not label:
            return jsonify({"error": "label cannot be blank"}), 400
        dup = AspectRatio.query.filter(
            db.func.lower(AspectRatio.label) == label.lower(),
            AspectRatio.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.label = label
    if "description" in data:
        obj.description = (data["description"] or "").strip()
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/projector-types/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_projector_type(obj_id):
    obj  = ProjectorType.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = ProjectorType.query.filter(
            db.func.lower(ProjectorType.name) == name.lower(),
            ProjectorType.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.name = name
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/audio-systems/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_audio_system(obj_id):
    obj  = AudioSystem.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = AudioSystem.query.filter(
            db.func.lower(AudioSystem.name) == name.lower(),
            AudioSystem.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.name = name
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/chains/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_chain(obj_id):
    obj  = Chain.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = Chain.query.filter(
            db.func.lower(Chain.name) == name.lower(),
            Chain.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.name = name
        # Keep denormalized Theater.chain string in sync with the lookup name
        Theater.query.filter_by(chain_id=obj_id).update({"chain": name})
    if "website" in data:
        obj.website = (data["website"] or "").strip()
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/countries/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_country(obj_id):
    obj  = Country.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = Country.query.filter(
            db.func.lower(Country.name) == name.lower(),
            Country.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.name = name
        Theater.query.filter_by(country_id=obj_id).update({"country": name})
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/regions/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_region(obj_id):
    obj  = Region.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = Region.query.filter(
            db.func.lower(Region.name) == name.lower(),
            Region.country_id == obj.country_id,
            Region.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists in this country"}), 409
        obj.name = name
        Theater.query.filter_by(region_id=obj_id).update({"state": name})
    db.session.commit()
    return jsonify(obj.to_dict())


@api_bp.route("/lookup/cities/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_city(obj_id):
    obj  = City.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = City.query.filter(
            db.func.lower(City.name) == name.lower(),
            City.country_id == obj.country_id,
            City.region_id  == obj.region_id,
            City.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists in this region"}), 409
        obj.name = name
        Theater.query.filter_by(city_id=obj_id).update({"city": name})
    db.session.commit()
    return jsonify(obj.to_dict())


# ---------------------------------------------------------------------------
# API: Lookup — Continents
# ---------------------------------------------------------------------------

@api_bp.route("/lookup/continents", methods=["GET"])
@login_required
def api_get_continents():
    """Return all continents ordered by name."""
    rows = Continent.query.order_by(Continent.name).all()
    return jsonify([r.to_dict() for r in rows])


@api_bp.route("/lookup/continents", methods=["POST"])
@require_role("admin", "editor")
def api_post_continent():
    """Create a new continent entry."""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    if Continent.query.filter(db.func.lower(Continent.name) == name.lower()).first():
        return jsonify({"error": "already exists"}), 409
    obj = Continent(name=name)
    db.session.add(obj)
    db.session.commit()
    return jsonify(obj.to_dict()), 201


@api_bp.route("/lookup/continents/<int:obj_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_continent(obj_id):
    obj = Continent.query.get_or_404(obj_id)
    if obj.theaters.count() > 0:
        return jsonify({"error": "Cannot delete: theaters reference this continent"}), 409
    db.session.delete(obj)
    db.session.commit()
    return jsonify({"deleted": obj_id})


@api_bp.route("/lookup/continents/<int:obj_id>", methods=["PATCH"])
@require_role("admin", "editor")
def api_patch_continent(obj_id):
    obj  = Continent.query.get_or_404(obj_id)
    data = request.get_json(force=True) or {}
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400
        dup = Continent.query.filter(
            db.func.lower(Continent.name) == name.lower(),
            Continent.id != obj_id,
        ).first()
        if dup:
            return jsonify({"error": "already exists"}), 409
        obj.name = name
    db.session.commit()
    return jsonify(obj.to_dict())


# ---------------------------------------------------------------------------
# API: Geocode
# ---------------------------------------------------------------------------

@api_bp.route("/geocode", methods=["POST"])
@login_required
def api_geocode():
    """Geocode an address via Nominatim. Returns {latitude, longitude, formatted_address}."""
    data = request.get_json(force=True) or {}
    from app.venue_crawler import geocode_venue
    result = geocode_venue(
        name=data.get("name", ""),
        city=data.get("city", ""),
        state=data.get("state", ""),
        country=data.get("country", ""),
        address=data.get("address", ""),
        zip_code=data.get("zip_code", ""),
    )
    if result.get("latitude") is None:
        return jsonify({"error": "Could not geocode the provided address"}), 422
    return jsonify(result)


@api_bp.route("/geocode/bulk/trigger", methods=["POST"])
@require_role("admin")
def api_geocode_bulk_trigger():
    """Start a background bulk-geocode job for all theaters missing lat/lng."""
    from app.venue_crawler import get_geocode_status, run_bulk_geocode

    status = get_geocode_status()
    if status["running"]:
        return jsonify({"status": "already_running"}), 409

    # Count theaters that need geocoding so we can return total upfront
    from app import db as _db
    total = Theater.query.filter(
        _db.or_(Theater.latitude.is_(None), Theater.longitude.is_(None))
    ).count()

    if total == 0:
        return jsonify({"status": "nothing_to_do", "total": 0})

    app = current_app._get_current_object()

    thread = threading.Thread(
        target=run_bulk_geocode,
        args=(app,),
        daemon=True,
        name="bulk-geocode",
    )
    thread.start()
    return jsonify({"status": "started", "total": total})


@api_bp.route("/geocode/bulk/status")
@login_required
def api_geocode_bulk_status():
    """Return the current bulk-geocode status."""
    from app.venue_crawler import get_geocode_status
    return jsonify(get_geocode_status())


# ---------------------------------------------------------------------------
# API: Scheduler
# ---------------------------------------------------------------------------

@api_bp.route("/scheduler/status")
@login_required
def api_scheduler_status():
    """Return the current scheduler status and next-run times for all jobs."""
    from app.scheduler import get_scheduler_status
    return jsonify(get_scheduler_status())


@api_bp.route("/scheduler/trigger", methods=["POST"])
@require_role("admin")
def api_trigger_scrape():
    """Manually trigger an immediate scrape."""
    from flask import current_app

    from app.notifications import process_new_showtimes
    from app.scraper import run_all_scrapers

    try:
        new_showtimes = run_all_scrapers()
        sent = process_new_showtimes(current_app._get_current_object(), new_showtimes)
        return jsonify({
            "status": "ok",
            "new_showtimes": len(new_showtimes),
            "notifications_sent": sent,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("Manual scrape failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# API: Venue crawler
# ---------------------------------------------------------------------------

@api_bp.route("/venues/crawl/status")
@login_required
def api_venue_crawl_status():
    from app.scheduler import get_scheduler_status

    scheduler_status = get_scheduler_status()
    venue_job = next(
        (j for j in scheduler_status.get("jobs", []) if j["id"] == "imax_venue_crawl"),
        None,
    )

    total   = Theater.query.count()
    crawled = Theater.query.filter(Theater.crawl_source == "imax_fandom").count()
    manual  = Theater.query.filter(Theater.crawl_source == "manual").count()
    last_crawled = (
        Theater.query.filter(Theater.last_crawled_at.isnot(None))
        .order_by(Theater.last_crawled_at.desc())
        .with_entities(Theater.last_crawled_at)
        .first()
    )

    return jsonify({
        "scheduler_running": scheduler_status.get("running", False),
        "crawl_running": _crawl_running,
        "next_crawl": venue_job["next_run"] if venue_job else None,
        "total_theaters": total,
        "crawl_source_imax_fandom": crawled,
        "crawl_source_manual": manual,
        "last_crawled_at": last_crawled[0].isoformat() if last_crawled and last_crawled[0] else None,
        "last_summary": _crawl_last_summary,
    })


@api_bp.route("/admin/theaters/sync-csv", methods=["POST"])
@require_role("admin")
def api_sync_theaters_from_csv():
    """Re-run the CSV upsert against all current rows. Returns inserted/updated/skipped counts."""
    from flask import current_app

    from app import _upsert_theaters_from_csv
    summary = _upsert_theaters_from_csv(current_app._get_current_object())
    return jsonify(summary)


@api_bp.route("/venues/crawl/trigger", methods=["POST"])
@require_role("admin")
def api_trigger_venue_crawl():
    """Trigger an immediate venue crawl asynchronously."""
    global _crawl_running

    if _crawl_running:
        return jsonify({"status": "already_running"}), 409

    from flask import current_app
    app = current_app._get_current_object()

    def _run():
        global _crawl_running, _crawl_last_summary
        _crawl_running = True
        try:
            from app.venue_crawler import run_venue_crawl
            with app.app_context():
                summary = run_venue_crawl()
            _crawl_last_summary = {**summary, "finished_at": datetime.now(timezone.utc).isoformat()}
            logger.info("Background venue crawl complete: %s", summary)
        except Exception as exc:  # noqa: BLE001
            _crawl_last_summary = {"error": str(exc), "finished_at": datetime.now(timezone.utc).isoformat()}
            logger.error("Background venue crawl failed: %s", exc)
        finally:
            _crawl_running = False

    thread = threading.Thread(target=_run, daemon=True, name="venue-crawl")
    thread.start()
    return jsonify({"status": "started"})


# ---------------------------------------------------------------------------
# API: Notifications log
# ---------------------------------------------------------------------------

@api_bp.route("/notifications")
@login_required
def api_notifications():
    """Return the 50 most recent notifications. Admins see all; users see their own."""
    if current_user.role_name == "admin":
        notifs = Notification.query.order_by(Notification.sent_at.desc()).limit(50).all()
    else:
        notifs = Notification.query.filter_by(
            user_id=current_user.id
        ).order_by(Notification.sent_at.desc()).limit(50).all()
    return jsonify([
        {
            "id": n.id,
            "user_id": n.user_id,
            "method": n.method,
            "message": n.message,
            "sent_at": n.sent_at.isoformat() if n.sent_at else None,
            "success": n.success,
        }
        for n in notifs
    ])
