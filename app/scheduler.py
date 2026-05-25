"""
Scheduler for periodic IMAX showtime scraping.

Uses APScheduler to run the scraper on a configurable interval.
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


def start_scheduler(app) -> None:
    """Start the background scheduler."""
    global _scheduler  # noqa: PLW0603

    if _scheduler and _scheduler.running:
        logger.warning("Scheduler already running; skipping start.")
        return

    interval_minutes = app.config.get("SCRAPER_INTERVAL_MINUTES", 30)

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        func=lambda: _scrape_job(app),
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="imax_scrape",
        name="IMAX showtime scraper",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started; scraping every %d minutes.", interval_minutes)


def stop_scheduler() -> None:
    """Stop the background scheduler."""
    global _scheduler  # noqa: PLW0603

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
    _scheduler = None


def get_scheduler_status() -> dict:
    """Return scheduler status info."""
    if not _scheduler:
        return {"running": False, "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.isoformat() if next_run else None,
        })

    return {"running": _scheduler.running, "jobs": jobs}
