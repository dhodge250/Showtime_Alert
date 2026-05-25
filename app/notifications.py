"""
Notification system for IMAX Alert.

Sends email and/or SMS alerts when new IMAX showtimes are detected.
"""
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app import db
from app.models import AlertPreference, Notification, Showtime, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(
    app_config: dict,
    to_address: str,
    subject: str,
    body_html: str,
    body_text: str,
) -> tuple[bool, str]:
    """
    Send an email via SMTP.

    Returns (success, error_message).
    """
    username = app_config.get("MAIL_USERNAME", "")
    password = app_config.get("MAIL_PASSWORD", "")
    from_address = app_config.get("MAIL_FROM", "noreply@imaxalert.com")

    if not username or not password:
        logger.warning("Email credentials not configured; skipping email to %s", to_address)
        return False, "Email credentials not configured"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        server = smtplib.SMTP(
            app_config.get("MAIL_SERVER", "smtp.gmail.com"),
            int(app_config.get("MAIL_PORT", 587)),
        )
        if app_config.get("MAIL_USE_TLS", True):
            server.starttls()
        server.login(username, password)
        server.sendmail(from_address, to_address, msg.as_string())
        server.quit()
        logger.info("Email sent to %s", to_address)
        return True, ""
    except smtplib.SMTPException as exc:
        logger.error("Failed to send email to %s: %s", to_address, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# SMS (Twilio)
# ---------------------------------------------------------------------------

def send_sms(app_config: dict, to_number: str, message: str) -> tuple[bool, str]:
    """
    Send an SMS via Twilio.

    Returns (success, error_message).
    """
    account_sid = app_config.get("TWILIO_ACCOUNT_SID", "")
    auth_token = app_config.get("TWILIO_AUTH_TOKEN", "")
    from_number = app_config.get("TWILIO_FROM_NUMBER", "")

    if not account_sid or not auth_token or not from_number:
        logger.warning("Twilio credentials not configured; skipping SMS to %s", to_number)
        return False, "Twilio credentials not configured"

    try:
        from twilio.rest import Client

        client = Client(account_sid, auth_token)
        sms = client.messages.create(body=message, from_=from_number, to=to_number)
        logger.info("SMS sent to %s, SID=%s", to_number, sms.sid)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send SMS to %s: %s", to_number, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_email_body(user: User, showtime: Showtime) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for a new-showtime alert."""
    movie_title = showtime.movie.title if showtime.movie else "Unknown Movie"
    theater_name = showtime.theater.name if showtime.theater else "Unknown Theater"
    show_dt_str = showtime.show_datetime.strftime("%A, %B %d, %Y at %I:%M %p")
    format_type = showtime.format_type or "IMAX"
    tickets_url = showtime.tickets_url or ""

    subject = f"🎬 IMAX Alert: {movie_title} tickets now available!"

    text_body = (
        f"Hi {user.name},\n\n"
        f"IMAX tickets are now available for:\n\n"
        f"  Movie:   {movie_title}\n"
        f"  Theater: {theater_name}\n"
        f"  Format:  {format_type}\n"
        f"  Date:    {show_dt_str}\n\n"
    )
    if tickets_url:
        text_body += f"Get your tickets here: {tickets_url}\n\n"
    text_body += "Act fast — IMAX showtimes sell out quickly!\n\nIMAX Alert"

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #0d0d0d; color: #f0f0f0; }}
    .container {{ max-width: 600px; margin: 30px auto; background: #1a1a2e; border-radius: 12px;
                 padding: 30px; }}
    h1 {{ color: #00d4ff; font-size: 24px; }}
    .movie-info {{ background: #16213e; border-radius: 8px; padding: 20px; margin: 20px 0; }}
    .movie-info p {{ margin: 8px 0; font-size: 16px; }}
    .label {{ color: #00d4ff; font-weight: bold; }}
    .btn {{ display: inline-block; background: #00d4ff; color: #0d0d0d; padding: 12px 28px;
            border-radius: 6px; text-decoration: none; font-weight: bold; margin-top: 16px; }}
    .footer {{ font-size: 12px; color: #666; margin-top: 24px; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>🎬 IMAX Alert: Tickets Available!</h1>
    <p>Hi {user.name},</p>
    <p>IMAX tickets are now available for a movie you're watching:</p>
    <div class="movie-info">
      <p><span class="label">Movie:</span> {movie_title}</p>
      <p><span class="label">Theater:</span> {theater_name}</p>
      <p><span class="label">Format:</span> {format_type}</p>
      <p><span class="label">Showtime:</span> {show_dt_str}</p>
    </div>
    {'<a href="' + tickets_url + '" class="btn">Get Tickets Now →</a>' if tickets_url else ''}
    <p style="margin-top:20px;">Act fast — IMAX showtimes sell out quickly!</p>
    <div class="footer">You are receiving this because you set up an IMAX Alert.
    Manage your alerts at your IMAX Alert dashboard.</div>
  </div>
</body>
</html>
"""
    return subject, html_body, text_body


def _build_sms_body(user: User, showtime: Showtime) -> str:
    """Return SMS text for a new-showtime alert."""
    movie_title = showtime.movie.title if showtime.movie else "Unknown Movie"
    theater_name = showtime.theater.name if showtime.theater else "Unknown Theater"
    show_dt_str = showtime.show_datetime.strftime("%b %d at %I:%M %p")
    tickets_url = showtime.tickets_url or ""
    msg = (
        f"IMAX ALERT: {movie_title} tickets available at {theater_name} "
        f"on {show_dt_str}."
    )
    if tickets_url:
        msg += f" {tickets_url}"
    return msg


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_new_showtimes(app, new_showtimes: list[Showtime]) -> int:
    """
    Check alert preferences against new showtimes and send notifications.

    Returns the number of notifications sent.
    """
    sent_count = 0
    with app.app_context():
        for showtime in new_showtimes:
            sent_count += _notify_for_showtime(app, showtime)
    return sent_count


def _notify_for_showtime(app, showtime: Showtime) -> int:
    """Send alerts for a single new showtime. Returns count of messages sent."""
    sent = 0
    prefs = AlertPreference.query.filter_by(
        is_active=True,
        alert_sent=False,
    ).all()

    for pref in prefs:
        # Match: theater must match (if specified) AND movie must match (if specified)
        theater_match = pref.theater_id is None or pref.theater_id == showtime.theater_id
        movie_match = pref.movie_id is None or pref.movie_id == showtime.movie_id
        if not (theater_match and movie_match):
            continue

        user: Optional[User] = pref.user
        if not user:
            continue

        subject, html_body, text_body = _build_email_body(user, showtime)
        sms_body = _build_sms_body(user, showtime)

        email_ok = False
        sms_ok = False

        if user.notify_email and user.email:
            ok, err = send_email(app.config, user.email, subject, html_body, text_body)
            _record_notification(user, pref, showtime, "email", text_body, ok, err)
            email_ok = ok
            sent += 1

        if user.notify_sms and user.phone:
            ok, err = send_sms(app.config, user.phone, sms_body)
            _record_notification(user, pref, showtime, "sms", sms_body, ok, err)
            sms_ok = ok
            sent += 1

        # Mark alert as sent once we have attempted to notify (prevents repeated alerts)
        attempted = (user.notify_email and user.email) or (user.notify_sms and user.phone)
        if attempted:
            pref.alert_sent = True
            pref.alert_sent_at = datetime.now(timezone.utc)

    db.session.commit()
    return sent


def _record_notification(
    user: User,
    pref: AlertPreference,
    showtime: Showtime,
    method: str,
    message: str,
    success: bool,
    error: str,
) -> None:
    notif = Notification(
        user=user,
        alert_preference_id=pref.id,
        showtime_id=showtime.id,
        method=method,
        message=message,
        success=success,
        error_message=error or None,
    )
    db.session.add(notif)
