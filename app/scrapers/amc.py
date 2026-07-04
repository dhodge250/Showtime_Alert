import logging
import re

from bs4 import BeautifulSoup

from app.scrapers.base import (
    USER_AGENT,
    PlaywrightBatchScraper,
    _local_to_utc,
    _parse_time_text,
    _scrape_ctx,
)
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_WAIT_MS = 7000
_IMAX_ARIA = re.compile(r"IMAX", re.I)
_SECTION_PREFIX = "Showtimes for "
_SECTION_ARIA = re.compile(r"^Showtimes for ", re.I)
# AMC aria-labels: "IMAX at AMC Showtimes", "Dolby Cinema at AMC Showtimes",
# "RealD 3D Showtimes", "Fan Faves Showtimes", etc.
# Strip "at AMC ..." (covers the main format groups) OR trailing " Showtimes".
_AMC_FORMAT_STRIP_RE = re.compile(r"\s+at\s+amc\b.*$|\s+showtimes?\s*$", re.I)


def _amc_format_label(aria_label: str) -> str:
    """Extract the screen-format name from an AMC li aria-label."""
    return _AMC_FORMAT_STRIP_RE.sub("", aria_label).strip() or aria_label


def _showtimes_url(website: str) -> str:
    url = website.rstrip("/")
    if not url.endswith("/showtimes"):
        url += "/showtimes"
    return url


def _parse_page(theater: Theater, movie_ids: set, soup: BeautifulSoup, scraper: "AMCScraper") -> list[Showtime]:
    new_showtimes: list[Showtime] = []
    on_demand = getattr(_scrape_ctx, "on_demand", False)
    # Browse-schedule scrapes collect all formats for discovery, same as on-demand
    all_formats = on_demand or getattr(_scrape_ctx, "browse_only", False)

    for section in soup.find_all("section", attrs={"aria-label": _SECTION_ARIA}):
        title = section["aria-label"][len(_SECTION_PREFIX):]
        if not title:
            continue

        img = section.find("img", src=re.compile(r"cloudinary\.com|amc-cdn", re.I))
        image_url = img["src"] if img and img.get("src") else ""

        movie = scraper.get_or_create_movie(title, image_url=image_url)
        if not scraper._movie_wanted(movie, movie_ids):
            continue

        # In all-formats mode scrape every format section; otherwise only IMAX
        format_lis = (
            section.find_all("li", attrs={"aria-label": True})
            if all_formats
            else [section.find("li", attrs={"aria-label": _IMAX_ARIA})]
        )

        for format_li in format_lis:
            if not format_li:
                continue
            aria = format_li.get("aria-label", "")
            # AMC sometimes injects placeholder li elements with "undefined" in
            # the label before the page fully hydrates — skip them.
            if "undefined" in aria.lower():
                continue
            # In alert mode we're always in the IMAX li → hardcode "IMAX".
            # In all-formats mode normalize the aria-label to a clean format name.
            format_type = (
                _amc_format_label(aria or "IMAX")
                if all_formats else "IMAX"
            )

            showtime_ul = format_li.find("ul", attrs={"aria-label": "Showtime Group Results"})
            if not showtime_ul:
                continue

            for link in showtime_ul.find_all("a"):
                time_text = link.get_text(strip=True)
                naive_local = _parse_time_text(time_text)
                if not naive_local:
                    continue
                show_dt = _local_to_utc(naive_local, theater)
                href = link.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.amctheatres.com" + href
                showtime, is_new = scraper.upsert_showtime(
                    theater, movie, show_dt, tickets_url=href, format_type=format_type
                )
                if is_new:
                    new_showtimes.append(showtime)

    return new_showtimes


class AMCScraper(PlaywrightBatchScraper):
    """Scraper for AMC Theatres IMAX showtimes."""

    chain_name = "AMC"
    health_website = "amctheatres.com"
    _user_agent = USER_AGENT

    def _scrape_with_context(self, theater: Theater, movie_ids: set, context) -> list[Showtime]:
        if not theater.website:
            return []
        url = _showtimes_url(theater.website)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(_WAIT_MS)
            soup = BeautifulSoup(page.content(), "lxml")
        finally:
            page.close()
        return _parse_page(theater, movie_ids, soup, self)

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Single-theater scrape — launches its own browser."""
        if not theater.website:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("AMC scraper requires playwright — skipping %s", theater.name)
            return []

        url = _showtimes_url(theater.website)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(_WAIT_MS)
                soup = BeautifulSoup(page.content(), "lxml")
            finally:
                page.close()
                browser.close()

        return _parse_page(theater, movie_ids, soup, self)
