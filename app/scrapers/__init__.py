"""
IMAX theater scraper package.

Each chain lives in its own module.  Import from here — callers should
not import individual modules directly.
"""
import concurrent.futures
import logging
import threading
from datetime import timedelta

from app.scrapers.base import (
    HEADERS,
    REQUEST_TIMEOUT,
    BaseScraper,
    _enrich_movie_from_tmdb,
    _get_active_targets,
    _parse_time_text,
    _scrape_ctx,
    cleanup_expired_showtimes,
    cleanup_orphaned_movies,
)
from app.scrapers.amc import AMCScraper
from app.scrapers.cinemark import CinemarkScraper
from app.scrapers.cineplex import CineplexScraper
from app.scrapers.regal import RegalScraper
from app.scrapers.royal_bc_museum import RoyalBCMuseumScraper
from app.scrapers.tcl import TCLScraper
from app.models import Showtime
from app.time_utils import utcnow

logger = logging.getLogger(__name__)

ALL_SCRAPERS: list[BaseScraper] = [
    AMCScraper(),
    RegalScraper(),
    CinemarkScraper(),
    TCLScraper(),
    RoyalBCMuseumScraper(),
    CineplexScraper(),
]

# ---------------------------------------------------------------------------
# Coordinator state — process-local, reset on restart
# ---------------------------------------------------------------------------

# Theater IDs currently being scraped by any trigger (scheduled, on-demand,
# health check).  All reads and compound check/add/discard operations must
# hold _inflight_lock to prevent races between concurrent callers.
_scraping_in_flight: set[int] = set()
_inflight_lock = threading.Lock()

# User IDs whose browse-schedule Run Now job is still running (including the
# post-scrape last_run DB commit).  Separate from _scraping_in_flight so the
# status endpoint can tell the client "all theaters done but job still
# finishing" and avoid the race where the page reloads before last_run lands.
_browse_run_users: set[int] = set()
_browse_run_lock = threading.Lock()


def is_browse_run_in_progress(user_id: int) -> bool:
    """Return True if a Run Now job is still in progress for this user."""
    with _browse_run_lock:
        return user_id in _browse_run_users

# Chain names that require a Playwright browser (expensive — RAM + CPU).
PLAYWRIGHT_CHAIN_NAMES: frozenset[str] = frozenset(["AMC", "Regal", "TCL"])

# Semaphores are pre-warmed by initialize_coordinator() at startup so that
# background threads never need an app context to call _get_semaphore().
# If somehow called before initialisation, _get_semaphore() falls back to
# hardcoded defaults without touching the DB.
_playwright_semaphore: threading.Semaphore | None = None
_http_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def initialize_coordinator() -> None:
    """
    Pre-warm coordinator semaphores from Settings.

    Must be called once at startup inside an active app context (e.g. from
    start_scheduler).  After this, _get_semaphore() is safe to call from any
    background thread without requiring an app context.
    """
    global _playwright_semaphore, _http_semaphore  # noqa: PLW0603
    from app.models import Settings
    with _semaphore_lock:
        pw_s = Settings.query.filter_by(key="playwright_concurrency").first()
        try:
            pw_cap = max(1, int(pw_s.value)) if pw_s and pw_s.value else 2
        except (ValueError, TypeError):
            pw_cap = 2

        http_s = Settings.query.filter_by(key="http_concurrency").first()
        try:
            http_cap = max(1, int(http_s.value)) if http_s and http_s.value else 5
        except (ValueError, TypeError):
            http_cap = 5

        _playwright_semaphore = threading.Semaphore(pw_cap)
        _http_semaphore = threading.Semaphore(http_cap)
        logger.debug(
            "Coordinator semaphores initialised: playwright=%d, http=%d",
            pw_cap, http_cap,
        )


def _get_semaphore(chain_name: str) -> threading.Semaphore:
    """
    Return the appropriate semaphore for this chain.

    Safe to call from any thread.  initialize_coordinator() should have been
    called at startup; if not, falls back to hardcoded defaults without
    querying the DB so no app context is required.
    """
    global _playwright_semaphore, _http_semaphore  # noqa: PLW0603
    if chain_name in PLAYWRIGHT_CHAIN_NAMES:
        if _playwright_semaphore is None:
            with _semaphore_lock:
                if _playwright_semaphore is None:
                    _playwright_semaphore = threading.Semaphore(2)
        return _playwright_semaphore
    else:
        if _http_semaphore is None:
            with _semaphore_lock:
                if _http_semaphore is None:
                    _http_semaphore = threading.Semaphore(5)
        return _http_semaphore


def is_scraping_in_flight(theater_id: int) -> bool:
    """Return True if any trigger is currently scraping this theater."""
    with _inflight_lock:
        return theater_id in _scraping_in_flight


