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
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

from app.scrapers.base import BaseScraper, _local_to_utc, _scrape_ctx
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
# API titles carry format prefixes like "(IMAX) ", "(DBOX) " — strip before DB lookup
_TITLE_PREFIX_RE = re.compile(r"^\([^)]+\)\s+")
_REQUEST_TIMEOUT = 15
_NEXT_DATA_RE = re.compile(
    r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _fetch_gas_token(page_url: str = _SITE_PAGE) -> str:
    """
    Return a fresh gasToken from the TCL homepage __NEXT_DATA__.

    The TCL website is behind Cloudflare, which blocks plain requests from
    Docker's IP space.  Playwright bypasses the CF challenge for the initial
    page load; no cookies are needed for subsequent OCAPI calls — only the
    token itself matters.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_UA, locale="en-US")
            page = ctx.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3_000)
            html = page.content()
            browser.close()
        m = _NEXT_DATA_RE.search(html)
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
            if sites and any(_IMAX_ATTR_ID in s.get("showtimeAttributeIds", []) for s in sites):
                result.append(entry["businessDate"])
                break
    return result


class TCLScraper(BaseScraper):
    """Scraper for TCL Chinese Theatre IMAX showtimes."""

    chain_name = "TCL"
    health_website = "tclchinesetheatres.com"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        if not theater.website:
            return []

        gas_token = _fetch_gas_token()
        if not gas_token:
            logger.warning("TCL: could not obtain gasToken — skipping")
            return []

        hdrs = _api_headers(gas_token)
        on_demand = getattr(_scrape_ctx, "on_demand", False)

        try:
            r = requests.get(
                f"{_OCAPI_BASE}/ocapi/v1/film-screening-dates",
                params={"siteIds": _SITE_ID},
                headers=hdrs,
                timeout=_REQUEST_TIMEOUT,
            )
            r.raise_for_status()
            all_dates_data = r.json().get("filmScreeningDates", [])
            # In on-demand mode use all dates; otherwise only dates with IMAX showtimes
            if on_demand:
                dates = [e["businessDate"] for e in all_dates_data]
            else:
                dates = _imax_dates(all_dates_data)
        except Exception as exc:
            logger.warning("TCL: film-screening-dates failed: %s", exc)
            return []

        logger.debug("TCL: %d dates to scrape (on_demand=%s)", len(dates), on_demand)
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

        on_demand = getattr(_scrape_ctx, "on_demand", False)
        new_showtimes: list[Showtime] = []
        for show in data.get("showtimes", []):
            attr_ids = show.get("attributeIds", [])
            if not on_demand and _IMAX_ATTR_ID not in attr_ids:
                continue

            raw_title = films.get(show.get("filmId", ""), "")
            if not raw_title:
                continue
            # Strip format prefixes like "(IMAX) ", "(DBOX) " so titles match alert movies
            title = _TITLE_PREFIX_RE.sub("", raw_title)

            starts_at_str = (show.get("schedule") or {}).get("startsAt", "")
            if not starts_at_str:
                continue
            try:
                parsed = datetime.fromisoformat(starts_at_str)
            except ValueError:
                logger.warning("TCL: bad startsAt value %r", starts_at_str)
                continue
            if parsed.tzinfo is not None:
                # API returned a tz-aware string → convert to naive UTC
                show_dt = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                # Naive local time → convert via theater timezone
                show_dt = _local_to_utc(parsed, theater)

            show_id = show.get("id", "")
            tickets_url = (
                f"https://www.tclchinesetheatres.com/order/showtimes/{show_id}/tickets"
                if show_id
                else ""
            )

            # Derive format: use title prefix or IMAX attribute presence
            title_prefix_m = _TITLE_PREFIX_RE.match(raw_title)
            if title_prefix_m:
                format_type = title_prefix_m.group(0).strip("() ")
            elif _IMAX_ATTR_ID in attr_ids:
                format_type = "IMAX"
            else:
                format_type = "Standard"

            movie = self.get_or_create_movie(title)
            if not self._movie_wanted(movie, movie_ids):
                continue

            showtime, is_new = self.upsert_showtime(
                theater,
                movie,
                show_dt,
                tickets_available=not show.get("isSoldOut", False),
                tickets_url=tickets_url,
                format_type=format_type,
            )
            if is_new:
                new_showtimes.append(showtime)

        return new_showtimes
