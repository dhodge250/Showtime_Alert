"""
Base scraper class, shared helpers, and maintenance utilities.
"""
import contextlib
import json
import logging
import math
import re
import threading
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from app import db
from app.models import AlertMovie, AlertPreference, Movie, Showtime, Theater, User

logger = logging.getLogger(__name__)

# Thread-local flag: set to True inside on_demand_scrape() context manager so
# upsert_showtime() marks new rows as on_demand without changing the scraper API.
_scrape_ctx = threading.local()

# ---------------------------------------------------------------------------
# Movie title normalisation for TMDB matching
# ---------------------------------------------------------------------------

# Suffixes theaters append that are NOT part of the canonical movie title.
_TITLE_SUFFIX_RE = re.compile(
    r"""
    \s*
    (?:
        # --- Format markers ---
        [-–]\s*imax\b
    |   \bin\s+imax(?:\s+(?:3d|laser(?:\s+with\s+laser)?))?
    |   \bimax(?:\s+(?:3d|laser(?:\s+with\s+laser)?))?
    |   \b3d\b
    |   \b4dx\b
    |   \bscreenx\b
    |   \bdolby\s+(?:cinema|atmos|vision)\b
    |   \(\d{4}\)                                 # trailing year "(2025)"

        # --- Accessibility / language variants ---
        # Matches: (Open Cap/Eng Sub), (Sensory), (Sensory Friendly), (Hindi), (CC), etc.
    |   \([^)]*\b(?:open\s+cap|closed\s+cap|eng(?:lish)?\s+sub|sensory(?:\s+friendly)?
                  |dubbed?|hindi|sub(?:title)?s?|cc\b|caption(?:ed)?
                  |audio\s+desc(?:ription)?)\b[^)]*\)

        # --- Event / screening qualifiers ---
    |   [-–]\s*(?:Early\s+Access|Fan\s+First\s+Screen\w*|Ghibli\s+\d{2,4})
    |   \bFan\s+First\s+Screen\w*
    |   \bFirst\s+Show\s+Events?
    |   \bFan\s+Events?
    |   \bEarly\s+Access\b

        # --- Anniversary / special event ---
        # Matches: "40th Anniv - Ghibli 2026", "85th Anniversary", "25th Anniv."
    |   \d+(?:st|nd|rd|th)\s+Anniv(?:ersary|\.)?(?:\s*[-–].*)?
    |   [-–]\s*\d+(?:st|nd|rd|th)\s+Anniversary
    )
    \s*$
    """,
    re.I | re.VERBOSE,
)

# Prefixes that are event/format labels, not part of the canonical title.
# "(IMAX) Title", "3D Title", "Pride: Title", "SMX26: Title", "Fangoria: Title"
_TITLE_PREFIX_RE = re.compile(
    r"^\([^)]+\)\s+"                                       # (FORMAT) Title
    r"|^3[Dd]\s+"                                          # 3D Title
    r"|^(?:Pride|SMX\d+|Fangoria|Sensory.Sensitive):\s*"  # event series
, re.I)


def _clean_title_for_tmdb(title: str) -> str:
    """Strip common theater-added format markers to get a searchable title."""
    cleaned = title.strip()
    cleaned = _TITLE_PREFIX_RE.sub("", cleaned)
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _TITLE_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned


