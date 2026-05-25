"""Flask routes for IMAX Alert application."""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from app import db
from app.models import AlertPreference, Movie, Notification, Showtime, Theater, User

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)
api_bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# UI Routes
# ---------------------------------------------------------------------------

@main_bp.route("/")
def index():
    """Dashboard: overview of theaters, active alerts, recent showtimes."""
    theaters = Theater.query.filter_by(is_active=True).all()
    movies = Movie.query.order_by(Movie.title).all()
    recent_showtimes = (
        Showtime.query.filter(Showtime.tickets_available.is_(True))
        .order_by(Showtime.first_seen.desc())
        .limit(20)
        .all()
    )
    alerts = AlertPreference.query.filter_by(is_active=True).all()
    users = User.query.all()
    return render_template(
        "index.html",
        theaters=theaters,
        movies=movies,
        recent_showtimes=recent_showtimes,
        alerts=alerts,
        users=users,
    )


@main_bp.route("/theaters")
def theaters():
    """Theater listing with map."""
    theaters_list = Theater.query.filter_by(is_active=True).all()
    theaters_json = [t.to_dict() for t in theaters_list]
    return render_template("theaters.html", theaters=theaters_list, theaters_json=theaters_json)


@main_bp.route("/theaters/<int:theater_id>")
def theater_detail(theater_id):
    """Theater detail page."""
    theater = Theater.query.get_or_404(theater_id)
    showtimes = (
        Showtime.query.filter_by(theater_id=theater_id)
        .filter(Showtime.show_datetime >= datetime.now(timezone.utc))
        .order_by(Showtime.show_datetime)
        .all()
    )
    return render_template("theater_detail.html", theater=theater, showtimes=showtimes)


@main_bp.route("/alerts")
def alerts():
    """Alert management page."""
    theaters_list = Theater.query.filter_by(is_active=True).all()
    movies_list = Movie.query.order_by(Movie.title).all()
    users_list = User.query.all()
    prefs = AlertPreference.query.order_by(AlertPreference.created_at.desc()).all()
    return render_template(
        "alerts.html",
        theaters=theaters_list,
        movies=movies_list,
        users=users_list,
        preferences=prefs,
    )


@main_bp.route("/profile", methods=["GET", "POST"])
def profile():
    """User profile / settings page."""
    if request.method == "POST":
        user_id = request.form.get("user_id")
        if user_id:
            user = User.query.get(user_id)
        else:
            user = None

        if not user:
            user = User(name=request.form.get("name", "User"))
            db.session.add(user)

        user.name = request.form.get("name", user.name)
        user.email = request.form.get("email", user.email)
        user.phone = request.form.get("phone", user.phone)
        user.notify_email = request.form.get("notify_email") == "on"
        user.notify_sms = request.form.get("notify_sms") == "on"
        location_lat = request.form.get("location_lat")
        location_lon = request.form.get("location_lon")
        if location_lat:
            user.location_lat = float(location_lat)
        if location_lon:
            user.location_lon = float(location_lon)
        user.location_name = request.form.get("location_name", user.location_name)
        db.session.commit()
        return render_template("profile.html", user=user, saved=True)

    user = User.query.first()
    return render_template("profile.html", user=user, saved=False)


# ---------------------------------------------------------------------------
# API: Theaters
# ---------------------------------------------------------------------------

@api_bp.route("/theaters")
def api_theaters():
    theaters = Theater.query.filter_by(is_active=True).all()
    return jsonify([t.to_dict() for t in theaters])


@api_bp.route("/theaters/<int:theater_id>")
def api_theater(theater_id):
    theater = Theater.query.get_or_404(theater_id)
    return jsonify(theater.to_dict())


# ---------------------------------------------------------------------------
# API: Movies
# ---------------------------------------------------------------------------

@api_bp.route("/movies")
def api_movies():
    movies = Movie.query.order_by(Movie.title).all()
    return jsonify([m.to_dict() for m in movies])


# ---------------------------------------------------------------------------
# API: Showtimes
# ---------------------------------------------------------------------------

@api_bp.route("/showtimes")
def api_showtimes():
    theater_id = request.args.get("theater_id", type=int)
    movie_id = request.args.get("movie_id", type=int)
    query = Showtime.query.filter(
        Showtime.show_datetime >= datetime.now(timezone.utc)
    )
    if theater_id:
        query = query.filter_by(theater_id=theater_id)
    if movie_id:
        query = query.filter_by(movie_id=movie_id)
    showtimes = query.order_by(Showtime.show_datetime).all()
    return jsonify([s.to_dict() for s in showtimes])


# ---------------------------------------------------------------------------
# API: Users
# ---------------------------------------------------------------------------

@api_bp.route("/users", methods=["GET"])
def api_users():
    users = User.query.all()
    return jsonify([u.to_dict() for u in users])


@api_bp.route("/users", methods=["POST"])
def api_create_user():
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
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201


@api_bp.route("/users/<int:user_id>", methods=["PUT"])
def api_update_user(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json(force=True)
    for field in ("name", "email", "phone", "location_lat", "location_lon",
                  "location_name", "notify_email", "notify_sms"):
        if field in data:
            setattr(user, field, data[field])
    db.session.commit()
    return jsonify(user.to_dict())


# ---------------------------------------------------------------------------
# API: Alert Preferences
# ---------------------------------------------------------------------------

@api_bp.route("/alerts", methods=["GET"])
def api_alerts():
    prefs = AlertPreference.query.order_by(AlertPreference.created_at.desc()).all()
    return jsonify([p.to_dict() for p in prefs])


@api_bp.route("/alerts", methods=["POST"])
def api_create_alert():
    data = request.get_json(force=True)
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id is required"}), 400

    user = User.query.get(data["user_id"])
    if not user:
        return jsonify({"error": "user not found"}), 404

    # Check for duplicate active alert
    theater_id = data.get("theater_id")
    movie_id = data.get("movie_id")
    existing = AlertPreference.query.filter_by(
        user_id=user.id,
        theater_id=theater_id,
        movie_id=movie_id,
        is_active=True,
        alert_sent=False,
    ).first()
    if existing:
        return jsonify({"error": "alert already exists", "alert": existing.to_dict()}), 409

    pref = AlertPreference(
        user_id=user.id,
        theater_id=theater_id,
        movie_id=movie_id,
    )
    db.session.add(pref)
    db.session.commit()
    return jsonify(pref.to_dict()), 201


@api_bp.route("/alerts/<int:pref_id>", methods=["DELETE"])
def api_delete_alert(pref_id):
    pref = AlertPreference.query.get_or_404(pref_id)
    pref.is_active = False
    db.session.commit()
    return jsonify({"deleted": True, "id": pref_id})


# ---------------------------------------------------------------------------
# API: Scheduler
# ---------------------------------------------------------------------------

@api_bp.route("/scheduler/status")
def api_scheduler_status():
    from app.scheduler import get_scheduler_status
    return jsonify(get_scheduler_status())


@api_bp.route("/scheduler/trigger", methods=["POST"])
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
        return jsonify({"status": "error", "message": "Scrape failed; check server logs for details."}), 500


# ---------------------------------------------------------------------------
# API: Notifications log
# ---------------------------------------------------------------------------

@api_bp.route("/notifications")
def api_notifications():
    notifs = Notification.query.order_by(Notification.sent_at.desc()).limit(50).all()
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
