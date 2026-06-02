"""
IMAX theater scraper package.

Each chain lives in its own module.  Import from here (or from the legacy
shim app.scraper) — callers should not import individual modules directly.
"""
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

import logging

logger = logging.getLogger(__name__)

ALL_SCRAPERS: list[BaseScraper] = [
    AMCScraper(),
    RegalScraper(),
    CinemarkScraper(),
    TCLScraper(),
    RoyalBCMuseumScraper(),
    CineplexScraper(),
]


def run_all_scrapers() -> list[Showtime]:
    """Run every scraper and return all newly discovered showtimes."""
    all_new: list[Showtime] = []
    for scraper in ALL_SCRAPERS:
        try:
            new = scraper.scrape_all()
            all_new.extend(new)
            logger.info("%s: %d new showtimes found", scraper.chain_name, len(new))
        except Exception as exc:  # noqa: BLE001
            logger.error("Scraper %s failed: %s", scraper.chain_name, exc)
    return all_new


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
    "run_all_scrapers",
]
