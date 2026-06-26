"""
Scraper for Cinemark IMAX showtimes.

Cinemark's website is server-side rendered and accessible via plain requests
(no Cloudflare or bot protection). Multi-date coverage is achieved by:
  1. Fetching the theater page once to get the theater ID and all available
     dates from the showdate carousel.
  2. Parsing today's IMAX showtimes directly from the main page HTML.
  3. For each additional date, calling the internal GetByTheaterId endpoint
     which returns an HTML fragment with that date's showtimes.

No Playwright / headless browser needed.
"""

import logging
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from app import db
from app.scrapers.base import BaseScraper, _local_to_utc, _scrape_ctx
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_API_HEADERS = {
    **_PAGE_HEADERS,
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
}
_SHOWTIMES_API = (
    "https://www.cinemark.com/umbraco/surface/Showtimes/GetByTheaterId"
)
_THEATER_ID_RE = re.compile(r"var\s+currentTheaterId\s*=\s*(\d+)")
_REQUEST_TIMEOUT = 15


def _extract_theater_id(soup: BeautifulSoup) -> str:
    for script in soup.find_all("script"):
        text = script.string or ""
        m = _THEATER_ID_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _parse_showtime_dt(
    date_iso: str, time_text: str, theater: "Theater | None" = None
) -> datetime | None:
    """
    Combine a YYYY-MM-DD local date+time string and return naive UTC.

    When theater is provided its state/country is used to determine the local
    timezone for the UTC conversion.  When None the naive local time is
    returned as-is (UTC assumed — only used in tests without a theater object).
    """
    clean = time_text.strip().upper()
    for fmt in ("%I:%M%p", "%I:%M %p"):
        try:
            naive_local = datetime.strptime(f"{date_iso} {clean}", f"%Y-%m-%d {fmt}")
            return _local_to_utc(naive_local, theater) if theater is not None else naive_local
        except ValueError:
            continue
    return None


def _parse_imax_showtimes(
    scraper: "CinemarkScraper",
    theater: Theater,
    movie_ids: set,
    soup: BeautifulSoup,
    date_iso: str,
) -> list[Showtime]:
    """Extract showtimes from a Cinemark HTML page or API fragment."""
    new_showtimes: list[Showtime] = []
    on_demand = getattr(_scrape_ctx, "on_demand", False)

    for block in soup.select("div.showtimeMovieBlock"):
        title_el = block.find("h3") or block.find("h2")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        img = block.find("img", class_="img-responsive")
        image_url = (
            (img.get("data-srcset") or img.get("src") or "") if img else ""
        )

        movie = scraper.get_or_create_movie(title, image_url=image_url)
        if not scraper._movie_wanted(movie, movie_ids):
            continue

        for show_div in block.select("div.showtime"):
            ptype = show_div.get("data-print-type-name", "")
            if not on_demand and "IMAX" not in ptype.upper():
                continue

            # Past showtimes render as <p class="off past"> with no link
            link = show_div.find("a", class_="showtime-link")
            if not link:
                continue

            time_text = link.get_text(strip=True)
            show_dt = _parse_showtime_dt(date_iso, time_text, theater)
            if not show_dt:
                continue

            href = link.get("href", "")
            tickets_url = f"https://www.cinemark.com{href}" if href else ""

            format_type = ptype if ptype else "Standard"

            showtime, is_new = scraper.upsert_showtime(
                theater,
                movie,
                show_dt,
                tickets_available=True,
                tickets_url=tickets_url,
                format_type=format_type,
            )
            if is_new:
                new_showtimes.append(showtime)

    return new_showtimes


class CinemarkScraper(BaseScraper):
    """Scraper for Cinemark IMAX showtimes."""

    chain_name = "Cinemark"
    health_website = "www.cinemark.com/"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        if not theater.website:
            return []

        # One session per theater keeps cookies consistent across all date requests,
        # which helps avoid 429s from Cinemark's rate limiter.
        session = requests.Session()
        session.headers.update(_PAGE_HEADERS)

        soup = self._fetch_page(session, theater.website)
        if not soup:
            return []

        theater_id = _extract_theater_id(soup)
        if not theater_id:
            logger.warning("Cinemark: could not find theaterId for %s", theater.name)
            return []

        date_links = soup.select("a.showdate-link[data-datevalue]")
        if not date_links:
            logger.warning("Cinemark: no showdate carousel entries for %s", theater.name)
            return []

        dates = [a["data-datevalue"] for a in date_links]

        new_showtimes: list[Showtime] = []

        # Today's data is already in the main page
        new_showtimes.extend(
            _parse_imax_showtimes(self, theater, movie_ids, soup, dates[0])
        )

        # Fetch each remaining date via the GetByTheaterId API endpoint.
        # A short inter-request delay avoids triggering the rate limiter.
        for date_iso in dates[1:]:
            time.sleep(0.5)
            date_soup = self._fetch_date(session, theater_id, date_iso, theater.website)
            if date_soup:
                new_showtimes.extend(
                    _parse_imax_showtimes(self, theater, movie_ids, date_soup, date_iso)
                )

        return new_showtimes

    def _fetch_page(self, session: requests.Session, url: str) -> BeautifulSoup | None:
        try:
            r = session.get(url, timeout=_REQUEST_TIMEOUT)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except requests.RequestException as exc:
            logger.warning("Cinemark: failed to fetch %s: %s", url, exc)
            return None

    def _fetch_date(
        self, session: requests.Session, theater_id: str, date_iso: str, referer: str
    ) -> BeautifulSoup | None:
        url = f"{_SHOWTIMES_API}?theaterId={theater_id}&showDate={date_iso}"
        headers = {**_API_HEADERS, "Referer": referer}
        for attempt in range(3):
            try:
                r = session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 10))
                    logger.warning(
                        "Cinemark: rate-limited for theater %s on %s — waiting %ds (attempt %d/3)",
                        theater_id, date_iso, wait, attempt + 1,
                    )
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return BeautifulSoup(r.text, "lxml")
            except requests.RequestException as exc:
                logger.warning(
                    "Cinemark: GetByTheaterId failed for theater %s on %s: %s",
                    theater_id, date_iso, exc,
                )
                return None
        logger.warning(
            "Cinemark: gave up on theater %s on %s after 3 rate-limited attempts",
            theater_id, date_iso,
        )
        return None
