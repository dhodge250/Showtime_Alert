"""
Notification system for IMAX Alert.

Sends email and/or SMS alerts when new IMAX showtimes are detected.

Core design: one notification per (user, alert) pair per run, not one per
showtime.  All matching showtimes are grouped into a single email so the
user sees everything in one place with a link for each individual showtime.
"""
import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app import db
from app.models import AlertMovie, AlertPreference, Notification, Showtime, Theater, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email transport
# ---------------------------------------------------------------------------

# Prevents indefinite hangs on bad port/protocol combos
_SMTP_TIMEOUT = 8


def send_email(
    app_config: dict,
    to_address: str,
    subject: str,
    body_html: str,
    body_text: str,
) -> tuple[bool, str]:
    """
    Send an email via SMTP.

    Port 465  → implicit SSL  (smtplib.SMTP_SSL, no STARTTLS call)
    Port 587  → STARTTLS      (smtplib.SMTP + starttls() when MAIL_USE_TLS)
    Other     → follows MAIL_USE_TLS flag

    Returns (success, error_message).
    """
    username = app_config.get("MAIL_USERNAME", "")
    password = app_config.get("MAIL_PASSWORD", "")
    from_address = app_config.get("MAIL_FROM", "noreply@imaxalert.com")

    if not username or not password:
        logger.warning(
            "Email credentials not configured; skipping email to %s",
            to_address,
        )
        return False, "Email credentials not configured"

    host = app_config.get("MAIL_SERVER", "smtp.gmail.com")
    port = int(app_config.get("MAIL_PORT", 587))
    use_tls = app_config.get("MAIL_USE_TLS", True)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
            if use_tls:
                server.starttls()
        server.login(username, password)
        server.sendmail(from_address, to_address, msg.as_string())
        server.quit()
        logger.info("Email sent to %s", to_address)
        return True, ""
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        logger.error("Failed to send email to %s: %s", to_address, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# SMS transport
# ---------------------------------------------------------------------------

def send_sms(
    app_config: dict, to_number: str, message: str
) -> tuple[bool, str]:
    """Send an SMS via Twilio. Returns (success, error_message)."""
    account_sid = app_config.get("TWILIO_ACCOUNT_SID", "")
    auth_token = app_config.get("TWILIO_AUTH_TOKEN", "")
    from_number = app_config.get("TWILIO_FROM_NUMBER", "")

    if not account_sid or not auth_token or not from_number:
        logger.warning(
            "Twilio credentials not configured; skipping SMS to %s", to_number
        )
        return False, "Twilio credentials not configured"

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        sms = client.messages.create(
            body=message, from_=from_number, to=to_number
        )
        logger.info("SMS sent to %s, SID=%s", to_number, sms.sid)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send SMS to %s: %s", to_number, exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Message builders — multi-showtime
# ---------------------------------------------------------------------------

def _build_email_body_multi(
    user: User,
    showtimes: list[Showtime],
) -> tuple[str, str, str]:
    """
    Build (subject, html_body, text_body) for an alert covering one or more showtimes.

    Uses the same info-box style as the original single-showtime email.
    When a group has multiple showtimes the label switches from "Showtime:"
    to "Showtimes:" and each datetime is listed with its own ticket link.
    """
    sorted_sts = sorted(showtimes, key=lambda s: s.show_datetime)

    # Group by (movie_title, theater_name), preserving chronological order
    groups: dict[tuple, list[Showtime]] = {}
    for st in sorted_sts:
        key = (
            st.movie.title if st.movie else "Unknown Movie",
            st.theater.name if st.theater else "Unknown Theater",
        )
        groups.setdefault(key, []).append(st)

    all_titles = list(dict.fromkeys(t for t, _ in groups))
    total = len(sorted_sts)

    # Subject
    if len(all_titles) == 1:
        subject = (
            f"IMAX Alert: {all_titles[0]} — "
            f"{total} showtime{'s' if total != 1 else ''} available!"
        )
    else:
        subject = f"IMAX Alert: {len(all_titles)} movies now available at IMAX!"

    # ── Plain-text body ────────────────────────────────────────────────────
    text_lines = [f"Hi {user.name},", "", "IMAX tickets are now available for:"]
    for (title, theater_name), sts in groups.items():
        fmt = sts[0].format_type or "IMAX"
        text_lines += [
            "",
            f"  Movie:   {title}",
            f"  Theater: {theater_name}",
            f"  Format:  {fmt}",
        ]
        if len(sts) == 1:
            dt = (
                sts[0].show_datetime.strftime("%A, %B %d, %Y at %I:%M %p")
                .replace(" 0", " ")
            )
            text_lines.append(f"  Showtime: {dt}")
            if sts[0].tickets_url:
                text_lines += ["", f"Get your tickets here: {sts[0].tickets_url}"]
        else:
            text_lines.append("  Showtimes:")
            for st in sts:
                dt = (
                    st.show_datetime.strftime("%a, %b %d, %Y at %I:%M %p")
                    .replace(" 0", " ")
                )
                line = f"    {dt}"
                if st.tickets_url:
                    line += f"  —  {st.tickets_url}"
                text_lines.append(line)
    text_lines += [
        "", "Act fast — IMAX showtimes sell out quickly!", "", "IMAX Alert"
    ]
    text_body = "\n".join(text_lines)

    # ── HTML body ──────────────────────────────────────────────────────────
    boxes_html = ""
    for (title, theater_name), sts in groups.items():
        fmt = sts[0].format_type or "IMAX"

        if len(sts) == 1:
            st = sts[0]
            dt = (
                st.show_datetime.strftime("%A, %B %d, %Y at %I:%M %p")
                .replace(" 0", " ").replace("at 0", "at ")
            )
            ticket_btn = (
                f'<a href="{st.tickets_url}" class="btn">Get Tickets Now &rarr;</a>'
                if st.tickets_url else ""
            )
            boxes_html += f"""
    <div class="movie-info">
      <p><span class="label">Movie:</span> {title}</p>
      <p><span class="label">Theater:</span> {theater_name}</p>
      <p><span class="label">Format:</span> {fmt}</p>
      <p><span class="label">Showtime:</span> {dt}</p>
    </div>
    {ticket_btn}"""
        else:
            rows = ""
            for st in sts:
                dt = (
                    st.show_datetime.strftime("%A, %B %d, %Y at %I:%M %p")
                    .replace(" 0", " ").replace("at 0", "at ")
                )
                link = (
                    f'<a href="{st.tickets_url}" class="btn-sm">Get Tickets &rarr;</a>'
                    if st.tickets_url else ""
                )
                rows += f"""
        <tr>
          <td style="padding:7px 0;font-size:15px;border-bottom:1px solid rgba(255,255,255,.07)">{dt}</td>
          <td style="padding:7px 0;text-align:right;border-bottom:1px solid rgba(255,255,255,.07);white-space:nowrap;padding-left:16px">{link}</td>
        </tr>"""
            boxes_html += f"""
    <div class="movie-info">
      <p><span class="label">Movie:</span> {title}</p>
      <p><span class="label">Theater:</span> {theater_name}</p>
      <p><span class="label">Format:</span> {fmt}</p>
      <p><span class="label">Showtimes:</span></p>
      <table style="width:100%;border-collapse:collapse;margin-top:4px">{rows}
      </table>
    </div>"""

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #0d0d0d; color: #f0f0f0; }}
    .container {{ max-width: 600px; margin: 30px auto; background: #1a1a2e; border-radius: 12px; padding: 30px; }}
    h1 {{ color: #00d4ff; font-size: 24px; }}
    .movie-info {{ background: #16213e; border-radius: 8px; padding: 20px; margin: 20px 0; }}
    .movie-info p {{ margin: 8px 0; font-size: 16px; }}
    .label {{ color: #00d4ff; font-weight: bold; }}
    .btn {{ display: inline-block; background: #00d4ff; color: #0d0d0d; padding: 12px 28px;
            border-radius: 6px; text-decoration: none; font-weight: bold; margin-top: 16px; }}
    .btn-sm {{ display: inline-block; background: #00d4ff; color: #0d0d0d; padding: 6px 14px;
               border-radius: 4px; text-decoration: none; font-weight: bold; font-size: 13px; }}
    .footer {{ font-size: 12px; color: #666; margin-top: 24px; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>IMAX Alert: Tickets Available!</h1>
    <p>Hi {user.name},</p>
    <p>IMAX tickets are now available for a movie you're watching:</p>
    {boxes_html}
    <p style="margin-top:20px;">Act fast — IMAX showtimes sell out quickly!</p>
    <div class="footer">You are receiving this because you set up an IMAX Alert.
    Manage your alerts at your IMAX Alert dashboard.</div>
  </div>
</body>
</html>"""

    return subject, html_body, text_body


def _build_sms_body_multi(user: User, showtimes: list[Showtime]) -> str:
    """Build an SMS body for one or more showtimes, grouped by movie."""
    sorted_sts = sorted(showtimes, key=lambda s: s.show_datetime)
    titles = list(
        dict.fromkeys(st.movie.title if st.movie else "Unknown" for st in sorted_sts)
    )
    theater = sorted_sts[0].theater.name if sorted_sts[0].theater else "IMAX"
    count = len(sorted_sts)

    if count == 1:
        st = sorted_sts[0]
        dt_str = st.show_datetime.strftime("%b %d at %I:%M %p").replace(" 0", " ")
        msg = f"IMAX ALERT: {titles[0]} at {theater} on {dt_str}."
        if st.tickets_url:
            msg += f" {st.tickets_url}"
    else:
        title_str = titles[0] if len(titles) == 1 else f"{len(titles)} movies"
        first_url = next((s.tickets_url for s in sorted_sts if s.tickets_url), "")
        msg = (
            f"IMAX ALERT: {count} showtimes for {title_str} at {theater}. "
            "Check email for full list."
        )
        if first_url:
            msg += f" First: {first_url}"

    return msg


# ---------------------------------------------------------------------------
# Legacy single-showtime builders (kept so existing call sites still compile)
# ---------------------------------------------------------------------------

def _build_email_body(user: User, showtime: Showtime) -> tuple[str, str, str]:
    """Single-showtime wrapper around _build_email_body_multi."""
    return _build_email_body_multi(user, [showtime])


def _build_sms_body(user: User, showtime: Showtime) -> str:
    """Single-showtime wrapper around _build_sms_body_multi."""
    return _build_sms_body_multi(user, [showtime])


# ---------------------------------------------------------------------------
# Core notification logic
# ---------------------------------------------------------------------------

def _radius_theater_ids(pref: AlertPreference) -> Optional[set[int]]:
    """
    Return the set of theater IDs within pref.radius_km of the alert owner's
    saved location, or None if the pref is not a radius alert or the user has
    no saved coordinates.  Used to scope both candidate queries and per-showtime
    filtering for radius-based alerts.
    """
    if pref.radius_km is None:
        return None
    user = User.query.get(pref.user_id)
    if user is None or user.location_lat is None or user.location_lon is None:
        return None
    from app.scrapers.base import _haversine_km
    return {
        t.id for t in Theater.query.filter_by(is_active=True).all()
        if t.latitude is not None and t.longitude is not None
        and _haversine_km(user.location_lat, user.location_lon, t.latitude, t.longitude) <= pref.radius_km
    }


def _get_matching_showtimes_for_pref(
    pref: AlertPreference,
    candidates: list[Showtime],
) -> list[Showtime]:
    """
    Filter *candidates* to the showtimes that should trigger *pref*.

    For any-movie alerts: returns candidates matching the theater that haven't
    already been notified, unless a notification cap is set — in that case all
    matching candidates are returned so the alert keeps firing until the cap.
    For specific-movie alerts: returns candidates whose movie_id matches an
    unsent AlertMovie row.  Per-showtime deduplication is skipped when a cap
    is set because the cap itself (notifications_fired >= max_notifications in
    _notify_for_alert) is the correct closure guard; deduplicating by showtime
    ID here would prevent any notification after the first from ever firing.
    """
    # When a target date is set, restrict candidates to that date ± buffer days.
    if pref.target_date:
        from datetime import timedelta
        buffer = pref.target_date_buffer or 0
        date_from = pref.target_date - timedelta(days=buffer)
        date_to   = pref.target_date + timedelta(days=buffer)
        candidates = [
            st for st in candidates
            if date_from <= st.show_datetime.date() <= date_to
        ]

    # Resolve radius theater set once (None when not a radius alert).
    radius_ids: Optional[set[int]] = _radius_theater_ids(pref) if pref.radius_km is not None else None

    def _theater_allowed(st: Showtime) -> bool:
        """Return True if this showtime's theater is in scope for pref."""
        if radius_ids is not None:
            return st.theater_id in radius_ids
        if pref.theater_id is not None:
            return st.theater_id == pref.theater_id
        return True  # any-theater alert

    result: list[Showtime] = []

    if pref.is_any_movie:
        if pref.max_notifications:
            # Cap mode: re-notify on every alert cycle until the cap is reached.
            for st in candidates:
                if _theater_allowed(st):
                    result.append(st)
        else:
            # One-shot mode: skip showtimes already covered by a prior notification.
            notified_ids: set[int] = set()
            for n in Notification.query.filter_by(alert_preference_id=pref.id).all():
                if n.notified_showtime_ids:
                    try:
                        notified_ids.update(json.loads(n.notified_showtime_ids))
                    except (ValueError, TypeError):
                        pass
                elif n.showtime_id is not None:
                    notified_ids.add(n.showtime_id)
            for st in candidates:
                if _theater_allowed(st) and st.id not in notified_ids:
                    result.append(st)
    else:
        unsent_movie_ids: set[int] = {
            am.movie_id
            for am in pref.alert_movies.filter_by(alert_sent=False).all()
        }
        for st in candidates:
            if _theater_allowed(st) and st.movie_id in unsent_movie_ids:
                result.append(st)

    return result


def _notify_for_alert(
    app, pref: AlertPreference, showtimes: list[Showtime]
) -> int:
    """
    Send a single notification covering all *showtimes* for *pref*.

    One email (and/or SMS) is sent per alert, listing every matching showtime
    grouped by movie.  Returns the number of messages dispatched (1 per
    channel, not per showtime).
    """
    if not showtimes:
        return 0

    user: Optional[User] = pref.user
    if not user:
        return 0

    subject, html_body, text_body = _build_email_body_multi(user, showtimes)
    sms_body = _build_sms_body_multi(user, showtimes)

    channel_attempted = False
    sent = 0

    showtime_ids_json = json.dumps([st.id for st in showtimes])

    if user.notify_email and user.email:
        ok, err = send_email(
            app.config, user.email, subject, html_body, text_body
        )
        _record_notification(
            user, pref, "email", text_body, ok, err, showtime_ids_json
        )
        channel_attempted = True
        if ok:
            sent += 1

    if user.notify_sms and user.phone:
        ok, err = send_sms(app.config, user.phone, sms_body)
        _record_notification(
            user, pref, "sms", sms_body, ok, err, showtime_ids_json
        )
        channel_attempted = True
        if ok:
            sent += 1

    if not channel_attempted:
        logger.warning(
            "AlertPreference %d matched %d showtime(s) but user %d has no "
            "notification channel configured — alert NOT marked sent.",
            pref.id,
            len(showtimes),
            user.id,
        )
        return 0

    if not sent:
        # Channels were attempted but every delivery failed — do not advance
        # the fired counter so the alert retries on the next cycle.
        logger.warning(
            "AlertPreference %d: notification attempted but all deliveries "
            "failed — notifications_fired not incremented.",
            pref.id,
        )
        return 0

    # Increment fired counter only when at least one channel delivered.
    pref.notifications_fired = (pref.notifications_fired or 0) + 1

    # Mark specific movies as sent and close pref if all are done.
    # In one-shot mode (no max_notifications) a movie is done after its first
    # notification. When a cap is set the Notification log handles per-showtime
    # deduplication, so movies stay "unsent" until the cap is reached.
    if not pref.is_any_movie and not pref.max_notifications:
        notified_movie_ids = {st.movie_id for st in showtimes}
        for am in pref.alert_movies.all():
            if am.movie_id in notified_movie_ids and not am.alert_sent:
                am.alert_sent = True
                am.alert_sent_at = datetime.now(timezone.utc)
        if pref.alert_movies.filter_by(alert_sent=False).count() == 0:
            pref.alert_sent = True
            pref.alert_sent_at = datetime.now(timezone.utc)

    # Enforce max_notifications cap (applies to both alert types)
    if (
        pref.max_notifications
        and pref.notifications_fired >= pref.max_notifications
    ):
        pref.is_active = False
        pref.alert_sent = True
        pref.alert_sent_at = datetime.now(timezone.utc)
        logger.info(
            "AlertPreference %d reached max_notifications=%d — auto-closing.",
            pref.id,
            pref.max_notifications,
        )

    db.session.commit()
    return sent


def _notify_for_showtime(app, showtime: Showtime) -> int:
    """
    Backward-compatible wrapper: find all alerts matching a single showtime
    and call _notify_for_alert for each.
    """
    prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()
    sent = 0
    for pref in prefs:
        matching = _get_matching_showtimes_for_pref(pref, [showtime])
        if matching:
            sent += _notify_for_alert(app, pref, matching)
    return sent


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_new_showtimes(app, new_showtimes: list[Showtime]) -> int:
    """
    Group new showtimes by matching alert and send one notification per alert.

    Called immediately after a scraper run with only the newly-inserted rows.
    Each user receives at most one email per alert per scraper run, listing
    all new showtimes for that alert together.
    """
    if not new_showtimes:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future_showtimes = [st for st in new_showtimes if st.show_datetime >= now]
    if not future_showtimes:
        return 0

    prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()
    sent = 0
    for pref in prefs:
        matching = _get_matching_showtimes_for_pref(pref, future_showtimes)
        if matching:
            sent += _notify_for_alert(app, pref, matching)
    return sent


def process_pending_alerts(app) -> int:
    """
    Process all active alerts against all existing showtimes in the DB.

    Works from the alert side so a reset alert fires even when the scraper
    found nothing new.  Each alert receives one consolidated notification
    listing all matching showtimes.
    """
    prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()
    if not prefs:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sent = 0
    for pref in prefs:
        q = Showtime.query.filter(Showtime.show_datetime >= now)
        if pref.radius_km is not None:
            nearby = _radius_theater_ids(pref)
            if not nearby:
                continue
            q = q.filter(Showtime.theater_id.in_(nearby))
        elif pref.theater_id:
            q = q.filter_by(theater_id=pref.theater_id)
        if not pref.is_any_movie:
            unsent_ids = [
                am.movie_id
                for am in pref.alert_movies.filter_by(alert_sent=False).all()
            ]
            if not unsent_ids:
                continue
            q = q.filter(Showtime.movie_id.in_(unsent_ids))
        candidates = q.all()
        matching = _get_matching_showtimes_for_pref(pref, candidates)
        if matching:
            sent += _notify_for_alert(app, pref, matching)
    return sent


# ---------------------------------------------------------------------------
# Record-keeping
# ---------------------------------------------------------------------------

def _record_notification(
    user: User,
    pref: AlertPreference,
    method: str,
    message: str,
    success: bool,
    error: str,
    notified_showtime_ids: Optional[str] = None,
) -> None:
    """Persist a Notification row recording the outcome of a send attempt."""
    db.session.add(Notification(
        user=user,
        alert_preference_id=pref.id,
        notified_showtime_ids=notified_showtime_ids,
        method=method,
        message=message,
        success=success,
        error_message=error or None,
    ))
    from app.log_utils import write_log
    log_level = "INFO" if success else "ERROR"
    log_msg = f"Notification {'sent' if success else 'failed'} via {method} to {user.email} (alert #{pref.id})"
    write_log("notification", log_msg, level=log_level, user_id=user.id,
              details={"pref_id": pref.id, "method": method, "error": error or None})
