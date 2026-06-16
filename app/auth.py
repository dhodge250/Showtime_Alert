"""Authentication blueprint for IMAX Alert."""
import functools
import logging
import re

from flask import (
    Blueprint,
    abort,
    current_app,
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

# ---------------------------------------------------------------------------
# Password strength rules (issue #24)
# ---------------------------------------------------------------------------

_PW_MIN_LEN = 8
_PW_RULES = [
    (r"[A-Z]", "at least one uppercase letter"),
    (r"[a-z]", "at least one lowercase letter"),
    (r"[0-9]", "at least one number"),
    (r"[^A-Za-z0-9]", "at least one special character"),
]


def validate_password_strength(password: str, current_hash: str | None = None) -> str | None:
    """
    Return an error message if *password* fails complexity requirements, or None if it passes.

    Checks: min length, uppercase, lowercase, digit, special char, and optionally
    that it does not match the user's current password.
    """
    from werkzeug.security import check_password_hash

    if len(password) < _PW_MIN_LEN:
        return f"Password must be at least {_PW_MIN_LEN} characters."
    for pattern, description in _PW_RULES:
        if not re.search(pattern, password):
            return f"Password must contain {description}."
    if current_hash and check_password_hash(current_hash, password):
        return "New password must be different from your current password."
    return None


# ---------------------------------------------------------------------------
# Before-request hook
# ---------------------------------------------------------------------------

@auth_bp.before_app_request
def enforce_password_change():
    """Redirect users with force_password_change=True to the change-password page."""
    if not current_user.is_authenticated:
        return
    if not current_user.force_password_change:
        return
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
    if not current_user.force_password_change:
        return redirect(url_for("main.index"))

    error = None
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        error = validate_password_strength(new_password, current_user.password_hash)
        if error is None and new_password != confirm:
            error = "Passwords do not match."
        if error is None:
            current_user.set_password(new_password)
            current_user.force_password_change = False
            db.session.commit()
            logger.info("User %s completed forced password change.", current_user.email)
            return redirect(url_for("main.index"))

    return render_template("change_password.html", error=error)


# ---------------------------------------------------------------------------
# Forgot / reset password (issue #22)
# ---------------------------------------------------------------------------

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute", error_message="Too many requests. Please wait a minute and try again.")
def forgot_password():
    """Send a password-reset email to the user's registered address."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    sent = False
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        from app.models import User
        user = User.query.filter(
            db.func.lower(User.email) == email
        ).first()

        if user and user.is_active:
            raw_token = user.generate_reset_token(expiry_hours=1)
            db.session.commit()
            _send_reset_email(user, raw_token)
            logger.info("Password reset email sent to %s", user.email)
            from app.log_utils import write_log
            write_log("auth", f"Password reset requested: {user.email}", user_id=user.id)

        # Always show the same confirmation to prevent email enumeration
        sent = True

    return render_template("forgot_password.html", sent=sent)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute", error_message="Too many attempts. Please wait a minute and try again.")
def reset_password(token: str):
    """Allow a user to set a new password using a valid reset token."""
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    from app.models import User
    user = _find_user_by_token(token)

    if user is None:
        return render_template("reset_password.html", invalid=True, token=token)

    error = None
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        error = validate_password_strength(new_password, current_hash=user.password_hash)
        if error is None and new_password != confirm:
            error = "Passwords do not match."
        if error is None:
            user.set_password(new_password)
            user.clear_reset_token()
            user.force_password_change = False
            db.session.commit()
            logger.info("User %s completed password reset.", user.email)
            from app.log_utils import write_log
            write_log("auth", f"Password reset completed: {user.email}", user_id=user.id)
            flash("Your password has been reset. Please sign in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("reset_password.html", invalid=False, token=token, error=error)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_user_by_token(raw_token: str):
    """Return the User whose stored reset token matches *raw_token*, or None."""
    from app.models import User
    from datetime import datetime, timezone
    now_naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    candidates = User.query.filter(
        User.reset_token.isnot(None),
        User.reset_token_expiry > now_naive_utc,
    ).all()
    for user in candidates:
        if user.verify_reset_token(raw_token):
            return user
    return None


def _send_reset_email(user, raw_token: str):
    """Dispatch a password-reset email using the app's configured SMTP settings."""
    from app.notifications import send_email
    reset_url = url_for("auth.reset_password", token=raw_token, _external=True)
    subject = "Reset your IMAX Alert password"
    body_html = f"""
<p>Hi {user.name},</p>
<p>We received a request to reset your IMAX Alert password. Click the link below to choose a new password:</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email.</p>
<p>— IMAX Alert</p>
"""
    body_text = (
        f"Hi {user.name},\n\n"
        f"We received a request to reset your IMAX Alert password.\n\n"
        f"Reset your password here:\n{reset_url}\n\n"
        f"This link expires in 1 hour. If you did not request a password reset, ignore this email.\n\n"
        f"— IMAX Alert"
    )
    success, err = send_email(
        current_app.config, user.email, subject, body_html, body_text
    )
    if not success:
        logger.warning("Failed to send reset email to %s: %s", user.email, err)
