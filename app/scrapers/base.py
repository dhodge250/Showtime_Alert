"""
Base scraper class, shared helpers, and maintenance utilities.
"""
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app import db
from app.models import AlertMovie, AlertPreference, Movie, Showtime, Theater

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Alert-driven target resolution
# ---------------------------------------------------------------------------

def _get_active_targets() -> dict:
    """
    Return {theater_id: set[movie_id]} for all active, unsent alerts.

    Keys:
      theater_id=None  → "any theater" alert; its movie set applies globally.
      theater_id=N     → specific theater; only those movies should be scraped there.

    Values (movie sets):
      movie_id=None    → "any movie" sentinel (alert has no AlertMovie rows).
      movie_id=N       → track only this movie at the keyed theater.

    Keeping movies scoped per theater prevents a "any movie at theater A" alert
    from causing unrelated movies to be recorded at theater B.
    """
    active_prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()

    targets: dict = {}

    for pref in active_prefs:
        tid = pref.theater_id  # None = any theater
        if tid not in targets:
            targets[tid] = set()

        am_count = pref.alert_movies.count()
        if am_count == 0:
            targets[tid].add(None)  # None = any movie sentinel
        else:
            for am in pref.alert_movies.filter_by(alert_sent=False).all():
                targets[tid].add(am.movie_id)

    return targets


