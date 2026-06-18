"""
Base scraper class, shared helpers, and maintenance utilities.
"""
import json
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from app import db
from app.models import AlertMovie, AlertPreference, Movie, Showtime, Theater, User

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Theater timezone helpers
# ---------------------------------------------------------------------------

_REGION_TZ: dict[str, str] = {
    # US states (full names)
    "Alabama": "America/Chicago",
    "Alaska": "America/Anchorage",
    "Arizona": "America/Phoenix",
    "Arkansas": "America/Chicago",
    "California": "America/Los_Angeles",
    "Colorado": "America/Denver",
    "Connecticut": "America/New_York",
    "Delaware": "America/New_York",
    "District of Columbia": "America/New_York",
    "Florida": "America/New_York",
    "Georgia": "America/New_York",
    "Hawaii": "Pacific/Honolulu",
    "Idaho": "America/Denver",
    "Illinois": "America/Chicago",
    "Indiana": "America/Indiana/Indianapolis",
    "Iowa": "America/Chicago",
    "Kansas": "America/Chicago",
    "Kentucky": "America/New_York",
    "Louisiana": "America/Chicago",
    "Maine": "America/New_York",
    "Maryland": "America/New_York",
    "Massachusetts": "America/New_York",
    "Michigan": "America/New_York",
    "Minnesota": "America/Chicago",
    "Mississippi": "America/Chicago",
    "Missouri": "America/Chicago",
    "Montana": "America/Denver",
    "Nebraska": "America/Chicago",
    "Nevada": "America/Los_Angeles",
    "New Hampshire": "America/New_York",
    "New Jersey": "America/New_York",
    "New Mexico": "America/Denver",
    "New York": "America/New_York",
    "North Carolina": "America/New_York",
    "North Dakota": "America/Chicago",
    "Ohio": "America/New_York",
    "Oklahoma": "America/Chicago",
    "Oregon": "America/Los_Angeles",
    "Pennsylvania": "America/New_York",
    "Rhode Island": "America/New_York",
    "South Carolina": "America/New_York",
    "South Dakota": "America/Chicago",
    "Tennessee": "America/Chicago",
    "Texas": "America/Chicago",
    "Utah": "America/Denver",
    "Vermont": "America/New_York",
    "Virginia": "America/New_York",
    "Washington": "America/Los_Angeles",
    "West Virginia": "America/New_York",
    "Wisconsin": "America/Chicago",
    "Wyoming": "America/Denver",
    # Canadian provinces (full names and common abbreviations)
    "Alberta": "America/Edmonton",         "AB": "America/Edmonton",
    "British Columbia": "America/Vancouver", "BC": "America/Vancouver",
    "Manitoba": "America/Winnipeg",         "MB": "America/Winnipeg",
    "New Brunswick": "America/Halifax",     "NB": "America/Halifax",
    "Newfoundland": "America/St_Johns",     "NL": "America/St_Johns",
    "Nova Scotia": "America/Halifax",       "NS": "America/Halifax",
    "Ontario": "America/Toronto",           "ON": "America/Toronto",
    "Prince Edward Island": "America/Halifax", "PE": "America/Halifax",
    "Quebec": "America/Montreal",           "QC": "America/Montreal",
    "Saskatchewan": "America/Regina",       "SK": "America/Regina",
    # Other regions encountered in the theater dataset
    "Mecca Province": "Asia/Riyadh",
    "Rio Grande do Sul": "America/Sao_Paulo",
}

_COUNTRY_TZ: dict[str, str] = {
    "Brazil":         "America/Sao_Paulo",
    "Chile":          "America/Santiago",
    "Germany":        "Europe/Berlin",
    "Panama":         "America/Panama",
    "Saudi Arabia":   "Asia/Riyadh",
    "United Kingdom": "Europe/London",
}


def _theater_tz(theater: Theater) -> ZoneInfo:
    """Return the IANA ZoneInfo for a theater's local timezone."""
    name = _REGION_TZ.get(theater.state or "") or _COUNTRY_TZ.get(theater.country or "")
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo("UTC")


def _local_to_utc(naive_local: datetime, theater: Theater) -> datetime:
    """
    Convert a naive local-theater datetime to a naive UTC datetime.

    All showtime rows are stored as naive UTC so comparisons against
    datetime.utcnow() are consistent regardless of the scraper source.
    """
    tz = _theater_tz(theater)
    return naive_local.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

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

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lng points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _get_active_targets() -> dict:
    """
    Return {theater_id: set[movie_id]} for all active, unsent alerts.

    Keys:
      theater_id=None  → "any theater" alert; its movie set applies globally.
      theater_id=N     → specific theater; only those movies should be scraped there.

    Values (movie sets):
      movie_id=None    → "any movie" sentinel (alert has no AlertMovie rows).
      movie_id=N       → track only this movie at the keyed theater.

    Radius alerts expand into their covered theater IDs at resolution time.
    Theaters without geocoded coordinates are excluded from radius matching.
    Keeping movies scoped per theater prevents a "any movie at theater A" alert
    from causing unrelated movies to be recorded at theater B.
    """
    active_prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()

    targets: dict = {}

    active_theaters = Theater.query.filter_by(is_active=True).all()

    for pref in active_prefs:
        # --- radius-based alert ---
        if pref.radius_km is not None:
            user = User.query.get(pref.user_id)
            if user is None or user.location_lat is None or user.location_lon is None:
                continue
            theaters_in_radius = [
                t for t in active_theaters
                if t.latitude is not None and t.longitude is not None
                and _haversine_km(user.location_lat, user.location_lon, t.latitude, t.longitude) <= pref.radius_km
            ]
            movie_ids: set = set()
            am_count = pref.alert_movies.count()
            if am_count == 0:
                movie_ids.add(None)
            else:
                for am in pref.alert_movies.filter_by(alert_sent=False).all():
                    movie_ids.add(am.movie_id)
            for theater in theaters_in_radius:
                if theater.id not in targets:
                    targets[theater.id] = set()
                targets[theater.id] |= movie_ids
            continue

        # --- specific or any-theater alert ---
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
    Parse a showtime text string into a **naive** datetime in local theater time.

    Returns None if parsing fails.  Callers are responsible for converting to
    UTC via _local_to_utc(dt, theater) before storing — do NOT label the result
    as UTC directly, because the source is always a local-time display string.
    """
    text = text.strip().upper()
    date_formats = [
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    time_only_formats = [
        "%I:%M %p",
        "%I:%M%p",
        "%H:%M",
        "%I %p",
    ]
    today = datetime.now(timezone.utc).date()

    for fmt in date_formats:
        try:
            return datetime.strptime(text, fmt)  # naive local
        except ValueError:
            continue

    for fmt in time_only_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(year=today.year, month=today.month, day=today.day)  # naive local
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
