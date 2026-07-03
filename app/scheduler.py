"""
Scheduler for periodic IMAX showtime scraping, venue list crawling,
alert processing, and expired showtime cleanup.

Uses APScheduler to run six jobs on independent schedules:
  - Showtime scraper:    every N minutes (default 30)
  - Alert processor:     every N minutes (default 15)
  - Venue crawler:       every N days    (default 7)
  - Showtime cleanup:    every N hours   (default 24)
  - Browse schedules:    every N minutes (default 30)
  - Scraper health check: cron schedule configured in Settings
"""
import logging
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# Tracks whether the scheduled health-check job is currently running.
# Mutated in-place so the reference stays stable for callers.  All reads and
# writes must hold _health_state_lock — the dict is shared between the cron
# job thread, manual-trigger threads, and status-endpoint request threads.
_health_check_state: dict = {
    "running": False,
    "chain_name": None,
    "started_at": None,
    "completed": 0,
    "total": 0,
}
_health_state_lock = threading.Lock()


def get_health_check_state() -> dict:
    """Return a snapshot of the current health-check job state."""
    with _health_state_lock:
        return dict(_health_check_state)


def trigger_health_check(app) -> bool:
    """
    Spawn _health_check_job in a daemon thread.

    Returns True if the job was started, False if it was already running.
    _health_check_job itself claims the running flag atomically, so even if
    two triggers race past this check, only one run proceeds.
    """
    with _health_state_lock:
        if _health_check_state["running"]:
            return False
    thread = threading.Thread(
        target=_health_check_job,
        args=(app,),
        daemon=True,
        name="health-check-manual",
    )
    thread.start()
    return True


def is_on_demand_fetch_running(theater_id: int) -> bool:
    """Return True if any scrape is currently in progress for this theater."""
    from app.scrapers import is_scraping_in_flight
    return is_scraping_in_flight(theater_id)


def trigger_theater_fetch(theater_id: int, scraper, app) -> bool:
    """
    Start a per-theater on-demand showtime fetch in a background daemon thread.

    Returns True if the fetch was started, False if one is already in progress.
    The fetch acquires the coordinator semaphore and updates last_scraped_at.
    """
    from app.scrapers import _scraping_in_flight, _inflight_lock, _get_semaphore

    with _inflight_lock:
        if theater_id in _scraping_in_flight:
            return False
        # Claim in-flight atomically so no concurrent caller can also claim it.
        _scraping_in_flight.add(theater_id)

    def _run():
        try:
            sem = _get_semaphore(scraper.chain_name)
            acquired = sem.acquire(blocking=True, timeout=300)
            if not acquired:
                logger.warning(
                    "On-demand fetch: semaphore timeout for theater %d", theater_id
                )
                return
            try:
                with app.app_context():
                    _theater_fetch_job(theater_id, scraper)
            finally:
                sem.release()
        finally:
            with _inflight_lock:
                _scraping_in_flight.discard(theater_id)

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"on-demand-fetch-{theater_id}",
    ).start()
    return True


def _theater_fetch_job(theater_id: int, scraper) -> None:
    """Background job: scrape all showtimes for one theater as on-demand rows."""
    from datetime import datetime, timezone
    from app import db
    from app.models import Showtime, Theater
    from app.scrapers.base import on_demand_scrape

    theater = Theater.query.get(theater_id)
    if theater is None:
        return

    # Remove stale on-demand showtimes; alert showtimes (on_demand=False) are untouched.
    Showtime.query.filter_by(theater_id=theater_id, on_demand=True).delete()
    db.session.commit()

    try:
        with on_demand_scrape():
            scraper.scrape_theater(theater, {None})
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        theater.on_demand_fetched_at = now
        theater.last_scraped_at = now
        db.session.commit()
        logger.info("On-demand fetch complete for theater %s (%s)", theater_id, theater.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("On-demand fetch failed for theater %s: %s", theater_id, exc)


def trigger_single_health_check(scraper, app) -> None:
    """
    Run a single-chain health check in a background daemon thread.

    Acquires the coordinator semaphore for the chain so the probe does not
    run concurrently with a scheduled or on-demand scrape of the same chain.
    Returns immediately — the result is written to the DB by the thread.
    """
    from app.scrapers import _get_semaphore
    from app.scrapers.health import run_health_check

    def _run():
        # _get_semaphore is safe to call here — it does not require an app
        # context after initialize_coordinator() ran at startup.
        sem = _get_semaphore(scraper.chain_name)
        acquired = sem.acquire(blocking=True, timeout=120)
        if not acquired:
            logger.warning("Health check: semaphore timeout for %s", scraper.chain_name)
            return
        try:
            with app.app_context():
                run_health_check(scraper)
        finally:
            sem.release()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"health-check-{scraper.chain_name}",
    ).start()