def queue_theaters_for_scrape(
    theater_ids: set[int],
    targets: dict | None = None,
    force: bool = False,
) -> list[Showtime]:
    """
    Unified entry point for scheduled and browse-schedule scrape triggers.

    Applies three safeguards before dispatching:
      1. In-flight check — skip theaters already being scraped.
      2. Cooldown check — skip theaters scraped within the last N minutes
         (bypassed when force=True, e.g. for Run Now).
      3. Concurrency cap — acquire a per-type semaphore (Playwright vs HTTP)
         before launching each chain batch.

    targets: {theater_id: set[movie_id]} scoping which movies to fetch per
             theater.  targets[None] = movie set for any-theater alerts.
             Pass None to scrape all movies ({None} sentinel per theater).
    force: bypass the cooldown check (used by on-demand Run Now).
    """
    from flask import current_app
    from app.models import Theater, Settings

    if not theater_ids:
        return []

    # Thread-locals set by on_demand_scrape()/browse_schedule_scrape() on the
    # calling thread do not propagate to the worker threads spawned below —
    # capture them, along with the live app object, before dispatching.
    ctx_on_demand = getattr(_scrape_ctx, "on_demand", False)
    ctx_browse = getattr(_scrape_ctx, "browse_only", False)
    ctx_log_buffer = getattr(_scrape_ctx, "log_buffer", None)
    flask_app = current_app._get_current_object()

    theaters = Theater.query.filter(Theater.id.in_(theater_ids)).all()
    now = utcnow()

    s = Settings.query.filter_by(key="scrape_cooldown_minutes").first()
    try:
        cooldown_min = max(0, int(s.value)) if s and s.value else 30
    except (ValueError, TypeError):
        cooldown_min = 30

    to_scrape: list = []
    skipped_inflight = 0
    skipped_cooldown = 0

    # Atomically filter and claim theaters so no two concurrent callers can
    # both pass the in-flight check and dispatch the same theater.
    with _inflight_lock:
        for theater in theaters:
            if theater.id in _scraping_in_flight:
                skipped_inflight += 1
                continue
            if not force and theater.last_scraped_at is not None:
                age_min = (now - theater.last_scraped_at).total_seconds() / 60
                if age_min < cooldown_min:
                    skipped_cooldown += 1
                    continue
            _scraping_in_flight.add(theater.id)
            to_scrape.append(theater)

    logger.info(
        "Coordinator: %d/%d theaters queued (in-flight=%d, cooldown=%d, force=%s)",
        len(to_scrape), len(theaters), skipped_inflight, skipped_cooldown, force,
    )

    if not to_scrape:
        return []

    # Build per-chain groups (theater IDs only — chain workers re-query
    # Theater rows in their own DB session, see _run_chain) and resolve
    # scraper instances.
    scraper_by_chain = {sc.chain_name: sc for sc in ALL_SCRAPERS}
    chain_groups: dict[str, list[int]] = {}
    for theater in to_scrape:
        chain = theater.chain_name
        if chain not in scraper_by_chain:
            logger.debug("Coordinator: no scraper registered for chain '%s'", chain)
            with _inflight_lock:
                _scraping_in_flight.discard(theater.id)
            continue
        chain_groups.setdefault(chain, []).append(theater.id)

    # Build a resolved targets dict: use {theater_id: {None}} when no targets
    # were provided (scrape all movies).
    resolved_targets: dict
    if targets is None:
        resolved_targets = {t.id: {None} for t in to_scrape}
    else:
        resolved_targets = targets

    if not chain_groups:
        return []

    # Dispatch each chain batch to its own worker thread. The per-chain
    # semaphores (acquired inside _run_chain) remain the real concurrency
    # governor — this just lets independent chains run alongside each other
    # instead of the previous strictly-sequential loop.
    run_start = utcnow()
    all_showtime_ids: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chain_groups)) as executor:
        futures = {
            executor.submit(
                _run_chain,
                flask_app,
                chain_name,
                scraper_by_chain[chain_name],
                chain_theater_ids,
                resolved_targets,
                ctx_on_demand,
                ctx_browse,
                ctx_log_buffer,
            ): chain_name
            for chain_name, chain_theater_ids in chain_groups.items()
        }
        for future in concurrent.futures.as_completed(futures):
            chain_name = futures[future]
            try:
                new_ids = future.result()
                all_showtime_ids.extend(new_ids)
            except Exception as exc:  # noqa: BLE001
                logger.error("Coordinator: chain '%s' worker crashed: %s", chain_name, exc)

    run_elapsed = (utcnow() - run_start).total_seconds()
    logger.info(
        "Coordinator: run complete in %.1fs across %d chain(s), %d new showtime(s)",
        run_elapsed, len(chain_groups), len(all_showtime_ids),
    )

    if not all_showtime_ids:
        return []

    # Showtime rows created inside a worker's own session become detached
    # once its app context pops — re-query on the dispatching thread so
    # callers (counting, process_new_showtimes) get live, attached objects.
    return Showtime.query.filter(Showtime.id.in_(all_showtime_ids)).all()


