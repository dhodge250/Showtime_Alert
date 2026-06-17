"""
TMDB (The Movie Database) integration helpers.

Uses v3 REST API with the key stored in the Settings table.
All functions gracefully return empty/None when not configured.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
REQUEST_TIMEOUT = 10


def _get_api_key() -> str:
    """Return the TMDB API key from the Settings table, or an empty string."""
    try:
        from app.models import Settings  # lazy import to avoid circular deps
        setting = Settings.query.filter_by(key="tmdb_api_key").first()
        return (setting.value or "").strip() if setting else ""
    except Exception:  # noqa: BLE001
        return ""


def is_configured() -> bool:
    """Return True if a TMDB API key is present in settings."""
    return bool(_get_api_key())


def search_movies(query: str) -> list[dict]:
    """
    Search TMDB for movies matching *query*.

    Returns a list of dicts suitable for JSON serialisation.
    """
    api_key = _get_api_key()
    if not api_key or not query:
        return []

    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={
                "api_key": api_key,
                "query": query,
                "page": 1,
                "include_adult": "false",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(
                "TMDB rate limit hit (429) during search for %r", query
            )
            return []
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB search error: %s", exc)
        return []

    return [_format_result(r) for r in results[:20]]


def find_movie_by_title(title: str) -> Optional[dict]:
    """
    Search TMDB for the best matching movie by title.

    Returns a dict with tmdb_id, title, poster_url, release_date, overview
    if a confident top result is found, or None if TMDB is not configured
    or no results are returned.

    This is intentionally lenient — it takes the first result and trusts the
    caller to decide whether the match is good enough.
    """
    api_key = _get_api_key()
    if not api_key or not title:
        return None

    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={
                "api_key": api_key,
                "query": title,
                "page": 1,
                "include_adult": "false",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(
                "TMDB rate limit hit (429) during title lookup for %r", title
            )
            return None
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("TMDB find_movie_by_title error for %r: %s", title, exc)
        return None

    if not results:
        return None

    return _format_result(results[0])


def get_movie_details(tmdb_id: int) -> dict:
    """
    Fetch full movie details from TMDB by ID.

    Returns a dict with keys: title, overview, release_date, runtime,
    poster_url, tmdb_id. Returns an empty dict on error.
    """
    api_key = _get_api_key()
    if not api_key:
        return {}

    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"api_key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning(
                "TMDB rate limit hit (429) during details fetch for id=%s",
                tmdb_id,
            )
            return {}
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "TMDB get_movie_details error for %s: %s", tmdb_id, exc
        )
        return {}

    genres = [g["name"] for g in data.get("genres", [])]
    return {
        "title": data.get("title", ""),
        "overview": data.get("overview", ""),
        "tagline": data.get("tagline", ""),
        "release_date": data.get("release_date"),
        "runtime": data.get("runtime"),
        # NOTE: MPAA certification requires append_to_response=release_dates.
        # The `adult` flag is not the MPAA rating — omit rather than mislead.
        "rating": None,
        "poster_url": (
            (TMDB_IMG + data["poster_path"]) if data.get("poster_path") else ""
        ),
        "tmdb_id": data.get("id"),
        "imdb_id": data.get("imdb_id", ""),
        "vote_average": data.get("vote_average"),
        "genres": genres,
    }


def _format_result(r: dict) -> dict:
    """Format a raw TMDB search result into the app's standard movie dict."""
    return {
        "tmdb_id": r.get("id"),
        "title": r.get("title", ""),
        "overview": r.get("overview", ""),
        "release_date": r.get("release_date", ""),
        "poster_url": (
            (TMDB_IMG + r["poster_path"]) if r.get("poster_path") else ""
        ),
    }