def _scrape_job(app):
    """Scheduled job: run all scrapers and dispatch notifications."""
    from app.notifications import process_new_showtimes
    from app.scrapers import run_all_scrapers

    with app.app_context():
        from app.log_utils import write_log
        logger.info("Scheduled scrape starting...")
        write_log("scrape", "Scheduled scrape starting")
        try:
            new_showtimes = run_all_scrapers()
            logger.info("Scrape complete. %d new showtimes found.", len(new_showtimes))
            sent = 0
            if new_showtimes:
                sent = process_new_showtimes(app, new_showtimes)
                logger.info("Sent %d notifications.", sent)
            write_log("scrape",
                      f"Scheduled scrape complete: {len(new_showtimes)} new showtimes, {sent} notifications sent",
                      details={"new_showtimes": len(new_showtimes), "notifications_sent": sent})
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduled scrape failed: %s", exc)
            write_log("scrape", f"Scheduled scrape failed: {exc}", level="ERROR")


def _venue_crawl_job(app):
    """Scheduled job: crawl IMAX venue list and refresh theater DB rows."""
    from app.venue_crawler import run_venue_crawl

    with app.app_context():
        from app.log_utils import write_log
        logger.info("Scheduled venue crawl starting...")
        write_log("scrape", "Scheduled venue crawl starting")
        try:
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
            level = "WARNING" if summary["errors"] else "INFO"
            write_log("scrape",
                      f"Venue crawl complete: {summary['venues_found']} venues, "
                      f"{summary['inserted']} inserted, {summary['updated']} updated, "
                      f"{len(summary['errors'])} errors",
                      level=level,
                      details=summary)
            if summary["errors"]:
                for err in summary["errors"]:
                    logger.warning("Venue crawl error: %s", err)
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduled venue crawl failed: %s", exc)
            write_log("scrape", f"Scheduled venue crawl failed: {exc}", level="ERROR")


def _alert_job(app):
    """Scheduled job: process pending alerts against existing showtimes."""
    from app.notifications import process_pending_alerts

    with app.app_context():
        from app.log_utils import write_log
        logger.info("Alert processor starting...")
        try:
            sent = process_pending_alerts(app)
            if sent:
                logger.info("Alert processor complete. %d notification(s) sent.", sent)
                write_log("alert", f"Alert processor: {sent} notification(s) sent",
                          details={"notifications_sent": sent})
            else:
                logger.debug("Alert processor complete. No notifications to send.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Alert processor failed: %s", exc)
            write_log("alert", f"Alert processor failed: {exc}", level="ERROR")


def _cleanup_job(app):
    """Scheduled job: delete expired showtimes, orphaned movies, and old log entries."""
    from datetime import datetime, timezone, timedelta

    from app.scrapers import cleanup_expired_showtimes, cleanup_orphaned_movies

    with app.app_context():
        count = cleanup_expired_showtimes()
        orphaned = cleanup_orphaned_movies()

        # Purge log entries older than log_retention_days setting
        try:
            from app import db
            from app.models import LogEntry, Settings
            s = Settings.query.filter_by(key="log_retention_days").first()
            retention_days = int(s.value) if s and s.value else 30
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            deleted_logs = LogEntry.query.filter(LogEntry.created_at < cutoff).delete()
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            deleted_logs = 0
            logger.warning("Log cleanup failed: %s", exc)

        logger.info(
            "Cleanup job complete. %d expired showtime(s), %d orphaned movie(s), "
            "%d log entries removed.",
            count,
            orphaned,
            deleted_logs,
        )


def _browse_schedules_job(app):
    """Scheduled job: run all due browse schedules and store scraped showtimes."""
    from app.scrapers import run_browse_schedules

    with app.app_context():
        from app.log_utils import write_log
        logger.info("Browse schedules job starting...")
        try:
            new_showtimes = run_browse_schedules()
            if new_showtimes:
                logger.info("Browse schedules job complete. %d new showtime(s).", len(new_showtimes))
            else:
                logger.debug("Browse schedules job complete. No new showtimes.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Browse schedules job failed: %s", exc)
            write_log("scrape", f"Browse schedules job failed: {exc}", level="ERROR")


