"""Flask routes for IMAX Alert application."""
import logging
import threading
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask import flash
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

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
    UserInvite,
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


def _get_setting_str(key: str, default: str) -> str:
    """Read a string setting from the Settings table, falling back to default."""
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value else default


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

    # Upcoming showtimes scoped to the current user's alerted theaters.
    # Admins see everything; regular users see only theaters they're watching.
    now = datetime.now(timezone.utc)
    base_q = (
        Showtime.query
        .filter(Showtime.on_demand == False)  # noqa: E712 — exclude manually-fetched rows
        .filter(Showtime.tickets_available.is_(True))
        .filter(Showtime.show_datetime >= now)
        .order_by(Showtime.show_datetime.asc())
    )
    if current_user.role_name == "admin":
        recent_showtimes = base_q.all()
    elif not alerts:
        recent_showtimes = []
    elif any(a.theater_id is None for a in alerts):
        # At least one "any theater" alert — show everything
        recent_showtimes = base_q.all()
    else:
        alert_theater_ids = [a.theater_id for a in alerts]
        recent_showtimes = base_q.filter(
            Showtime.theater_id.in_(alert_theater_ids)
        ).all()

    rows_per_page = _get_setting_int("rows_per_page", 15)

    return render_template(
        "index.html",
        theaters=theaters,
        movies=movies,
        recent_showtimes=recent_showtimes,
        alerts=alerts,
        users=users,
        unit=_current_unit(),
        tracked_movie_count=len(tracked_movie_ids),
        rows_per_page=rows_per_page,
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
    user_lat = current_user.location_lat if current_user.is_authenticated else None
    user_lon = current_user.location_lon if current_user.is_authenticated else None
    return render_template(
        "theaters.html",
        theaters=theaters_list,
        theaters_json=theaters_json,
        aspect_ratios=aspect_ratios,
        projector_types=projector_types,
        continents=continents,
        unit=_current_unit(),
        user_lat=user_lat,
        user_lon=user_lon,
    )


@main_bp.route("/theaters/<int:theater_id>")
@login_required
def theater_detail(theater_id):
    """Theater detail page."""
    import json
    from app.scrapers import ALL_SCRAPERS
    from app.scrapers.base import _haversine_km
    from app.scheduler import is_on_demand_fetch_running

    theater = Theater.query.get_or_404(theater_id)
    showtimes = (
        Showtime.query.filter_by(theater_id=theater_id)
        .filter(Showtime.show_datetime >= datetime.now(timezone.utc))
        .order_by(Showtime.show_datetime)
        .all()
    )

    unit = _current_unit()

    user_distance = None
    if (
        current_user.location_lat is not None
        and current_user.location_lon is not None
        and theater.latitude is not None
        and theater.longitude is not None
    ):
        dist_km = _haversine_km(
            current_user.location_lat, current_user.location_lon,
            theater.latitude, theater.longitude,
        )
        if unit == "imperial":
            user_distance = f"{dist_km / 1.60934:.1f} mi"
        else:
            user_distance = f"{dist_km:.1f} km"

    amenities_list = []
    if theater.amenities:
        try:
            amenities_list = json.loads(theater.amenities)
        except (ValueError, TypeError):
            pass

    has_scraper = any(s.chain_name == theater.chain for s in ALL_SCRAPERS)
    cooldown_hours = _get_setting_int("on_demand_fetch_cooldown_hours", 24)
    fetch_running = is_on_demand_fetch_running(theater_id)

    cooldown_active = False
    cooldown_remaining_sec = 0
    if theater.on_demand_fetched_at and not fetch_running:
        from datetime import timedelta
        elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - theater.on_demand_fetched_at
        remaining = timedelta(hours=cooldown_hours) - elapsed
        if remaining.total_seconds() > 0:
            cooldown_active = True
            cooldown_remaining_sec = int(remaining.total_seconds())

    rows_per_page = _get_setting_int("rows_per_page", 15)

    return render_template(
        "theater_detail.html",
        theater=theater,
        showtimes=showtimes,
        unit=unit,
        user_distance=user_distance,
        amenities_list=amenities_list,
        has_scraper=has_scraper,
        cooldown_hours=cooldown_hours,
        fetch_running=fetch_running,
        cooldown_active=cooldown_active,
        cooldown_remaining_sec=cooldown_remaining_sec,
        rows_per_page=rows_per_page,
    )


@main_bp.route("/theaters/compare")
def theater_compare():
    """IMAX screen size comparison page — public, no login required."""
    theaters_with_dims = (
        Theater.query
        .filter(
            Theater.is_active == True,
            Theater.screen_width_m.isnot(None),
            Theater.screen_height_m.isnot(None),
        )
        .order_by(Theater.name)
        .all()
    )

    raw_t = request.args.get("t", "")
    selected_ids = []
    if raw_t:
        for part in raw_t.split(","):
            try:
                selected_ids.append(int(part.strip()))
            except ValueError:
                pass
    selected_ids = selected_ids[:15]

    def _cmp_dict(t):
        return {
            "id": t.id,
            "name": t.name,
            "city": t.city_name or t.city or "",
            "state": t.region_name or t.state or "",
            "country": t.country_name or t.country or "",
            "w": t.screen_width_m,
            "h": t.screen_height_m,
            "wft": t.screen_width_ft,
            "hft": t.screen_height_ft,
            "chain": t.chain_name or "",
            "proj": t.projector_type_name or "",
        }

    return render_template(
        "theater_compare.html",
        theaters=theaters_with_dims,
        theaters_json=[_cmp_dict(t) for t in theaters_with_dims],
        selected_ids=selected_ids,
        unit=_current_unit(),
    )


@main_bp.route("/alerts")
@login_required
def alerts():
    """Alert management page."""
    theaters_list = Theater.query.filter_by(is_active=True).all()
    movies_list = Movie.query.order_by(Movie.title).all()

    from sqlalchemy import desc, case
    if current_user.role_name == "admin":
        users_list = User.query.all()
        prefs = AlertPreference.query.order_by(
            case((AlertPreference.is_active == True, 0), else_=1),
            desc(AlertPreference.created_at),
        ).all()
    else:
        users_list = [current_user]
        prefs = AlertPreference.query.filter_by(user_id=current_user.id).order_by(
            case((AlertPreference.is_active == True, 0), else_=1),
            desc(AlertPreference.created_at),
        ).all()

    rows_per_page = _get_setting_int("rows_per_page", 15)
    default_max_notifications = _get_setting_int("default_max_notifications", 0) or None
    user_has_location = (
        current_user.location_lat is not None and current_user.location_lon is not None
    )
    unit = current_user.measurement_unit or "imperial"
    theaters_coords = [
        {"id": t.id, "name": t.name, "lat": t.latitude, "lng": t.longitude}
        for t in theaters_list
        if t.latitude is not None and t.longitude is not None
    ]
    return render_template(
        "alerts.html",
        theaters=theaters_list,
        theaters_json=[{"id": t.id, "name": t.name, "city": t.city or "", "state": t.state or ""} for t in theaters_list],
        theaters_coords_json=theaters_coords,
        movies=movies_list,
        users=users_list,
        preferences=prefs,
        rows_per_page=rows_per_page,
        default_max_notifications=default_max_notifications,
        user_has_location=user_has_location,
        user_lat=current_user.location_lat,
        user_lng=current_user.location_lon,
        unit=unit,
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
    if pref.radius_km is not None:
        # Radius alert: limit to theaters within radius of user's saved location
        from app.scrapers.base import _haversine_km
        alert_user = User.query.get(pref.user_id)
        if alert_user and alert_user.location_lat is not None and alert_user.location_lon is not None:
            nearby_ids = [
                t.id for t in Theater.query.filter_by(is_active=True).all()
                if t.latitude is not None and t.longitude is not None
                and _haversine_km(alert_user.location_lat, alert_user.location_lon, t.latitude, t.longitude) <= pref.radius_km
            ]
            q = q.filter(Showtime.theater_id.in_(nearby_ids))
    elif pref.theater_id:
        q = q.filter_by(theater_id=pref.theater_id)
    if not pref.is_any_movie:
        movie_ids = [am.movie_id for am in pref.alert_movies.all()]
        if movie_ids:
            q = q.filter(Showtime.movie_id.in_(movie_ids))
    showtimes = q.order_by(Showtime.show_datetime).all()

    notifs = Notification.query.filter_by(alert_preference_id=pref.id).order_by(Notification.sent_at.desc()).all()
    alert_movies = pref.alert_movies.all()
    rows_per_page = _get_setting_int("rows_per_page", 15)

    return render_template(
        "alert_detail.html",
        pref=pref,
        alert_movies=alert_movies,
        showtimes=showtimes,
        notifs=notifs,
        rows_per_page=rows_per_page,
    )


@main_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    code = 308 if request.method == "POST" else 301
    return redirect(url_for("main.settings_account"), code)


@main_bp.route("/settings")
@login_required
def settings():
    return redirect(url_for("main.settings_account"))


@main_bp.route("/settings/account", methods=["GET", "POST"])
@login_required
def settings_account():
    """My Account — view profile info + update preferences."""
    user = current_user._get_current_object()
    saved = False

    if request.method == "POST":
        user.notify_email = request.form.get("notify_email") == "on"
        user.notify_sms = request.form.get("notify_sms") == "on"
        unit = request.form.get("measurement_unit", "metric")
        if unit in ("metric", "imperial"):
            user.measurement_unit = unit
        tz = request.form.get("timezone", "UTC").strip()
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(tz)
            user.timezone = tz
        except (ZoneInfoNotFoundError, KeyError):
            pass
        db.session.commit()
        saved = True

    return render_template("settings_account.html", user=user, saved=saved, unit=user.measurement_unit or "metric")


@main_bp.route("/profile/mfa-setup", methods=["GET", "POST"])
@login_required
def profile_mfa_setup():
    """Show QR code and confirm TOTP enrollment."""
    user = current_user._get_current_object()
    error = None
    recovery_codes = None

    if request.method == "POST":
        action = request.form.get("action")
        if action == "begin":
            if not user.mfa_secret or user.mfa_enabled:
                if user.mfa_enabled:
                    user.mfa_enabled = False  # clear flag so login doesn't demand the new unconfirmed secret if reconfiguration is abandoned
                user.generate_mfa_secret()
                db.session.commit()
            qr_image = _make_qr_data_url(user.mfa_totp_uri())
            return render_template("mfa_setup.html", user=user, step="confirm",
                                   qr_image=qr_image, secret=user.mfa_secret)
        elif action == "confirm":
            if not user.mfa_secret:
                return redirect(url_for("main.profile_mfa_setup"))
            code = request.form.get("totp_code", "").strip()
            if user.verify_totp(code):
                user.mfa_enabled = True
                recovery_codes = user.generate_recovery_codes()
                db.session.commit()
                from app.log_utils import write_log
                write_log("auth", f"MFA enabled by {user.email}", user_id=user.id)
                return render_template("mfa_setup.html", user=user, step="done",
                                       recovery_codes=recovery_codes)
            else:
                error = "Invalid code — please try again."
                qr_image = _make_qr_data_url(user.mfa_totp_uri())
                return render_template("mfa_setup.html", user=user, step="confirm",
                                       qr_image=qr_image, secret=user.mfa_secret,
                                       error=error)
        elif action == "regen_recovery":
            if not user.mfa_enabled:
                return redirect(url_for("main.settings_account"))
            recovery_codes = user.generate_recovery_codes()
            db.session.commit()
            return render_template("mfa_setup.html", user=user, step="done",
                                   recovery_codes=recovery_codes)

    return render_template("mfa_setup.html", user=user, step="begin")


def _make_qr_data_url(uri: str) -> str:
    """Generate a QR code for *uri* and return it as a base64 PNG data URL."""
    import base64
    import io
    import qrcode
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@main_bp.route("/profile/mfa-disable", methods=["POST"])
@login_required
def profile_mfa_disable():
    """Disable MFA for the current user after re-authentication."""
    user = current_user._get_current_object()
    password = request.form.get("password", "")
    if not user.check_password(password):
        flash("Incorrect password. MFA was not disabled.", "error")
        return redirect(url_for("main.settings_account"))
    user.clear_mfa()
    db.session.commit()
    from app.log_utils import write_log
    write_log("auth", f"MFA disabled by {user.email}", user_id=user.id)
    flash("Multi-factor authentication has been disabled.", "success")
    return redirect(url_for("main.settings_account"))


# ---------------------------------------------------------------------------
# Movies: user-scoped tracked movie list and detail
# ---------------------------------------------------------------------------

@main_bp.route("/movies")
@login_required
def movies():
    """Movies tab — all movies the user has ever had an alert for, plus on-demand scraped movies."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Alert-linked movies (include fired/deleted so tiles persist beyond the alert lifecycle)
    alert_rows = (
        db.session.query(AlertMovie.movie_id, func.count(AlertMovie.id).label("alert_count"))
        .join(AlertPreference, AlertMovie.alert_id == AlertPreference.id)
        .filter(AlertPreference.user_id == current_user.id)
        .group_by(AlertMovie.movie_id)
        .all()
    )
    alert_movie_ids = [r.movie_id for r in alert_rows]
    alert_counts = {r.movie_id: r.alert_count for r in alert_rows}

    # On-demand movies: movies surfaced by manual theater fetches with upcoming showtimes
    on_demand_id_rows = (
        db.session.query(Showtime.movie_id)
        .filter(Showtime.on_demand == True, Showtime.show_datetime >= now)  # noqa: E712
        .distinct()
        .all()
    )
    on_demand_movie_ids = [r.movie_id for r in on_demand_id_rows]

    all_movie_ids = list(set(alert_movie_ids) | set(on_demand_movie_ids))
    if not all_movie_ids:
        return render_template("movies.html", movie_list=[])

    movies_map = {m.id: m for m in Movie.query.filter(Movie.id.in_(all_movie_ids)).all()}

    next_dt_rows = (
        db.session.query(Showtime.movie_id, func.min(Showtime.show_datetime).label("next_dt"))
        .filter(Showtime.movie_id.in_(all_movie_ids), Showtime.show_datetime >= now)
        .group_by(Showtime.movie_id)
        .all()
    )
    next_dt_map = {r.movie_id: r.next_dt for r in next_dt_rows}

    # Count distinct theaters the user has configured alerts for (alert movies only)
    theater_count_rows = (
        db.session.query(
            AlertMovie.movie_id,
            func.count(func.distinct(AlertPreference.theater_id)).label("cnt"),
        )
        .join(AlertPreference, AlertMovie.alert_id == AlertPreference.id)
        .filter(
            AlertMovie.movie_id.in_(alert_movie_ids),
            AlertPreference.user_id == current_user.id,
            AlertPreference.theater_id.isnot(None),
        )
        .group_by(AlertMovie.movie_id)
        .all()
    )
    theater_counts = {r.movie_id: r.cnt for r in theater_count_rows}

    movie_list = []
    for mid in all_movie_ids:
        movie = movies_map.get(mid)
        if not movie:
            continue
        movie_list.append({
            "movie": movie,
            "alert_count": alert_counts.get(mid, 0),
            "theater_count": theater_counts.get(mid, 0),
            "next_dt": next_dt_map.get(mid),
        })

    movie_list.sort(key=lambda x: (
        x["next_dt"] is None,
        x["next_dt"] or datetime.max,
        x["movie"].title.lower(),
    ))

    movies_per_page = _get_setting_int("movies_per_page", 50)
    return render_template("movies.html", movie_list=movie_list, movies_per_page=movies_per_page)


@main_bp.route("/movies/<int:movie_id>")
@login_required
def movie_detail(movie_id):
    """Movie detail page — showtimes and alerts for one tracked movie."""
    from app import tmdb as tmdb_mod

    movie = Movie.query.get_or_404(movie_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Allow access if user has an alert for this movie, or if it has on-demand showtimes
    has_alert = (
        db.session.query(AlertMovie.id)
        .join(AlertPreference, AlertMovie.alert_id == AlertPreference.id)
        .filter(
            AlertPreference.user_id == current_user.id,
            AlertMovie.movie_id == movie_id,
        )
        .first()
    )
    has_on_demand = (
        Showtime.query
        .filter(
            Showtime.movie_id == movie_id,
            Showtime.on_demand == True,  # noqa: E712
            Showtime.show_datetime >= now,
        )
        .first()
    )
    if not has_alert and not has_on_demand:
        abort(404)

    showtimes = (
        Showtime.query
        .options(joinedload(Showtime.theater))
        .filter(Showtime.movie_id == movie_id, Showtime.show_datetime >= now)
        .order_by(Showtime.show_datetime)
        .all()
    )

    user_alerts = (
        AlertPreference.query
        .join(AlertMovie, AlertMovie.alert_id == AlertPreference.id)
        .filter(
            AlertPreference.user_id == current_user.id,
            AlertPreference.is_active.is_(True),
            AlertMovie.movie_id == movie_id,
        )
        .options(joinedload(AlertPreference.theater))
        .all()
    )

    tmdb_extra = {}
    if movie.tmdb_id and tmdb_mod.is_configured():
        tmdb_extra = tmdb_mod.get_movie_details(movie.tmdb_id)

    rows_per_page = _get_setting_int("rows_per_page", 15)

    return render_template(
        "movie_detail.html",
        movie=movie,
        showtimes=showtimes,
        user_alerts=user_alerts,
        tmdb_extra=tmdb_extra,
        rows_per_page=rows_per_page,
    )


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
    """Admin: list all users and pending invites."""
    users_list = User.query.order_by(User.name).all()
    roles = Role.query.order_by(Role.name).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pending_invites = UserInvite.query.options(
        joinedload(UserInvite.created_by)
    ).filter(
        UserInvite.accepted_at.is_(None),
        UserInvite.expires_at > now,
    ).order_by(UserInvite.created_at.desc()).all()
    return render_template("admin_users.html", users=users_list, roles=roles,
                           pending_invites=pending_invites)


@main_bp.route("/admin/users/new", methods=["GET", "POST"])
@require_role("admin")
def admin_user_new():
    """Admin: create a new user."""
    roles = Role.query.order_by(Role.name).all()
    error = None
    submitted_user = None
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
            if not password:
                error = "A temporary password is required."
            else:
                from app.auth import validate_password_strength
                error = validate_password_strength(password)
                if not error:
                    user.set_password(password)
            if not error:
                db.session.add(user)
                db.session.commit()
                return redirect(url_for("main.admin_users"))
            submitted_user = user  # preserve form data for re-render

    return render_template("admin_user_edit.html", user=submitted_user, is_new=True, roles=roles, error=error)


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
            else:
                from app.auth import validate_password_strength
                error = validate_password_strength(new_password, current_hash=user.password_hash)

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


@main_bp.route("/admin/users/<int:user_id>/reset-mfa", methods=["POST"])
@require_role("admin")
def admin_user_reset_mfa(user_id):
    """Admin: clear MFA for a user so they can re-enroll."""
    user = User.query.get_or_404(user_id)
    user.clear_mfa()
    db.session.commit()
    from app.log_utils import write_log
    write_log("auth", f"Admin reset MFA for user {user.email}", user_id=current_user.id)
    flash(f"MFA has been reset for {user.name}.", "success")
    return redirect(url_for("main.admin_user_edit", user_id=user_id))


# ---------------------------------------------------------------------------
# Admin: User invites
# ---------------------------------------------------------------------------

@main_bp.route("/admin/users/invite", methods=["POST"])
@require_role("admin")
def admin_user_invite():
    """Admin: send an email invite to a new user."""
    email = request.form.get("invite_email", "").strip().lower()
    role_id = request.form.get("invite_role_id", type=int)

    if not email:
        flash("Email address is required.", "error")
        return redirect(url_for("main.admin_users"))

    if User.query.filter(db.func.lower(User.email) == email).first():
        flash(f"A user with email '{email}' already exists.", "error")
        return redirect(url_for("main.admin_users"))

    existing = UserInvite.query.filter(
        db.func.lower(UserInvite.email) == email,
        UserInvite.accepted_at.is_(None),
        UserInvite.expires_at > datetime.now(timezone.utc).replace(tzinfo=None),
    ).first()
    if existing:
        flash(f"A pending invite for '{email}' already exists.", "warning")
        return redirect(url_for("main.admin_users"))

    invite, raw_token = UserInvite.create(email, role_id, current_user.id)
    db.session.add(invite)
    db.session.commit()

    try:
        _send_invite_email(invite, raw_token)
        from app.log_utils import write_log
        write_log("auth", f"Invite sent to {email} by {current_user.email}", user_id=current_user.id)
        flash(f"Invite sent to {email}.", "success")
    except Exception:
        logger.exception("Failed to send invite email to %s", email)
        flash(f"Invite created but email delivery failed for {email}.", "warning")

    return redirect(url_for("main.admin_users"))


@main_bp.route("/admin/users/invites/<int:invite_id>/revoke", methods=["POST"])
@require_role("admin")
def admin_invite_revoke(invite_id):
    """Admin: revoke a pending invite."""
    invite = UserInvite.query.get_or_404(invite_id)
    invite_email = invite.email  # capture before expunge
    db.session.delete(invite)
    db.session.commit()
    from app.log_utils import write_log
    write_log("auth", f"Invite for {invite_email} revoked by {current_user.email}", user_id=current_user.id)
    return redirect(url_for("main.admin_users"))


def _send_invite_email(invite: "UserInvite", raw_token: str):
    """Send invitation email with signup link."""
    from app.notifications import send_email
    signup_url = url_for("auth.accept_invite", token=raw_token, _external=True)
    subject = "You've been invited to IMAX Alert"
    body_html = f"""
<p>You've been invited to join IMAX Alert. Click the link below to create your account:</p>
<p><a href="{signup_url}">{signup_url}</a></p>
<p>This invitation expires in 48 hours.</p>
<p>— IMAX Alert</p>
"""
    body_text = (
        f"You've been invited to join IMAX Alert.\n\n"
        f"Create your account here:\n{signup_url}\n\n"
        f"This invitation expires in 48 hours.\n\n— IMAX Alert"
    )
    success, err = send_email(current_app.config, invite.email, subject, body_html, body_text)
    if not success:
        raise RuntimeError(f"Email delivery failed: {err}")


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
        old_scraper       = _get_setting_int("scraper_interval_minutes", 30)
        old_alert         = _get_setting_int("alert_interval_minutes", 15)
        old_crawl         = _get_setting_int("venue_crawl_interval_days", 7)
        old_cleanup       = _get_setting_int("cleanup_interval_hours", 24)
        old_rows_per_page = _get_setting_int("rows_per_page", 15)

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
        try:
            new_rows_per_page = max(5, min(100, int(request.form.get("rows_per_page", old_rows_per_page))))
        except (ValueError, TypeError):
            new_rows_per_page = old_rows_per_page
        old_movies_per_page = _get_setting_int("movies_per_page", 50)
        try:
            new_movies_per_page = max(10, min(200, int(request.form.get("movies_per_page", old_movies_per_page))))
        except (ValueError, TypeError):
            new_movies_per_page = old_movies_per_page
        old_log_retention = _get_setting_int("log_retention_days", 30)
        try:
            new_log_retention = max(1, min(365, int(request.form.get("log_retention_days", old_log_retention))))
        except (ValueError, TypeError):
            new_log_retention = old_log_retention
        old_on_demand_cooldown = _get_setting_int("on_demand_fetch_cooldown_hours", 24)
        try:
            new_on_demand_cooldown = max(1, min(168, int(request.form.get("on_demand_fetch_cooldown_hours", old_on_demand_cooldown))))
        except (ValueError, TypeError):
            new_on_demand_cooldown = old_on_demand_cooldown
        old_session_timeout = _get_setting_int("session_timeout_minutes", 60)
        try:
            raw_timeout = int(request.form.get("session_timeout_minutes", old_session_timeout))
            new_session_timeout = 0 if raw_timeout == 0 else max(5, min(1440, raw_timeout))
        except (ValueError, TypeError):
            new_session_timeout = old_session_timeout

        # default_max_notifications: optional positive int, blank = delete/clear
        raw_def_max = request.form.get("default_max_notifications", "").strip()
        if raw_def_max:
            try:
                new_def_max = str(max(1, int(raw_def_max)))
            except (ValueError, TypeError):
                new_def_max = ""
        else:
            new_def_max = ""
        setting = Settings.query.filter_by(key="default_max_notifications").first()
        if new_def_max:
            if setting:
                setting.value = new_def_max
            else:
                db.session.add(Settings(key="default_max_notifications", value=new_def_max))
        elif setting:
            db.session.delete(setting)

        for key, val in (
            ("scraper_interval_minutes",       str(new_scraper)),
            ("alert_interval_minutes",         str(new_alert)),
            ("venue_crawl_interval_days",      str(new_crawl)),
            ("cleanup_interval_hours",         str(new_cleanup)),
            ("rows_per_page",                  str(new_rows_per_page)),
            ("movies_per_page",                str(new_movies_per_page)),
            ("log_retention_days",             str(new_log_retention)),
            ("on_demand_fetch_cooldown_hours", str(new_on_demand_cooldown)),
            ("session_timeout_minutes",        str(new_session_timeout)),
        ):
            setting = Settings.query.filter_by(key=key).first()
            if setting:
                setting.value = val
            else:
                db.session.add(Settings(key=key, value=val))

        # --- Health check schedule ---
        old_hc_freq = _get_setting_str("health_check_frequency", "daily")
        old_hc_time = _get_setting_str("health_check_time", "00:00")
        old_hc_dow  = _get_setting_str("health_check_day_of_week", "0")
        old_hc_dom  = _get_setting_str("health_check_day_of_month", "1")
        old_hc_tz   = _get_setting_str("health_check_timezone", "UTC")

        new_hc_freq = request.form.get("health_check_frequency", "daily")
        if new_hc_freq not in ("daily", "weekly", "monthly"):
            new_hc_freq = "daily"

        raw_hc_time = request.form.get("health_check_time", "00:00").strip()
        try:
            hh, mm = (int(x) for x in raw_hc_time.split(":"))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
            new_hc_time = f"{hh:02d}:{mm:02d}"
        except (ValueError, TypeError):
            new_hc_time = "00:00"

        try:
            new_hc_dow = str(max(0, min(6, int(request.form.get("health_check_day_of_week", "0")))))
        except (ValueError, TypeError):
            new_hc_dow = "0"

        try:
            new_hc_dom = str(max(1, min(31, int(request.form.get("health_check_day_of_month", "1")))))
        except (ValueError, TypeError):
            new_hc_dom = "1"

        raw_hc_tz = request.form.get("health_check_timezone", "UTC").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(raw_hc_tz)
            new_hc_tz = raw_hc_tz
        except Exception:
            new_hc_tz = old_hc_tz or "UTC"

        for key, val in (
            ("health_check_frequency",    new_hc_freq),
            ("health_check_time",         new_hc_time),
            ("health_check_day_of_week",  new_hc_dow),
            ("health_check_day_of_month", new_hc_dom),
            ("health_check_timezone",     new_hc_tz),
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

        # Invalidate the cached session timeout so the context processor re-reads it
        current_app.config["SESSION_TIMEOUT_MINUTES"] = new_session_timeout

        # Reschedule live if the values changed
        if new_scraper != old_scraper or new_alert != old_alert or new_crawl != old_crawl or new_cleanup != old_cleanup:
            try:
                from app.scheduler import reschedule_jobs
                reschedule_jobs(new_scraper, new_crawl, new_cleanup, new_alert)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not reschedule jobs: %s", exc)

        if (new_hc_freq != old_hc_freq or new_hc_time != old_hc_time
                or new_hc_dow != old_hc_dow or new_hc_dom != old_hc_dom
                or new_hc_tz != old_hc_tz):
            try:
                from app.scheduler import reschedule_health_check
                reschedule_health_check(new_hc_freq, new_hc_time, new_hc_dow, new_hc_dom, new_hc_tz)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not reschedule health check: %s", exc)

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


@api_bp.route("/session/ping", methods=["GET"])
@login_required
def session_ping():
    """Extend the current session; used by the client-side idle timeout heartbeat."""
    return jsonify({"ok": True})


@main_bp.route("/admin/logs")
@require_role("admin")
def admin_logs():
    """Admin: in-app structured log viewer."""
    from app.models import LogEntry
    level_filter = request.args.get("level", "")
    category_filter = request.args.get("category", "")
    q = LogEntry.query
    if level_filter:
        q = q.filter_by(level=level_filter)
    if category_filter:
        q = q.filter_by(category=category_filter)
    entries = q.order_by(LogEntry.created_at.desc()).limit(500).all()
    rows_per_page = _get_setting_int("rows_per_page", 15)
    categories = ["auth", "alert", "scrape", "notification", "geocode", "admin", "system"]
    return render_template(
        "admin_logs.html",
        entries=entries,
        rows_per_page=rows_per_page,
        level_filter=level_filter,
        category_filter=category_filter,
        categories=categories,
    )


@main_bp.route("/admin/system-status")
@require_role("admin")
def admin_system_status():
    """Admin: scraper health status dashboard."""
    from sqlalchemy import func
    from app import db
    from app.models import ScraperStatus, Theater
    from app.scrapers import ALL_SCRAPERS
    from app.scheduler import get_scheduler_status, get_health_check_state

    chain_names = [s.chain_name for s in ALL_SCRAPERS]

    # Latest ScraperStatus per chain — 1 query via self-join on max checked_at
    subq = (
        db.session.query(
            ScraperStatus.chain_name,
            func.max(ScraperStatus.checked_at).label("max_at"),
        )
        .group_by(ScraperStatus.chain_name)
        .subquery()
    )
    latest_rows = (
        db.session.query(ScraperStatus)
        .join(
            subq,
            (ScraperStatus.chain_name == subq.c.chain_name)
            & (ScraperStatus.checked_at == subq.c.max_at),
        )
        .all()
    )
    latest_by_chain = {r.chain_name: r for r in latest_rows}

    # Theater counts per chain — 1 query via GROUP BY
    count_rows = (
        db.session.query(Theater.chain, func.count(Theater.id).label("cnt"))
        .filter(Theater.is_active == True)  # noqa: E712
        .group_by(Theater.chain)
        .all()
    )
    theater_count_by_chain = {r.chain: r.cnt for r in count_rows}

    rows = []
    for chain_name in chain_names:
        latest = latest_by_chain.get(chain_name)
        rows.append({
            "chain_name": chain_name,
            "theater_count": theater_count_by_chain.get(chain_name, 0),
            "status": latest.status if latest else "never",
            "checked_at": latest.checked_at if latest else None,
            "showtime_count": latest.showtime_count if latest else None,
            "error_summary": latest.error_summary if latest else None,
        })

    ok_count = sum(1 for r in rows if r["status"] == "ok")
    warning_count = sum(1 for r in rows if r["status"] == "warning")
    error_count = sum(1 for r in rows if r["status"] == "error")
    never_count = sum(1 for r in rows if r["status"] == "never")

    scheduler_status = get_scheduler_status()
    health_job = next(
        (j for j in scheduler_status.get("jobs", []) if j["id"] == "imax_health_check"),
        None,
    )

    return render_template(
        "admin_system_status.html",
        rows=rows,
        ok_count=ok_count,
        warning_count=warning_count,
        error_count=error_count,
        never_count=never_count,
        health_job=health_job,
        health_running=get_health_check_state(),
    )


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


@api_bp.route("/theaters/<int:theater_id>/fetch-showtimes", methods=["POST"])
@login_required
def api_theater_fetch_showtimes(theater_id: int):
    """Trigger an on-demand showtime fetch for a single theater."""
    from datetime import timedelta
    from flask import current_app
    from app.scrapers import ALL_SCRAPERS
    from app.scheduler import trigger_theater_fetch, is_on_demand_fetch_running

    theater = Theater.query.get_or_404(theater_id)

    if is_on_demand_fetch_running(theater_id):
        return jsonify({"status": "in_progress"})

    cooldown_hours = _get_setting_int("on_demand_fetch_cooldown_hours", 24)
    if theater.on_demand_fetched_at:
        elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - theater.on_demand_fetched_at
        if elapsed < timedelta(hours=cooldown_hours):
            remaining = timedelta(hours=cooldown_hours) - elapsed
            return jsonify({
                "status": "cooldown",
                "fetched_at": theater.on_demand_fetched_at.isoformat(),
                "next_available_in_seconds": int(remaining.total_seconds()),
            })

    scraper = next((s for s in ALL_SCRAPERS if s.chain_name == theater.chain), None)
    if scraper is None:
        return jsonify({"status": "no_scraper", "chain": theater.chain}), 422

    trigger_theater_fetch(theater_id, scraper, current_app._get_current_object())
    return jsonify({"status": "started"})


@api_bp.route("/theaters/<int:theater_id>/fetch-showtimes/status")
@login_required
def api_theater_fetch_status(theater_id: int):
    """Return on-demand fetch state for a single theater."""
    from app.models import Showtime
    from app.scheduler import is_on_demand_fetch_running

    theater = Theater.query.get_or_404(theater_id)
    on_demand_count = (
        Showtime.query
        .filter_by(theater_id=theater_id, on_demand=True)
        .filter(Showtime.show_datetime >= datetime.now(timezone.utc))
        .count()
    )
    return jsonify({
        "running": is_on_demand_fetch_running(theater_id),
        "fetched_at": theater.on_demand_fetched_at.isoformat() if theater.on_demand_fetched_at else None,
        "on_demand_count": on_demand_count,
    })


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


@api_bp.route("/movies/enrich-stubs", methods=["POST"])
@require_role("admin")
def api_enrich_movie_stubs():
    """Re-enrich all Movie rows that have no tmdb_id (background thread)."""
    def _run(app):
        with app.app_context():
            from app.scrapers.base import _enrich_movie_from_tmdb
            stubs = Movie.query.filter(Movie.tmdb_id.is_(None)).all()
            fixed = 0
            for m in stubs:
                old_title = m.title
                try:
                    _enrich_movie_from_tmdb(m)
                    if m.tmdb_id or m.title != old_title:
                        fixed += 1
                except Exception as exc:
                    logger.warning("Enrich stub failed for '%s': %s", m.title, exc)
            db.session.commit()
            logger.info("enrich-stubs: processed %d stubs, fixed %d", len(stubs), fixed)

    t = threading.Thread(target=_run, args=(current_app._get_current_object(),), daemon=True)
    t.start()
    stub_count = Movie.query.filter(Movie.tmdb_id.is_(None)).count()
    return jsonify({"ok": True, "stubs_queued": stub_count})


@api_bp.route("/movies/search")
@login_required
def api_movies_search():
    """Search movies — always queries both local DB and TMDB, merges results.

    Local DB results appear first (they already have a local ``id`` and poster).
    TMDB results for titles not yet in the DB follow, deduplicated by tmdb_id.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    local_movies = Movie.query.filter(Movie.title.ilike(f"%{q}%")).order_by(Movie.title).limit(20).all()
    results = [m.to_dict() for m in local_movies]
    local_tmdb_ids = {m.tmdb_id for m in local_movies if m.tmdb_id}

    try:
        from app.tmdb import is_configured, search_movies
        if is_configured():
            for r in search_movies(q):
                if r.get("tmdb_id") not in local_tmdb_ids:
                    results.append(r)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB search failed: %s", exc)

    return jsonify(results[:20])


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
        from app.auth import validate_password_strength
        pw_error = validate_password_strength(data["password"])
        if pw_error:
            return jsonify({"error": pw_error}), 400
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

    # ── Radius alert ──────────────────────────────────────────────────────
    radius_km = None
    raw_radius = data.get("radius_km")
    if raw_radius is not None and raw_radius != "":
        try:
            radius_km = float(raw_radius)
            if radius_km <= 0:
                return jsonify({"error": "radius_km must be positive"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "radius_km must be a number"}), 400
        # Radius alerts are not bound to a specific theater
        theater_id = None
        # Require the user to have a saved location for radius alerts
        if user.location_lat is None or user.location_lon is None:
            return jsonify({"error": "Your profile location must be set before creating a radius alert"}), 400

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

    # Parse target_date before duplicate checks (both checks reference it)
    from datetime import date as date_type
    target_date = None
    raw_date = (data.get("target_date") or "").strip()
    if raw_date:
        try:
            target_date = date_type.fromisoformat(raw_date)
        except ValueError:
            return jsonify({"error": f"Invalid target_date '{raw_date}'; expected YYYY-MM-DD."}), 400

    target_date_buffer = None
    if target_date:
        raw_buffer = data.get("target_date_buffer")
        try:
            buf = int(raw_buffer) if raw_buffer is not None and raw_buffer != "" else None
            target_date_buffer = max(0, buf) if buf is not None else None
        except (ValueError, TypeError):
            pass

    # ── Duplicate / conflict check ────────────────────────────────────────
    # A user cannot have the same (movie, theater, target_date) in any active
    # alert unless that movie's AlertMovie row has already fired.
    # Different target_dates on the same movie/theater are allowed (e.g. same
    # film on June 21 and June 28 are independent alerts).
    # Radius alerts bypass the theater-keyed duplicate check.
    conflicting_titles: list[str] = []
    for m in resolved_movies if not radius_km else []:
        conflict = (
            AlertMovie.query
            .join(AlertPreference)
            .filter(
                AlertPreference.user_id == user.id,
                AlertPreference.theater_id == theater_id,
                AlertPreference.is_active == True,  # noqa: E712
                AlertPreference.target_date == target_date,
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

    # ── Check for existing any-movie alert for same theater + date ────────
    # (Only if no specific movies were provided — i.e. new alert is also any-movie)
    if not resolved_movies:
        existing_any = AlertPreference.query.filter_by(
            user_id=user.id,
            theater_id=theater_id,
            is_active=True,
            alert_sent=False,
            target_date=target_date,
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
        target_date=target_date,
        target_date_buffer=target_date_buffer,
        radius_km=radius_km,
    )
    db.session.add(pref)
    db.session.flush()  # get pref.id

    for m in resolved_movies:
        am = AlertMovie(alert_id=pref.id, movie_id=m.id)
        db.session.add(am)

    db.session.commit()

    resp_data = pref.to_dict()

    movie_names = [m.title for m in resolved_movies] if resolved_movies else ["Any Movie"]
    theater_name = Theater.query.get(theater_id).name if theater_id else "Any Theater"
    from app.log_utils import write_log
    write_log("alert", f"Alert created: {', '.join(movie_names)} @ {theater_name}",
              user_id=current_user.id,
              details={"pref_id": pref.id, "theater_id": theater_id,
                       "target_date": target_date.isoformat() if target_date else None,
                       "target_date_buffer": target_date_buffer})

    warnings: list[str] = []

    # Warn if a dated alert overlaps an existing undated alert for the same
    # movie(s)/theater \u2014 both will fire independently causing duplicate notifications.
    if target_date and theater_id:
        overlap_titles: list[str] = []
        if resolved_movies:
            for m in resolved_movies:
                undated = (
                    AlertMovie.query
                    .join(AlertPreference)
                    .filter(
                        AlertPreference.user_id == user.id,
                        AlertPreference.theater_id == theater_id,
                        AlertPreference.is_active == True,  # noqa: E712
                        AlertPreference.target_date.is_(None),
                        AlertMovie.movie_id == m.id,
                        AlertMovie.alert_sent == False,  # noqa: E712
                    )
                    .first()
                )
                if undated:
                    overlap_titles.append(m.title)
        else:
            # Any-movie dated alert \u2014 check for undated any-movie alert
            undated_any = AlertPreference.query.filter_by(
                user_id=user.id,
                theater_id=theater_id,
                is_active=True,
                alert_sent=False,
                target_date=None,
            ).filter(
                ~AlertPreference.alert_movies.any()  # type: ignore[attr-defined]
            ).first()
            if undated_any:
                overlap_titles.append("Any Movie")

        if overlap_titles:
            overlap_msg = (
                f"An undated alert for {', '.join(overlap_titles)} at {theater_name} "
                "is already active and will also fire on this date. "
                "Both alerts are active \u2014 you may receive duplicate notifications."
            )
            warnings.append(overlap_msg)
            write_log(
                "alert",
                f"Duplicate overlap warning: dated alert #{pref.id} overlaps undated "
                f"alert for {', '.join(overlap_titles)} @ {theater_name}",
                level="WARNING",
                user_id=current_user.id,
                details={"pref_id": pref.id, "theater_id": theater_id,
                         "overlapping_movies": overlap_titles,
                         "target_date": target_date.isoformat()},
            )

    # Warn if the selected theater has no website
    if theater_id:
        t = Theater.query.get(theater_id)
        if t and not (t.website or "").strip():
            warnings.append(
                f"'{t.name}' has no website configured. "
                "Showtimes cannot be scraped for this theater until a website is added "
                "in Admin \u2192 Theaters."
            )

    if warnings:
        resp_data["warning"] = " ".join(warnings)

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
    from app.log_utils import write_log
    write_log("alert", f"Alert removed (id={pref_id})", user_id=current_user.id,
              details={"pref_id": pref_id})
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
    pref.notifications_fired = 0
    if pref.radius_km is not None:
        pref.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.commit()
    from app.log_utils import write_log
    write_log("alert", f"Alert reset (id={pref_id})", user_id=current_user.id,
              details={"pref_id": pref_id})
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
    if pref.radius_km is not None:
        pref.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
    from app.log_utils import write_log
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

    write_log("geocode", f"Bulk geocode triggered: {total} theaters queued",
              user_id=current_user.id, details={"total": total})

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


@api_bp.route("/geocode/bulk/rerun", methods=["POST"])
@require_role("admin")
def api_geocode_bulk_rerun():
    """Re-geocode every theater regardless of whether coordinates already exist."""
    from app.log_utils import write_log
    from app.venue_crawler import get_geocode_status, run_bulk_geocode

    status = get_geocode_status()
    if status["running"]:
        return jsonify({"status": "already_running"}), 409

    total = Theater.query.count()
    write_log("geocode", f"Re-geocode All triggered: {total} theaters queued",
              user_id=current_user.id, details={"total": total})

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=run_bulk_geocode,
        args=(app,),
        kwargs={"mode": "all"},
        daemon=True,
        name="bulk-geocode-all",
    )
    thread.start()
    return jsonify({"status": "started", "total": total})


@api_bp.route("/geocode/reset", methods=["POST"])
@require_role("admin")
def api_geocode_reset():
    """Null latitude and longitude for all theaters so they can be re-geocoded."""
    from app.log_utils import write_log
    from app.venue_crawler import reset_geocoding

    try:
        count = reset_geocoding()
        write_log("geocode", f"Geocoding reset: {count} theaters cleared",
                  user_id=current_user.id, details={"count": count})
        return jsonify({"status": "ok", "count": count})
    except Exception as exc:  # noqa: BLE001
        logger.error("Geocoding reset failed: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@api_bp.route("/admin/theaters/reseed/preview", methods=["POST"])
@require_role("admin")
def api_reseed_preview():
    """Return how many theaters would be updated by a re-seed (dry run)."""
    from app.venue_crawler import reseed_from_csv
    columns = (request.get_json(force=True) or {}).get("columns", [])
    result = reseed_from_csv(columns, dry_run=True)
    return jsonify(result)


@api_bp.route("/admin/theaters/reseed", methods=["POST"])
@require_role("admin")
def api_reseed_from_csv():
    """Restore selected Theater columns from seeds/imax_theaters.csv."""
    from app.log_utils import write_log
    from app.venue_crawler import reseed_from_csv

    columns = (request.get_json(force=True) or {}).get("columns", [])
    if not columns:
        return jsonify({"status": "error", "message": "No columns specified"}), 400

    result = reseed_from_csv(columns)
    level = "WARNING" if result["errors"] else "INFO"
    write_log("geocode",
              f"Re-seed from CSV: {result['updated']} updated, "
              f"{result['unmatched']} unmatched, {len(result['errors'])} errors",
              level=level, user_id=current_user.id,
              details={"columns": columns, **result})
    return jsonify({"status": "ok", **result})


# ---------------------------------------------------------------------------
# API: Scraper health check (on-demand)
# ---------------------------------------------------------------------------

@api_bp.route("/admin/scraper-check/<string:chain_name>", methods=["POST"])
@require_role("admin")
def api_scraper_health_check(chain_name: str):
    """Trigger an on-demand health check for a single scraper chain."""
    from flask import current_app
    from app.scrapers import ALL_SCRAPERS
    from app.scheduler import trigger_single_health_check

    scraper = next((s for s in ALL_SCRAPERS if s.chain_name == chain_name), None)
    if scraper is None:
        return jsonify({"error": f"Unknown chain: {chain_name}"}), 404

    from app.log_utils import write_log
    write_log("scrape", f"On-demand health check triggered for {chain_name}", user_id=current_user.id)

    trigger_single_health_check(scraper, current_app._get_current_object())
    return jsonify({"ok": True, "status": "started", "chain_name": chain_name})


@api_bp.route("/admin/health-check/status")
@require_role("admin")
def api_health_check_status():
    """Return the current running state of the scheduled health-check job."""
    from app.scheduler import get_health_check_state
    return jsonify(get_health_check_state())


@api_bp.route("/admin/health-check/run-all", methods=["POST"])
@require_role("admin")
def api_health_check_run_all():
    """Trigger an on-demand full health check (all chains) in a background thread."""
    from app.scheduler import trigger_health_check, get_health_check_state
    from app.log_utils import write_log

    if get_health_check_state()["running"]:
        return jsonify({"status": "already_running"}), 409

    write_log("scrape", "On-demand full health check triggered", user_id=current_user.id)
    started = trigger_health_check(current_app._get_current_object())
    if started:
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"}), 409


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

    from app.log_utils import write_log
    write_log("scrape", "Manual scrape triggered", user_id=current_user.id)
    try:
        new_showtimes = run_all_scrapers()
        sent = process_new_showtimes(current_app._get_current_object(), new_showtimes)
        write_log("scrape", f"Manual scrape complete: {len(new_showtimes)} new showtimes, {sent} notifications",
                  user_id=current_user.id,
                  details={"new_showtimes": len(new_showtimes), "notifications_sent": sent})
        return jsonify({
            "status": "ok",
            "new_showtimes": len(new_showtimes),
            "notifications_sent": sent,
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("Manual scrape failed: %s", exc)
        write_log("scrape", f"Manual scrape failed: {exc}", level="ERROR", user_id=current_user.id)
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
# API: Theater export / import
# ---------------------------------------------------------------------------

@api_bp.route("/admin/theaters/export", methods=["GET"])
@require_role("admin", "editor")
def api_export_theaters():
    """Download all theaters as a CSV file."""
    from app.venue_crawler import export_theaters_csv

    csv_data = export_theaters_csv()
    resp = make_response(csv_data)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=imax_theaters_export.csv"
    return resp


@api_bp.route("/admin/theaters/import", methods=["POST"])
@require_role("admin")
def api_import_theaters():
    """Import/upsert theaters from an uploaded CSV file."""
    from app.log_utils import write_log
    from app.venue_crawler import import_theaters_from_csv_str

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"status": "error", "message": "File must be a .csv"}), 400

    try:
        csv_text = f.read().decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"status": "error", "message": "File must be UTF-8 encoded"}), 400

    result = import_theaters_from_csv_str(csv_text)
    level = "WARNING" if result["errors"] else "INFO"
    write_log(
        "geocode",
        f"Theater CSV import: {result['inserted']} inserted, {result['updated']} updated, "
        f"{result['skipped']} skipped, {len(result['errors'])} errors",
        level=level,
    )
    return jsonify(result)


@api_bp.route("/admin/theaters/export/email", methods=["POST"])
@require_role("admin", "editor")
def api_export_theaters_email():
    """Generate theater CSV and email it to the requesting user."""
    from app.notifications import send_email_with_attachment
    from app.venue_crawler import export_theaters_csv

    to_addr = current_user.email
    if not to_addr:
        return jsonify({"status": "error", "message": "Your account has no email address configured"}), 400

    csv_data = export_theaters_csv()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"imax_theaters_{ts}.csv"

    ok, err = send_email_with_attachment(
        app_config=current_app.config,
        to_address=to_addr,
        subject=f"IMAX Alert — Theater Export ({ts})",
        body_html=f"<p>Your theater CSV export is attached as <strong>{filename}</strong>.</p>",
        body_text=f"Your theater CSV export is attached as {filename}.",
        attachment_data=csv_data.encode("utf-8"),
        attachment_filename=filename,
    )
    if ok:
        return jsonify({"status": "ok", "email": to_addr})
    return jsonify({"status": "error", "message": err}), 500


@api_bp.route("/admin/theaters/export/save", methods=["POST"])
@require_role("admin", "editor")
def api_export_theaters_save():
    """Save theater CSV to the server's persistent data directory."""
    import os
    from app.venue_crawler import export_theaters_csv

    db_url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if db_url.startswith("sqlite:///"):
        db_path = db_url[len("sqlite:///"):]
        data_dir = os.path.dirname(os.path.abspath(db_path)) if db_path else "/app/data"
    else:
        data_dir = "/app/data"

    exports_dir = os.path.join(data_dir, "exports")
    try:
        os.makedirs(exports_dir, exist_ok=True)
    except OSError as exc:
        return jsonify({"status": "error", "message": f"Cannot create exports directory: {exc}"}), 500

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"imax_theaters_{ts}.csv"
    filepath = os.path.join(exports_dir, filename)

    csv_data = export_theaters_csv()
    try:
        with open(filepath, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_data)
    except OSError as exc:
        return jsonify({"status": "error", "message": f"Could not write file: {exc}"}), 500

    return jsonify({"status": "ok", "filename": filename, "path": filepath})


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