class BaseScraper:
    """Base class for IMAX theater scrapers."""

    chain_name: str = ""

    def fetch(self, url: str) -> Optional[BeautifulSoup]:
        """Fetch a URL and return a BeautifulSoup object, or None on failure."""
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as exc:
            logger.warning("Failed to fetch %s: %s", url, exc)
            return None

    def get_or_create_movie(self, title: str, **kwargs) -> Movie:
        """
        Return existing movie by title (case-insensitive) or create a new one.

        Before creating a new row, TMDB is queried to resolve the canonical
        tmdb_id for the title.  If an existing Movie already has that id (e.g.
        "Star Wars: The Mandalorian and Grogu" vs "The Mandalorian and Grogu"),
        the existing row is returned instead of creating a duplicate.
        """
        movie = Movie.query.filter(Movie.title.ilike(title)).first()
        if movie:
            return movie

        # Pre-check TMDB to catch title-format mismatches before creating a stub.
        try:
            from app.tmdb import find_movie_by_title, is_configured
            if is_configured():
                result = find_movie_by_title(title)
                if result and result.get("tmdb_id"):
                    existing = Movie.query.filter_by(tmdb_id=result["tmdb_id"]).first()
                    if existing:
                        return existing
        except Exception:
            pass

        movie = Movie(title=title, **kwargs)
        db.session.add(movie)
        db.session.flush()
        _enrich_movie_from_tmdb(movie)
        return movie

    def upsert_showtime(
        self,
        theater: Theater,
        movie: Movie,
        show_datetime: datetime,
        tickets_available: bool = True,
        tickets_url: str = "",
        format_type: str = "IMAX",
    ) -> tuple[Showtime, bool]:
        """Insert or update a showtime row. Returns (showtime, is_new)."""
        showtime = Showtime.query.filter_by(
            theater_id=theater.id,
            movie_id=movie.id,
            show_datetime=show_datetime,
        ).first()

        if showtime:
            showtime.tickets_available = tickets_available
            showtime.last_checked = datetime.now(timezone.utc)
            return showtime, False

        showtime = Showtime(
            theater=theater,
            movie=movie,
            show_datetime=show_datetime,
            tickets_available=tickets_available,
            tickets_url=tickets_url,
            format_type=format_type,
        )
        db.session.add(showtime)
        return showtime, True

    def _movie_wanted(self, movie: Movie, movie_ids: set) -> bool:
        """
        Return True if this movie should have its showtimes persisted.

        None in movie_ids is the "any movie" sentinel — always True.
        Otherwise the movie's DB id must be in the set.
        """
        if None in movie_ids:
            return True
        return movie.id in movie_ids

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Scrape a single theater. Override in subclasses."""
        raise NotImplementedError

    def scrape_all(self) -> list[Showtime]:
        """
        Scrape theaters for this chain that have at least one active, unsent alert.

        Movies are scoped per theater so that an "any movie" alert at theater A
        does not cause unrelated movies to be recorded at theater B.
        """
        targets = _get_active_targets()

        if not targets:
            logger.debug("%s: no active alerts — skipping scrape", self.chain_name)
            return []

        query = Theater.query.filter_by(chain=self.chain_name, is_active=True)
        if None not in targets:
            query = query.filter(Theater.id.in_(targets.keys()))

        theaters = query.all()
        if not theaters:
            return []

        new_showtimes: list[Showtime] = []
        for theater in theaters:
            # Merge movie sets: any-theater alerts + this-theater-specific alerts
            movie_ids: set = set()
            if None in targets:
                movie_ids |= targets[None]
            if theater.id in targets:
                movie_ids |= targets[theater.id]

            if not movie_ids:
                continue

            try:
                results = self.scrape_theater(theater, movie_ids)
                new_showtimes.extend(results)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error scraping %s: %s", theater.name, exc)
        db.session.commit()
        return new_showtimes


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _enrich_movie_from_tmdb(movie: Movie) -> None:
    """
    Attempt to populate a freshly-created Movie row with TMDB metadata.

    Silently does nothing if TMDB is not configured, the title yields no
    results, or the row already has a tmdb_id.
    """
    if movie.tmdb_id:
        return  # already enriched

    try:
        from app.tmdb import find_movie_by_title, is_configured
        if not is_configured():
            return

        result = find_movie_by_title(movie.title)
        if not result:
            return

        # Guard: don't create a duplicate via tmdb_id unique constraint
        existing = Movie.query.filter_by(tmdb_id=result["tmdb_id"]).first()
        if existing and existing.id != movie.id:
            logger.debug(
                "TMDB id=%s already belongs to Movie id=%s; skipping enrichment of id=%s",
                result["tmdb_id"], existing.id, movie.id,
            )
            return

        movie.tmdb_id    = result["tmdb_id"]
        movie.poster_url = result.get("poster_url") or movie.image_url or ""
        release_date_raw = result.get("release_date")
        if release_date_raw:
            try:
                movie.release_date = date.fromisoformat(release_date_raw)
            except (ValueError, TypeError):
                movie.release_date = None
        else:
            movie.release_date = None
        logger.debug("Enriched Movie id=%s '%s' with TMDB id=%s", movie.id, movie.title, movie.tmdb_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB enrichment failed for '%s': %s", movie.title, exc)


def _parse_time_text(text: str) -> Optional[datetime]:
    """
    Attempt to parse a showtime text string into a datetime.

    Tries multiple common formats used by theater chains.
    Returns None if parsing fails.
    """
    text = text.strip().upper()
    # Formats that include a date component
    date_formats = [
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    # Formats that have only a time component (no year/month/day)
    time_only_formats = [
        "%I:%M %p",
        "%I:%M%p",
        "%H:%M",
        "%I %p",
    ]
    today = datetime.now(timezone.utc).date()

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    for fmt in time_only_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            # Attach today's date and UTC timezone
            parsed = parsed.replace(
                year=today.year, month=today.month, day=today.day, tzinfo=timezone.utc
            )
            return parsed
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Maintenance utilities
# ---------------------------------------------------------------------------

def cleanup_expired_showtimes() -> int:
    """
    Delete Showtime rows whose show_datetime is in the past.

    Called by the nightly maintenance scheduler job.
    Returns the count of rows deleted.
    """
    cutoff = datetime.now(timezone.utc)
    expired = Showtime.query.filter(Showtime.show_datetime < cutoff).all()
    count = len(expired)
    for st in expired:
        db.session.delete(st)
    if count:
        db.session.commit()
        logger.info("Cleaned up %d expired showtime(s).", count)
    return count


def cleanup_orphaned_movies() -> int:
    """
    Delete Movie rows that have no showtimes and no active AlertMovie references.

    Movies added via the scraper become orphaned once their showtimes are deleted.
    Movies added for alerts are protected by their AlertMovie FK and are not touched.
    Returns the count of rows deleted.
    """
    orphaned = (
        Movie.query
        .filter(~Movie.showtimes.any())
        .filter(~Movie.alert_movies.any())
        .all()
    )
    count = len(orphaned)
    for m in orphaned:
        db.session.delete(m)
    if count:
        db.session.commit()
        logger.info("Cleaned up %d orphaned movie(s).", count)
    return count
