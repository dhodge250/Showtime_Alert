import logging
import re

from bs4 import BeautifulSoup

from app import db
from app.scrapers.base import BaseScraper, _get_active_targets, _local_to_utc, _parse_time_text, _scrape_ctx
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_WAIT_MS = 7000
_IMAX_ARIA = re.compile(r"IMAX", re.I)
_SECTION_PREFIX = "Showtimes for "
_SECTION_ARIA = re.compile(r"^Showtimes for ", re.I)
# AMC aria-labels look like "IMAX at AMC Showtimes" or "Dolby Cinema at AMC Showtimes"
_AMC_FORMAT_STRIP_RE = re.compile(r"\s+at\s+amc\b.*$", re.I)


def _amc_format_label(aria_label: str) -> str:
    """Extract the format name from an AMC li aria-label."""
    return _AMC_FORMAT_STRIP_RE.sub("", aria_label).strip() or aria_label


def _showtimes_url(website: str) -> str:
    url = website.rstrip("/")
    if not url.endswith("/showtimes"):
        url += "/showtimes"
    return url


def _parse_page(theater: Theater, movie_ids: set, soup: BeautifulSoup, scraper: "AMCScraper") -> list[Showtime]:
    new_showtimes: list[Showtime] = []
    on_demand = getattr(_scrape_ctx, "on_demand", False)

    for section in soup.find_all("section", attrs={"aria-label": _SECTION_ARIA}):
        title = section["aria-label"][len(_SECTION_PREFIX):]
        if not title:
            continue

        img = section.find("img", src=re.compile(r"cloudinary\.com|amc-cdn", re.I))
        image_url = img["src"] if img and img.get("src") else ""

        movie = scraper.get_or_create_movie(title, image_url=image_url)
        if not scraper._movie_wanted(movie, movie_ids):
            continue

        # In on-demand mode scrape all format sections; otherwise only IMAX
        format_lis = (
            section.find_all("li", attrs={"aria-label": True})
            if on_demand
            else [section.find("li", attrs={"aria-label": _IMAX_ARIA})]
        )

        for format_li in format_lis:
            if not format_li:
                continue
            # In alert mode we're always in the IMAX li → hardcode "IMAX".
            # In on-demand mode normalize the aria-label to a clean format name.
            format_type = (
                _amc_format_label(format_li.get("aria-label", "IMAX"))
                if on_demand else "IMAX"
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


class AMCScraper(BaseScraper):
    """Scraper for AMC Theatres IMAX showtimes."""

    chain_name = "AMC"
    health_website = "amctheatres.com"

    def scrape_all(self) -> list[Showtime]:
        """Share one Playwright browser across all AMC theater scrapes."""
        targets = _get_active_targets()
        if not targets:
            logger.debug("AMC: no active alerts — skipping scrape")
            return []

        query = Theater.query.filter_by(chain=self.chain_name, is_active=True)
        if None not in targets:
            query = query.filter(Theater.id.in_(targets.keys()))
        theaters = query.all()
        if not theaters:
            return []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("AMC scraper requires playwright — skipping")
            return []

        new_showtimes: list[Showtime] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_UA, locale="en-US")
            for theater in theaters:
                movie_ids: set = set()
                if None in targets:
                    movie_ids |= targets[None]
                if theater.id in targets:
                    movie_ids |= targets[theater.id]
                if not movie_ids:
                    continue
                try:
                    new_showtimes.extend(
                        self._scrape_with_context(theater, movie_ids, context)
                    )
                except Exception as exc:
                    logger.error("Error scraping %s: %s", theater.name, exc)
            browser.close()

        db.session.commit()
        return new_showtimes

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
            context = browser.new_context(user_agent=_UA, locale="en-US")
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(_WAIT_MS)
                soup = BeautifulSoup(page.content(), "lxml")
            finally:
                page.close()
                browser.close()

        return _parse_page(theater, movie_ids, soup, self)