def _run_chain(
    app,
    chain_name: str,
    scraper,
    theater_ids: list[int],
    resolved_targets: dict,
    ctx_on_demand: bool,
    ctx_browse: bool,
    ctx_log_buffer: list | None,
) -> list[int]:
    """
    Scrape one chain's batch of theaters on a worker thread.

    Runs inside its own app context so it gets its own DB session — the
    caller's Theater/Settings objects belong to the dispatching thread's
    session and are never touched here; theaters are re-queried by id so
    nothing crosses sessions between threads. Returns new Showtime ids only
    (not ORM objects, which would be detached once this context pops).
    """
    # Thread-locals set by on_demand_scrape()/browse_schedule_scrape() on the
    # dispatching thread do not propagate to this thread — apply them here
    # for the duration of this chain's scrape, and clear them afterward.
    _scrape_ctx.on_demand = ctx_on_demand
    _scrape_ctx.browse_only = ctx_browse
    _scrape_ctx.log_buffer = ctx_log_buffer
    try:
        sem = _get_semaphore(chain_name)
        acquired = sem.acquire(blocking=True, timeout=300)  # 5 min max wait
        if not acquired:
            logger.warning(
                "Coordinator: semaphore timeout for chain '%s' — skipping %d theaters",
                chain_name, len(theater_ids),
            )
            with _inflight_lock:
                for tid in theater_ids:
                    _scraping_in_flight.discard(tid)
            return []

        start = utcnow()
        try:
            with app.app_context():
                from app import db
                from app.models import Theater

                worker_theaters = Theater.query.filter(Theater.id.in_(theater_ids)).all()
                try:
                    new, failed = scraper.scrape_theaters_batch(worker_theaters, resolved_targets)
                    new_ids = [s.id for s in new]
                    # Only advance last_scraped_at for theaters that didn't
                    # fail — a theater that raised inside scrape_theater()
                    # should be retried next cycle rather than waiting out
                    # the cooldown window.
                    succeeded_ids = [t.id for t in worker_theaters if t.id not in failed]
                    if succeeded_ids:
                        Theater.query.filter(Theater.id.in_(succeeded_ids)).update(
                            {"last_scraped_at": utcnow()}, synchronize_session=False,
                        )
                        db.session.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Coordinator: chain '%s' batch failed: %s", chain_name, exc)
                    # Clear the failed transaction so a retry on the next
                    # cycle doesn't hit PendingRollbackError on its first query.
                    db.session.rollback()
                    new_ids = []
        finally:
            sem.release()
            with _inflight_lock:
                for tid in theater_ids:
                    _scraping_in_flight.discard(tid)

        elapsed = (utcnow() - start).total_seconds()
        logger.info(
            "Coordinator: chain '%s' finished in %.1fs (%d theater(s))",
            chain_name, elapsed, len(theater_ids),
        )
        return new_ids
    finally:
        _scrape_ctx.on_demand = False
        _scrape_ctx.browse_only = False
        _scrape_ctx.log_buffer = None


