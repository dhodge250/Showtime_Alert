"""
Backward-compatible shim — implementation lives in app.scrapers package.

All existing callers (routes.py, scheduler.py, tests) import from this module
and continue to work without modification.
"""
from app.scrapers import (  # noqa: F401
    ALL_SCRAPERS,
    HEADERS,
    REQUEST_TIMEOUT,
    AMCScraper,
    BaseScraper,
    CinemarkScraper,
    CineplexScraper,
    RegalScraper,
    RoyalBCMuseumScraper,
    TCLScraper,
    _enrich_movie_from_tmdb,
    _get_active_targets,
    _parse_time_text,
    cleanup_expired_showtimes,
    cleanup_orphaned_movies,
    run_all_scrapers,
)