def _health_check_job(app):
    """Scheduled job: run a lightweight health check against every registered scraper chain."""
    # Atomic claim: the cron firing and a manual trigger can race — only one
    # may proceed, the other exits without touching shared state.
    with _health_state_lock:
        if _health_check_state["running"]:
            logger.info("Health check already running — skipping this invocation.")
            return
        _health_check_state["running"] = True
        _health_check_state["chain_name"] = None
        _health_check_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _health_check_state["completed"] = 0
        _health_check_state["total"] = 0
    try:
        with app.app_context():
            from app.log_utils import write_log
            from app.scrapers import ALL_SCRAPERS
            from app.scrapers.health import run_health_check

            logger.info("Scraper health check starting...")
            write_log("scrape", "Scraper health check starting")
            from app.scrapers import _get_semaphore

            with _health_state_lock:
                _health_check_state["total"] = len(ALL_SCRAPERS)
            results = []
            for scraper in ALL_SCRAPERS:
                with _health_state_lock:
                    _health_check_state["chain_name"] = scraper.chain_name
                sem = _get_semaphore(scraper.chain_name)
                acquired = sem.acquire(blocking=True, timeout=120)
                if not acquired:
                    logger.warning(
                        "Health check: semaphore timeout for %s — skipping",
                        scraper.chain_name,
                    )
                    with _health_state_lock:
                        _health_check_state["completed"] += 1
                    results.append({
                        "status": "error",
                        "chain_name": scraper.chain_name,
                        "error_summary": "Skipped — semaphore timeout",
                    })
                    continue
                try:
                    result = run_health_check(scraper)
                finally:
                    sem.release()
                with _health_state_lock:
                    _health_check_state["completed"] += 1
                results.append(result)
                logger.info(
                    "Health check %s: status=%s showtimes=%s",
                    scraper.chain_name,
                    result["status"],
                    result.get("showtime_count"),
                )
            ok = sum(1 for r in results if r["status"] == "ok")
            warn = sum(1 for r in results if r["status"] == "warning")
            err = sum(1 for r in results if r["status"] == "error")
            write_log(
                "scrape",
                f"Scraper health check complete: {ok} OK, {warn} Warning, {err} Error",
                details={"results": results},
            )
    finally:
        with _health_state_lock:
            _health_check_state["running"] = False
            _health_check_state["chain_name"] = None
            _health_check_state["started_at"] = None
            _health_check_state["completed"] = 0
            _health_check_state["total"] = 0


def _build_health_trigger(
    frequency: str,
    time_str: str,
    day_of_week: str,
    day_of_month: str,
    timezone: str = "UTC",
) -> CronTrigger:
    """Return a CronTrigger for the health check job from validated settings strings."""
    try:
        h, m = (int(x) for x in str(time_str or "00:00").split(":"))
    except (ValueError, AttributeError):
        h, m = 0, 0
    h = max(0, min(23, h))
    m = max(0, min(59, m))
    tz = timezone or "UTC"

    if frequency == "weekly":
        try:
            dow = max(0, min(6, int(day_of_week)))
        except (ValueError, TypeError):
            dow = 0
        return CronTrigger(day_of_week=dow, hour=h, minute=m, timezone=tz)
    elif frequency == "monthly":
        try:
            dom = max(1, min(31, int(day_of_month)))
        except (ValueError, TypeError):
            dom = 1
        return CronTrigger(day=dom, hour=h, minute=m, timezone=tz)
    else:
        return CronTrigger(hour=h, minute=m, timezone=tz)


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

    def _setting_str(key: str, default: str) -> str:
        """Read a string scheduler setting from the DB, with a fallback."""
        try:
            from app.models import Settings
            with app.app_context():
                s = Settings.query.filter_by(key=key).first()
                return s.value if s and s.value else default
        except Exception:  # noqa: BLE001
            return default

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
    browse_check_minutes = _setting_int(
        "browse_schedule_check_minutes", "BROWSE_SCHEDULE_CHECK_MINUTES", 30
    )
    hc_freq = _setting_str("health_check_frequency", "daily")
    hc_time = _setting_str("health_check_time", "00:00")
    hc_dow  = _setting_str("health_check_day_of_week", "0")
    hc_dom  = _setting_str("health_check_day_of_month", "1")
    hc_tz   = _setting_str("health_check_timezone", "UTC")

    # Pre-warm coordinator semaphores from Settings while we still have an app
    # context.  After this, _get_semaphore() is safe for any background thread.
    with app.app_context():
        from app.scrapers import initialize_coordinator
        initialize_coordinator()

    from app.scrapers.base import install_browse_log_handler
    install_browse_log_handler()

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

    # Browse schedule runner — checks due schedules every N minutes (default 30)
    _scheduler.add_job(
        func=lambda: _browse_schedules_job(app),
        trigger=IntervalTrigger(minutes=browse_check_minutes),
        id="imax_browse_schedules",
        name="Browse schedule runner",
        replace_existing=True,
    )

    # Scraper health check — cron schedule configured in Settings
    _scheduler.add_job(
        func=lambda: _health_check_job(app),
        trigger=_build_health_trigger(hc_freq, hc_time, hc_dow, hc_dom, hc_tz),
        id="imax_health_check",
        name="Scraper health check",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started; scraping every %d min, alerts every %d min, "
        "venue crawl every %d days, cleanup every %d hours, "
        "browse schedules every %d min, health check on its configured cron schedule.",
        interval_minutes,
        alert_minutes,
        venue_crawl_days,
        cleanup_hours,
        browse_check_minutes,
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


def reschedule_health_check(
    frequency: str,
    time_str: str,
    day_of_week: str,
    day_of_month: str,
    timezone: str = "UTC",
) -> None:
    """Update the health check job trigger without restarting the scheduler."""
    if not _scheduler or not _scheduler.running:
        logger.warning("reschedule_health_check called but scheduler not running; ignoring.")
        return
    trigger = _build_health_trigger(frequency, time_str, day_of_week, day_of_month, timezone)
    _scheduler.reschedule_job("imax_health_check", trigger=trigger)
    logger.info(
        "Health check rescheduled: frequency=%s time=%s dow=%s dom=%s tz=%s",
        frequency, time_str, day_of_week, day_of_month, timezone,
    )