def run_browse_schedules() -> list[Showtime]:
    """
    Consolidated browse-schedule job: scrapes all showtimes from every theater
    within each due user's configured radius and stores them for passive browsing.

    No alerts are created or sent.  All coordination (deduplication across users
    with overlapping radii, cooldown, in-flight tracking, and concurrency caps)
    is handled entirely by the coordinator — this function only computes the
    union of theater sets and delegates to queue_theaters_for_scrape().

    Execution flow:
      1. Query all enabled BrowseSchedule rows where next_run <= now(UTC).
      2. For each due schedule compute theaters within the user's radius.
      3. Union all theater sets from all due schedules.
      4. Call queue_theaters_for_scrape() with the combined set.
      5. Update last_run + next_run for every processed schedule.
    """
    from app import db
    from app.models import BrowseSchedule, Theater, User
    from app.scrapers.base import theater_ids_within_radius, to_km
    from app.log_utils import write_log

    now = utcnow()
    due = BrowseSchedule.query.filter_by(enabled=True).filter(
        BrowseSchedule.next_run <= now
    ).all()

    if not due:
        logger.debug("Browse schedules: no schedules due")
        return []

    logger.info("Browse schedules: %d schedule(s) due", len(due))

    # Fetch active geocoded theaters once and reuse across all radius calculations
    # to avoid an extra DB query per due schedule.
    all_geocoded_theaters = (
        Theater.query.filter_by(is_active=True)
        .filter(Theater.latitude.isnot(None), Theater.longitude.isnot(None))
        .all()
    )

    all_theater_ids: set[int] = set()
    schedule_info = []
    # Only schedules with a valid user location are considered "processed".
    # Skipped schedules are left unchanged so they're retried on the next tick
    # rather than being silently delayed by frequency_minutes.
    # Each entry is (schedule, user_tz_name) so next_run can be computed correctly.
    processed_schedules: list[tuple] = []

    for schedule in due:
        user = User.query.get(schedule.user_id)
        if user is None or user.location_lat is None or user.location_lon is None:
            logger.debug(
                "Browse schedule %d: user has no saved location — skipping", schedule.id
            )
            continue

        radius_km = to_km(schedule.radius, schedule.radius_unit)
        theater_ids = theater_ids_within_radius(
            user.location_lat, user.location_lon, radius_km,
            theaters=all_geocoded_theaters,
        )
        all_theater_ids |= theater_ids
        schedule_info.append({
            "user": user.name,
            "radius": schedule.radius,
            "unit": schedule.radius_unit,
            "theaters_in_radius": len(theater_ids),
        })
        processed_schedules.append((schedule, user.timezone or "UTC"))

    if not all_theater_ids:
        # No theaters in radius: still advance schedules — the run completed,
        # just found nothing to scrape.
        for schedule, tz_name in processed_schedules:
            schedule.last_run = now
            schedule.next_run = schedule.compute_next_run(now, tz_name)
        db.session.commit()
        logger.info("Browse schedules: no theaters found in any user radius — done")
        return []

    logger.info(
        "Browse schedules: %d theater(s) in combined radius across %d processed schedule(s)",
        len(all_theater_ids), len(processed_schedules),
    )

    from app.models import LogEntry
    from app.scrapers.base import browse_schedule_scrape
    start = utcnow()
    with browse_schedule_scrape() as log_buf:
        new_showtimes = queue_theaters_for_scrape(all_theater_ids, targets=None, force=False)
    elapsed = (utcnow() - start).total_seconds()

    # Advance last_run/next_run after the scrape completes so a crash between
    # dispatch and commit causes a retry rather than a silent data-loss with a
    # false "ran" timestamp.  Use compute_next_run so Daily/Weekly schedules
    # respect the user's preferred hour and timezone.
    for schedule, tz_name in processed_schedules:
        schedule.last_run = now
        schedule.next_run = schedule.compute_next_run(now, tz_name)
    db.session.commit()

    # Flush scraper WARNING/ERROR records captured during the scrape.
    for level, msg in log_buf:
        db.session.add(LogEntry(level=level, category="scrape", message=msg))

    write_log(
        "scrape",
        f"Browse schedules: {len(new_showtimes)} new showtime(s) from "
        f"{len(all_theater_ids)} theater(s) "
        f"({len(processed_schedules)}/{len(due)} schedule(s) processed, {elapsed:.1f}s)",
        details={
            "schedules": schedule_info,
            "theaters_dispatched": len(all_theater_ids),
            "new_showtimes": len(new_showtimes),
            "due_count": len(due),
            "processed_count": len(processed_schedules),
        },
    )
    return new_showtimes


def run_all_scrapers() -> list[Showtime]:
    """
    Collect all alert-targeted theaters and dispatch through the coordinator.

    Replaces the old per-chain scrape_all() loop.  The coordinator applies
    cooldown, in-flight, and concurrency safeguards before scraping.
    """
    from app.models import Theater

    targets = _get_active_targets()
    if not targets:
        logger.info("run_all_scrapers: no active alerts — skipping")
        return []

    # Collect every theater ID that each chain's scraper would visit.
    # When targets[None] exists (any-theater alerts), include all active
    # theaters for each registered scraper chain.
    all_theater_ids: set[int] = set()
    for scraper in ALL_SCRAPERS:
        query = Theater.query.filter_by(chain=scraper.chain_name, is_active=True)
        if None not in targets:
            query = query.filter(Theater.id.in_(targets.keys()))
        for t in query.all():
            all_theater_ids.add(t.id)

    if not all_theater_ids:
        return []

    return queue_theaters_for_scrape(all_theater_ids, targets=targets, force=False)


__all__ = [
    "HEADERS",
    "REQUEST_TIMEOUT",
    "BaseScraper",
    "AMCScraper",
    "RegalScraper",
    "CinemarkScraper",
    "TCLScraper",
    "CineplexScraper",
    "RoyalBCMuseumScraper",
    "_get_active_targets",
    "_parse_time_text",
    "_enrich_movie_from_tmdb",
    "cleanup_expired_showtimes",
    "cleanup_orphaned_movies",
    "ALL_SCRAPERS",
    "PLAYWRIGHT_CHAIN_NAMES",
    "initialize_coordinator",
    "is_scraping_in_flight",
    "queue_theaters_for_scrape",
    "run_all_scrapers",
    "run_browse_schedules",
]
