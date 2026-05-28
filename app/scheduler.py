"""
Scheduler for periodic IMAX showtime scraping, venue list crawling,
alert processing, and expired showtime cleanup.

Uses APScheduler to run four jobs on independent schedules:
  - Showtime scraper:  every N minutes (default 30)
  - Alert processor:   every N minutes (default 15)
  - Venue crawler:     every N days    (default 7)
  - Showtime cleanup:  every N hours   (default 24)
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _scrape_job(app):
    """Scheduled job: run all scrapers and dispatch notifications."""
    from app.notifications import process_new_showtimes
    from app.scraper import run_all_scrapers

    with app.app_context():
        logger.info("Scheduled scrape starting...")
        new_showtimes = run_all_scrapers()
        logger.info("Scrape complete. %d new showtimes found.", len(new_showtimes))

        if new_showtimes:
            sent = process_new_showtimes(app, new_showtimes)
            logger.info("Sent %d notifications.", sent)


def _venue_crawl_job(app):
    """Scheduled job: crawl IMAX venue list and refresh theater DB rows."""
    from app.venue_crawler import run_venue_crawl

    with app.app_context():
        logger.info("Scheduled venue crawl starting...")
        summary = run_venue_crawl()
        logger.info(
            "Venue crawl complete: %d venues found, %d inserted, %d updated, "
            "%d geocoded, %d geocode failures, %d errors",
            summary["venues_found"],
            summary["inserted"],
            summary["updated"],
            summary["geocoded"],
            summary["geocode_failed"],
            len(summary["errors"]),
        )
        if summary["errors"]:
            for err in summary["errors"]:
                logger.warning("Venue crawl error: %s", err)


def _alert_job(app):
    """Scheduled job: process pending alerts against existing showtimes."""
    from app.notifications import process_pending_alerts

    with app.app_context():
        logger.info("Alert processor starting...")
        sent = process_pending_alerts(app)
        if sent:
            logger.info(
                "Alert processor complete. %d notification(s) sent.", sent
            )
        else:
            logger.debug("Alert processor complete. No notifications to send.")


def _cleanup_job(app):
    """Scheduled job: delete expired showtimes then orphaned movies."""
    from app.scraper import cleanup_expired_showtimes, cleanup_orphaned_movies

    with app.app_context():
        count = cleanup_expired_showtimes()
        orphaned = cleanup_orphaned_movies()
        logger.info(
            "Cleanup job complete. %d expired showtime(s) removed, "
            "%d orphaned movie(s) removed.",
            count,
            orphaned,
        )


def start_scheduler(app) -> None:
    """Start the background scheduler with scrape, venue crawl, and cleanup jobs."""
    global _scheduler  # noqa: PLW0603

    if _scheduler and _scheduler.running:
        logger.warning("Scheduler already running; skipping start.")
        return

    # Read intervals from the Settings table so admin-saved values survive
    # restarts. Falls back to app.config (env var) if the table isn't
    # populated yet.
    def _setting_int(key: str, config_key: str, default: int) -> int:
        """Read an integer scheduler setting from the DB, with a config fallback."""
        try:
            from app.models import Settings
            with app.app_context():
                s = Settings.query.filter_by(key=key).first()
                return (
                    int(s.value) if s and s.value
                    else app.config.get(config_key, default)
                )
        except Exception:  # noqa: BLE001
            return app.config.get(config_key, default)

    interval_minutes = _setting_int(
        "scraper_interval_minutes", "SCRAPER_INTERVAL_MINUTES", 30
    )
    alert_minutes = _setting_int(
        "alert_interval_minutes", "ALERT_INTERVAL_MINUTES", 15
    )
    venue_crawl_days = _setting_int(
        "venue_crawl_interval_days", "VENUE_CRAWL_INTERVAL_DAYS", 7
    )
    cleanup_hours = _setting_int(
        "cleanup_interval_hours", "CLEANUP_INTERVAL_HOURS", 24
    )

    _scheduler = BackgroundScheduler()

    # Showtime scraper — runs every N minutes
    _scheduler.add_job(
        func=lambda: _scrape_job(app),
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="imax_scrape",
        name="Showtime scraper",
        replace_existing=True,
    )

    # Alert processor — runs every N minutes, independent of the scraper
    _scheduler.add_job(
        func=lambda: _alert_job(app),
        trigger=IntervalTrigger(minutes=alert_minutes),
        id="imax_alerts",
        name="Alert processor",
        replace_existing=True,
    )

    # Venue crawler — runs every N days
    _scheduler.add_job(
        func=lambda: _venue_crawl_job(app),
        trigger=IntervalTrigger(days=venue_crawl_days),
        id="imax_venue_crawl",
        name="Venue crawler",
        replace_existing=True,
    )

    # Expired showtime cleanup — runs every N hours
    _scheduler.add_job(
        func=lambda: _cleanup_job(app),
        trigger=IntervalTrigger(hours=cleanup_hours),
        id="imax_cleanup",
        name="Showtime cleanup",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started; scraping every %d min, alerts every %d min, "
        "venue crawl every %d days, cleanup every %d hours.",
        interval_minutes,
        alert_minutes,
        venue_crawl_days,
        cleanup_hours,
    )


def stop_scheduler() -> None:
    """Stop the background scheduler if it is running."""
    global _scheduler  # noqa: PLW0603

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
    _scheduler = None


def get_scheduler_status() -> dict:
    """Return a dict describing running state and next-run times for all jobs."""
    if not _scheduler:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            # ISO string with timezone offset — browser JS converts to local time.
            "next_run_iso": next_run.isoformat() if next_run else None,
        })

    return {"running": _scheduler.running, "jobs": jobs}


def reschedule_jobs(
    scraper_minutes: int,
    crawl_days: int,
    cleanup_hours: int = 24,
    alert_minutes: int = 15,
) -> None:
    """
    Update trigger intervals for all scheduled jobs without restarting.

    Safe to call at any time after ``start_scheduler()``. All four job
    intervals (scraper, alerts, venue crawl, cleanup) are updated atomically.
    """
    if not _scheduler or not _scheduler.running:
        logger.warning(
            "reschedule_jobs called but scheduler is not running; ignoring."
        )
        return

    _scheduler.reschedule_job(
        "imax_scrape", trigger=IntervalTrigger(minutes=scraper_minutes)
    )
    _scheduler.reschedule_job(
        "imax_alerts", trigger=IntervalTrigger(minutes=alert_minutes)
    )
    _scheduler.reschedule_job(
        "imax_venue_crawl", trigger=IntervalTrigger(days=crawl_days)
    )
    _scheduler.reschedule_job(
        "imax_cleanup", trigger=IntervalTrigger(hours=cleanup_hours)
    )
    logger.info(
        "Jobs rescheduled: scraper every %d min, alerts every %d min, "
        "venue crawl every %d days, cleanup every %d hours.",
        scraper_minutes,
        alert_minutes,
        crawl_days,
        cleanup_hours,
    )
