"""
Scraper for TCL Chinese Theatre IMAX showtimes.

TCL's website runs on the Vista/Lumos ticketing platform. Showtimes are
served via the Vista OCAPI at digital-api.tclchinesetheatres.com.  The
API requires a short-lived gasToken that is embedded in the __NEXT_DATA__
of every page load.  Plain requests throughout — no Playwright needed.

Strategy:
  1. Fetch gasToken from the TCL homepage __NEXT_DATA__.
  2. GET /ocapi/v1/film-screening-dates?siteIds=0001 to find all dates
     that have at least one IMAX showtime (attributeId 0000000009).
  3. For each such date, GET /ocapi/v1/showtimes/by-business-date/{date}
     ?siteIds=0001 and filter for showtimes with the IMAX attribute.
  4. Film titles come from relatedData.films in the showtime response.
"""

import json
import logging
import re
from datetime import datetime

import requests

from app.scrapers.base import BaseScraper
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_PAGE_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
_OCAPI_BASE = "https://digital-api.tclchinesetheatres.com"
_SITE_PAGE = "https://www.tclchinesetheatres.com/"
_SITE_ID = "0001"
_IMAX_ATTR_ID = "0000000009"
_REQUEST_TIMEOUT = 15
_NEXT_DATA_RE = re.compile(
    r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _fetch_gas_token(page_url: str = _SITE_PAGE) -> str:
    """Return a fresh gasToken from the TCL homepage __NEXT_DATA__."""
    try:
        r = requests.get(page_url, headers=_PAGE_HEADERS, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        m = _NEXT_DATA_RE.search(r.text)
        if not m:
            logger.warning("TCL: __NEXT_DATA__ not found in %s", page_url)
            return ""
        nd = json.loads(m.group(1))
        return nd["props"]["pageProps"]["environment"]["gasToken"]
    except Exception as exc:
        logger.warning("TCL: failed to fetch gasToken: %s", exc)
        return ""


def _api_headers(gas_token: str) -> dict:
    return {**_PAGE_HEADERS, "Accept": "application/json", "Authorization": f"Bearer {gas_token}"}


def _imax_dates(film_screening_dates: list) -> list[str]:
    """Return business dates that have at least one IMAX showtime."""
    result = []
    for entry in film_screening_dates:
        for screening in entry.get("filmScreenings", []):
            sites = screening.get("sites", [])
            if sites and _IMAX_ATTR_ID in sites[0].get("showtimeAttributeIds", []):
                result.append(entry["businessDate"])
                break
    return result


class TCLScraper(BaseScraper):
    """Scraper for TCL Chinese Theatre IMAX showtimes."""

    chain_name = "TCL"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        if not theater.website:
            return []

        gas_token = _fetch_gas_token()
        if not gas_token:
            logger.warning("TCL: could not obtain gasToken — skipping")
            return []

        hdrs = _api_headers(gas_token)

        try:
            r = requests.get(
                f"{_OCAPI_BASE}/ocapi/v1/film-screening-dates",
                params={"siteIds": _SITE_ID},
                headers=hdrs,
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            dates = _imax_dates(r.json().get("filmScreeningDates", []))
        except Exception as exc:
            logger.warning("TCL: film-screening-dates failed: %s", exc)
            return []

        logger.debug("TCL: %d IMAX dates to scrape", len(dates))
        new_showtimes: list[Showtime] = []
        for date_iso in dates:
            new_showtimes.extend(self._scrape_date(theater, movie_ids, hdrs, date_iso))

        return new_showtimes

    def _scrape_date(
        self, theater: Theater, movie_ids: set, hdrs: dict, date_iso: str
    ) -> list[Showtime]:
        try:
            r = requests.get(
                f"{_OCAPI_BASE}/ocapi/v1/showtimes/by-business-date/{date_iso}",
                params={"siteIds": _SITE_ID},
                headers=hdrs,
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as exc:
            logger.warning("TCL: showtimes failed for %s: %s", date_iso, exc)
            return []

        data = r.json()
        films = {
            f["id"]: f["title"]["text"]
            for f in data.get("relatedData", {}).get("films", [])
        }

        new_showtimes: list[Showtime] = []
        for show in data.get("showtimes", []):
            if _IMAX_ATTR_ID not in show.get("attributeIds", []):
                continue

            title = films.get(show.get("filmId", ""), "")
            if not title:
                continue

            starts_at_str = (show.get("schedule") or {}).get("startsAt", "")
            if not starts_at_str:
                continue
            try:
                show_dt = datetime.fromisoformat(starts_at_str)
            except ValueError:
                logger.warning("TCL: bad startsAt value %r", starts_at_str)
                continue

            show_id = show.get("id", "")
            tickets_url = (
                f"https://www.tclchinesetheatres.com/order/showtimes/{show_id}/tickets"
                if show_id
                else ""
            )

            movie = self.get_or_create_movie(title)
            if not self._movie_wanted(movie, movie_ids):
                continue

            showtime, is_new = self.upsert_showtime(
                theater,
                movie,
                show_dt,
                tickets_available=not show.get("isSoldOut", False),
                tickets_url=tickets_url,
                format_type="IMAX",
            )
            if is_new:
                new_showtimes.append(showtime)

        return new_showtimes