@contextlib.contextmanager
def on_demand_scrape():
    """Mark all upsert_showtime calls on this thread as on_demand=True."""
    _scrape_ctx.on_demand = True
    try:
        yield
    finally:
        _scrape_ctx.on_demand = False

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

        Lookup order:
          1. Exact ilike match on raw title.
          2. Exact ilike match on cleaned title (format suffixes stripped).
          3. TMDB lookup (raw title, then cleaned) → match by tmdb_id or
             canonical title; opportunistically fix messy stored titles.
          4. Create with canonical TMDB title (or cleaned, or raw as fallback).
        """
        # 1. Exact match on raw title
        movie = Movie.query.filter(Movie.title.ilike(title)).first()
        if movie:
            # Re-enrich un-enriched stubs so the title gets fixed on next scrape
            if not movie.tmdb_id:
                _enrich_movie_from_tmdb(movie)
            return movie

        # 2. Exact match on cleaned title
        cleaned = _clean_title_for_tmdb(title)
        if cleaned != title:
            movie = Movie.query.filter(Movie.title.ilike(cleaned)).first()
            if movie:
                if not movie.tmdb_id:
                    _enrich_movie_from_tmdb(movie)
                return movie

        # 3. TMDB lookup — raw title first, then cleaned
        tmdb_result = None
        try:
            from app.tmdb import find_movie_by_title, is_configured
            if is_configured():
                tmdb_result = find_movie_by_title(title)
                if not tmdb_result and cleaned != title:
                    tmdb_result = find_movie_by_title(cleaned)
        except Exception:
            pass

        if tmdb_result and tmdb_result.get("tmdb_id"):
            # Match by tmdb_id — most reliable dedup path
            existing = Movie.query.filter_by(tmdb_id=tmdb_result["tmdb_id"]).first()
            if existing:
                # Opportunistically update a messy stored title to canonical
                canonical = tmdb_result.get("title") or ""
                if canonical and existing.title != canonical:
                    conflict = Movie.query.filter(
                        Movie.title.ilike(canonical), Movie.id != existing.id
                    ).first()
                    if not conflict:
                        existing.title = canonical
                return existing

            # Match by canonical TMDB title (avoid creating a near-duplicate)
            canonical = tmdb_result.get("title") or ""
            if canonical:
                existing = Movie.query.filter(Movie.title.ilike(canonical)).first()
                if existing:
                    return existing

        # 4. Create new movie — prefer canonical TMDB title > cleaned > raw
        store_title = (tmdb_result or {}).get("title") or cleaned or title
        movie = Movie(title=store_title, **kwargs)
        db.session.add(movie)
        db.session.flush()

        if tmdb_result:
            _apply_tmdb_to_movie(movie, tmdb_result)
        else:
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
        on_demand = getattr(_scrape_ctx, "on_demand", False)

        showtime = Showtime.query.filter_by(
            theater_id=theater.id,
            movie_id=movie.id,
            show_datetime=show_datetime,
        ).first()

        if showtime:
            showtime.tickets_available = tickets_available
            showtime.last_checked = datetime.now(timezone.utc)
            # Alert showtimes (on_demand=False) are never downgraded by an on-demand fetch.
            if not on_demand:
                showtime.on_demand = False
            return showtime, False

        showtime = Showtime(
            theater=theater,
            movie=movie,
            show_datetime=show_datetime,
            tickets_available=tickets_available,
            tickets_url=tickets_url,
            format_type=format_type,
            on_demand=on_demand,
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

def _apply_tmdb_to_movie(movie: Movie, result: dict) -> None:
    """Write a TMDB result dict onto a Movie row (tmdb_id, poster_url, release_date)."""
    movie.tmdb_id    = result["tmdb_id"]
    movie.poster_url = result.get("poster_url") or getattr(movie, "image_url", "") or ""
    release_date_raw = result.get("release_date")
    if release_date_raw:
        try:
            movie.release_date = date.fromisoformat(release_date_raw)
        except (ValueError, TypeError):
            movie.release_date = None
    else:
        movie.release_date = None


def _enrich_movie_from_tmdb(movie: Movie) -> None:
    """
    Attempt to populate a Movie row with TMDB metadata.

    Also updates movie.title to the canonical TMDB title when the stored title
    differs (e.g. theater added " in IMAX" or used different punctuation).
    Silently does nothing if TMDB is not configured or no results are found.
    """
    if movie.tmdb_id:
        return

    try:
        from app.tmdb import find_movie_by_title, is_configured
        if not is_configured():
            return

        # Try stored title first, then cleaned version
        result = find_movie_by_title(movie.title)
        if not result:
            cleaned = _clean_title_for_tmdb(movie.title)
            if cleaned != movie.title:
                result = find_movie_by_title(cleaned)
        if not result:
            return

        # Guard: if another movie already holds this tmdb_id, merge this stub into it
        existing = Movie.query.filter_by(tmdb_id=result["tmdb_id"]).first()
        if existing and existing.id != movie.id:
            logger.debug(
                "TMDB id=%s belongs to Movie id=%s — merging stub id=%s into it",
                result["tmdb_id"], existing.id, movie.id,
            )
            _merge_duplicate_movie(stub=movie, canonical=existing)
            return

        # Update title to canonical TMDB title when safe (no other row has it)
        canonical = result.get("title") or ""
        if canonical and canonical != movie.title:
            conflict = Movie.query.filter(
                Movie.title.ilike(canonical), Movie.id != movie.id
            ).first()
            if not conflict:
                movie.title = canonical

        _apply_tmdb_to_movie(movie, result)
        logger.debug("Enriched Movie id=%s '%s' with TMDB id=%s", movie.id, movie.title, movie.tmdb_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB enrichment failed for '%s': %s", movie.title, exc)


def _merge_duplicate_movie(stub: Movie, canonical: Movie) -> None:
    """
    Re-point all references from stub to canonical, then delete the stub.

    Called when enrichment discovers a newly-created stub is actually the same
    film as an existing canonical record (matched via tmdb_id).
    """
    try:
        # Re-point Showtime rows (skip any that would violate the unique constraint)
        for st in list(stub.showtimes):
            conflict = Showtime.query.filter_by(
                theater_id=st.theater_id,
                movie_id=canonical.id,
                show_datetime=st.show_datetime,
            ).first()
            if conflict:
                db.session.delete(st)
            else:
                st.movie_id = canonical.id

        # Re-point AlertMovie rows
        for am in list(stub.alert_movies):
            conflict = AlertMovie.query.filter_by(
                alert_id=am.alert_id,
                movie_id=canonical.id,
            ).first()
            if conflict:
                db.session.delete(am)
            else:
                am.movie_id = canonical.id

        # Re-point legacy AlertPreference.movie_id
        AlertPreference.query.filter_by(movie_id=stub.id).update(
            {"movie_id": canonical.id}, synchronize_session="fetch"
        )

        db.session.flush()
        db.session.delete(stub)
        db.session.flush()

        # Update canonical title to TMDB canonical if needed (done by caller normally)
        logger.info(
            "Merged stub Movie id=%s ('%s') into canonical Movie id=%s ('%s')",
            stub.id, stub.title, canonical.id, canonical.title,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to merge Movie stub id=%s into id=%s: %s", stub.id, canonical.id, exc)


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
