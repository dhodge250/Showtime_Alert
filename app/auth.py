"""Authentication blueprint for IMAX Alert."""
import functools
import logging

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app import db, limiter

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


@auth_bp.before_app_request
def enforce_password_change():
    """Redirect users with force_password_change=True to the change-password page."""
    if not current_user.is_authenticated:
        return
    if not current_user.force_password_change:
        return
    # Allow the change-password page and logout through; block everything else
    allowed = {"auth.change_password", "auth.logout", "static"}
    if request.endpoint and request.endpoint not in allowed:
        return redirect(url_for("auth.change_password"))


# ---------------------------------------------------------------------------
# Role-based access decorator
# ---------------------------------------------------------------------------

def require_role(*roles):
    """
    Decorator that combines @login_required with a role check.
    Usage:
        @require_role("admin")
        @require_role("admin", "editor")
    """
    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role_name not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", error_message="Too many login attempts. Please wait a minute and try again.")
def login():
    """Login page."""
    from app.models import User

    # Already logged in
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "on"

        user = User.query.filter_by(email=email).first()
        if user and user.is_active and user.check_password(password):
            login_user(user, remember=remember)
            logger.info("User %s logged in.", user.email)
            from app.log_utils import write_log
            write_log("auth", f"Login: {user.email}", user_id=user.id)
            if user.force_password_change:
                return redirect(url_for("auth.change_password"))
            next_page = request.args.get("next")
            # Safety: only redirect to relative paths
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("main.index"))
        else:
            error = "Invalid credentials or account is disabled."
            logger.warning("Failed login attempt for email=%r", email)
            from app.log_utils import write_log
            write_log("auth", f"Failed login attempt: {email}", level="WARNING")

    return render_template("login.html", error=error)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Log out the current user."""
    logger.info("User %s logged out.", current_user.email)
    from app.log_utils import write_log
    write_log("auth", f"Logout: {current_user.email}", user_id=current_user.id)
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Forced password change — required before accessing the app for first-time accounts."""
    # If the user doesn't need to change their password, redirect away
    if not current_user.force_password_change:
        return redirect(url_for("main.index"))

    error = None
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            error = "Password must be at least 8 characters."
        elif new_password != confirm:
            error = "Passwords do not match."
        else:
            current_user.set_password(new_password)
            current_user.force_password_change = False
            db.session.commit()
            logger.info("User %s completed forced password change.", current_user.email)
            return redirect(url_for("main.index"))

    return render_template("change_password.html", error=error)
