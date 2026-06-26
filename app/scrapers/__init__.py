"""
IMAX theater scraper package.

Each chain lives in its own module.  Import from here (or from the legacy
shim app.scraper) — callers should not import individual modules directly.
"""
import logging
import threading
from datetime import datetime

from app.scrapers.base import (
    HEADERS,
    REQUEST_TIMEOUT,
    BaseScraper,
    _enrich_movie_from_tmdb,
    _get_active_targets,
    _parse_time_text,
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
# health check). Checked before dispatching to prevent concurrent duplicate
# scrapes of the same theater.
_scraping_in_flight: set[int] = set()

# Chain names that require a Playwright browser (expensive — RAM + CPU).
PLAYWRIGHT_CHAIN_NAMES: frozenset[str] = frozenset(["AMC", "Regal", "TCL"])

# Semaphores are initialised lazily on first use so Settings are available.
_playwright_semaphore: threading.Semaphore | None = None
_http_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _get_semaphore(chain_name: str) -> threading.Semaphore:
    """Return the appropriate semaphore for this chain, initialising it once."""
    global _playwright_semaphore, _http_semaphore  # noqa: PLW0603
    if chain_name in PLAYWRIGHT_CHAIN_NAMES:
        if _playwright_semaphore is None:
            with _semaphore_lock:
                if _playwright_semaphore is None:
                    from app.models import Settings
                    s = Settings.query.filter_by(key="playwright_concurrency").first()
                    cap = max(1, int(s.value)) if s and s.value else 2
                    _playwright_semaphore = threading.Semaphore(cap)
        return _playwright_semaphore
    else:
        if _http_semaphore is None:
            with _semaphore_lock:
                if _http_semaphore is None:
                    from app.models import Settings
                    s = Settings.query.filter_by(key="http_concurrency").first()
                    cap = max(1, int(s.value)) if s and s.value else 5
                    _http_semaphore = threading.Semaphore(cap)
        return _http_semaphore


def is_scraping_in_flight(theater_id: int) -> bool:
    """Return True if any trigger is currently scraping this theater."""
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
    from app.models import Theater, Settings

    if not theater_ids:
        return []

    theaters = Theater.query.filter(Theater.id.in_(theater_ids)).all()
    now = datetime.utcnow()

    s = Settings.query.filter_by(key="scrape_cooldown_minutes").first()
    cooldown_min = int(s.value) if s and s.value else 30

    to_scrape: list = []
    skipped_inflight = 0
    skipped_cooldown = 0

    for theater in theaters:
        if theater.id in _scraping_in_flight:
            skipped_inflight += 1
            continue
        if not force and theater.last_scraped_at is not None:
            age_min = (now - theater.last_scraped_at).total_seconds() / 60
            if age_min < cooldown_min:
                skipped_cooldown += 1
                continue
        to_scrape.append(theater)

    logger.info(
        "Coordinator: %d/%d theaters queued (in-flight=%d, cooldown=%d, force=%s)",
        len(to_scrape), len(theaters), skipped_inflight, skipped_cooldown, force,
    )

    if not to_scrape:
        return []

    # Build per-chain groups and resolve scraper instances.
    scraper_by_chain = {sc.chain_name: sc for sc in ALL_SCRAPERS}
    chain_groups: dict[str, list] = {}
    for theater in to_scrape:
        chain = theater.chain_name
        if chain not in scraper_by_chain:
            logger.debug("Coordinator: no scraper registered for chain '%s'", chain)
            continue
        chain_groups.setdefault(chain, []).append(theater)

    # Build a resolved targets dict: use {theater_id: {None}} when no targets
    # were provided (scrape all movies).
    resolved_targets: dict
    if targets is None:
        resolved_targets = {t.id: {None} for t in to_scrape}
    else:
        resolved_targets = targets

    all_new: list[Showtime] = []

    for chain_name, chain_theaters in chain_groups.items():
        scraper = scraper_by_chain[chain_name]
        sem = _get_semaphore(chain_name)

        # Mark in-flight before acquiring the semaphore so other triggers
        # see these theaters as busy while this batch is waiting to start.
        for t in chain_theaters:
            _scraping_in_flight.add(t.id)

        acquired = sem.acquire(blocking=True, timeout=300)  # 5 min max wait
        if not acquired:
            logger.warning(
                "Coordinator: semaphore timeout for chain '%s' — skipping %d theaters",
                chain_name, len(chain_theaters),
            )
            for t in chain_theaters:
                _scraping_in_flight.discard(t.id)
            continue

        try:
            new = scraper.scrape_theaters_batch(chain_theaters, resolved_targets)
            all_new.extend(new)
        except Exception as exc:  # noqa: BLE001
            logger.error("Coordinator: chain '%s' batch failed: %s", chain_name, exc)
        finally:
            sem.release()
            _finish_theater_scrapes(chain_theaters)

    return all_new


def _finish_theater_scrapes(theaters: list) -> None:
    """Update last_scraped_at and remove theaters from the in-flight set."""
    from app import db
    now = datetime.utcnow()
    for theater in theaters:
        _scraping_in_flight.discard(theater.id)
        theater.last_scraped_at = now
    try:
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Coordinator: could not update last_scraped_at: %s", exc)


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
    "is_scraping_in_flight",
    "queue_theaters_for_scrape",
    "run_all_scrapers",
]
