"""
Scraper health check utilities.

run_health_check() executes a lightweight test scrape for one theater of the
given chain, classifies the result, and persists a ScraperStatus row.

The caller is responsible for providing an active Flask app context.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Return (error_class, human-readable summary) for a caught exception."""
    import requests as req

    exc_type = type(exc).__name__
    msg = str(exc)

    if isinstance(exc, req.exceptions.ConnectTimeout):
        return exc_type, "Connection timed out — website may be down or blocking requests"
    if isinstance(exc, req.exceptions.ReadTimeout):
        return exc_type, "Read timed out — website is responding slowly or blocking requests"
    if isinstance(exc, req.exceptions.ConnectionError):
        return exc_type, "Connection error — website may be down or unreachable"
    if isinstance(exc, req.exceptions.HTTPError):
        try:
            code = exc.response.status_code
            if 400 <= code < 500:
                return exc_type, f"HTTP {code}: theater website returned a client error"
            if 500 <= code < 600:
                return exc_type, f"HTTP {code}: theater website is returning server errors"
        except Exception:
            pass
        return exc_type, f"HTTP error: {msg[:120]}"

    # Playwright / browser errors
    if "playwright" in exc_type.lower() or "playwright" in msg.lower():
        return exc_type, "Browser automation failed — Cloudflare or JS challenge may have changed"

    # Generic parse / selector issues
    if "AttributeError" in exc_type or "KeyError" in exc_type or "IndexError" in exc_type:
        return exc_type, f"Page structure error — HTML selectors may need updating ({exc_type})"

    first_line = msg.split("\n")[0][:200]
    return exc_type, f"{exc_type}: {first_line}"


def run_health_check(scraper) -> dict:
    """
    Run a lightweight health check for one scraper chain.

    Picks one active theater for the chain, calls scrape_theater(), and
    writes a ScraperStatus row.  Returns a summary dict.

    Requires an active Flask app context from the caller.
    """
    from app import db
    from app.models import ScraperStatus, Theater

    chain_name = scraper.chain_name
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    health_website = getattr(scraper, "health_website", None)
    theater_q = Theater.query.filter(
        Theater.is_active == True,  # noqa: E712
        Theater.chain == chain_name,
    )
    if health_website:
        theater_q = theater_q.filter(Theater.website.contains(health_website))
    theater = theater_q.first()

    theater_count = Theater.query.filter(
        Theater.is_active == True,  # noqa: E712
        Theater.chain == chain_name,
    ).count()

    status = "error"
    error_class = None
    error_summary = None
    showtime_count = None

    if theater is None:
        error_class = "NoTheater"
        error_summary = "No active theaters configured for this chain"
    else:
        # Wrap the scrape in a savepoint so nothing persists to the DB.
        # If scrape_theater() calls db.session.commit() internally (Regal does),
        # that only releases the savepoint into the outer transaction; the
        # db.session.rollback() in the finally block discards all of it.
        db.session.begin_nested()
        try:
            found_showtimes = scraper.scrape_theater(theater, {None})
            showtime_count = len(found_showtimes)
            if showtime_count > 0:
                status = "ok"
            else:
                status = "warning"
                error_summary = "Scraper ran successfully but found no showtimes — page structure may have changed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Health check failed for %s: %s", chain_name, exc)
            error_class, error_summary = _classify_error(exc)
        finally:
            db.session.rollback()  # discard all writes from the probe scrape

    row = ScraperStatus(
        chain_name=chain_name,
        checked_at=now,
        status=status,
        theater_count=theater_count,
        showtime_count=showtime_count,
        error_class=error_class,
        error_summary=error_summary,
    )
    db.session.add(row)
    db.session.commit()

    return {
        "chain_name": chain_name,
        "status": status,
        "theater_count": theater_count,
        "showtime_count": showtime_count,
        "error_class": error_class,
        "error_summary": error_summary,
    }
