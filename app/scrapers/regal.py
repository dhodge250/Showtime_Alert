import logging
import re
from datetime import datetime, timezone

from app import db
from app.scrapers.base import BaseScraper, _get_active_targets
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_WAIT_MS = 7000
_THEATRE_CODE_RE = re.compile(r"-(\d{4})$")


def _theatre_code_from_url(website: str) -> str:
    """Extract the 4-digit theatre code from a Regal theater URL slug."""
    m = _THEATRE_CODE_RE.search(website.rstrip("/"))
    return m.group(1) if m else ""


def _parse_utc_showtime(utc_str: str) -> datetime | None:
    """
    Parse a Regal UtcShowTime string like '2026-06-16T16:00:00.000Z'
    into a UTC-aware datetime.
    """
    if not utc_str:
        return None
    # Strip trailing .000Z or Z before parsing
    clean = utc_str.rstrip("Z")
    if "." in clean:
        clean = clean.split(".")[0]
    try:
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_shows(
    scraper: "RegalScraper",
    theater: Theater,
    movie_ids: set,
    shows: list,
    theatre_code: str,
) -> list[Showtime]:
    """Parse Regal show entries and return newly inserted Showtime rows."""
    new_showtimes: list[Showtime] = []
    for day_entry in shows:
        show_date = (day_entry.get("AdvertiseShowDate") or "")[:10]
        for film in day_entry.get("Film", []):
            title = film.get("Title", "")
            if not title:
                continue
            master_code = (film.get("MasterMovieCode") or "").lower()

            movie = scraper.get_or_create_movie(title)
            if not scraper._movie_wanted(movie, movie_ids):
                continue

            for perf in film.get("Performances", []):
                attrs = perf.get("PerformanceAttributes") or []
                if not any("IMAX" in a.upper() for a in attrs):
                    continue

                show_dt = _parse_utc_showtime(perf.get("UtcShowTime", ""))
                if not show_dt:
                    continue

                # Regal ticket URL: /buy-tickets/{hoCode}/{theatreCode}/{perfId}/{MM-DD-YYYY}
                perf_id = perf.get("PerformanceId", "")
                tickets_url = ""
                if perf_id and master_code and show_date:
                    api_date = f"{show_date[5:7]}-{show_date[8:10]}-{show_date[:4]}"
                    tickets_url = (
                        f"https://www.regmovies.com/buy-tickets"
                        f"/{master_code}/{theatre_code}/{perf_id}/{api_date}"
                    )

                stop_sales = perf.get("StopSales", False)
                showtime, is_new = scraper.upsert_showtime(
                    theater,
                    movie,
                    show_dt,
                    tickets_available=not stop_sales,
                    tickets_url=tickets_url,
                    format_type="IMAX",
                )
                if is_new:
                    new_showtimes.append(showtime)

    return new_showtimes


class RegalScraper(BaseScraper):
    """
    Scraper for Regal Cinemas IMAX showtimes.

    Uses Playwright (headless Chromium) to bypass Cloudflare bot protection on
    regmovies.com. After the initial page load, the /api/getShowtimes endpoint
    is called from within the browser context for each upcoming date.
    """

    chain_name = "Regal"

    def scrape_all(self) -> list[Showtime]:
        """Share one Playwright browser across all Regal theater scrapes."""
        targets = _get_active_targets()
        if not targets:
            logger.debug("Regal: no active alerts — skipping scrape")
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
            logger.warning("Regal scraper requires playwright — skipping")
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

    def _scrape_with_context(
        self, theater: Theater, movie_ids: set, context
    ) -> list[Showtime]:
        if not theater.website:
            return []

        page = context.new_page()
        try:
            page.goto(theater.website, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(_WAIT_MS)

            nd = page.evaluate("window.__NEXT_DATA__")
            if not nd:
                logger.warning("Regal: no __NEXT_DATA__ on %s", theater.website)
                return []

            props = nd.get("props", {}).get("pageProps", {})
            theatre_code = str(props.get("theatreCode") or _theatre_code_from_url(theater.website))
            if not theatre_code:
                logger.warning("Regal: could not determine theatreCode for %s", theater.name)
                return []

            # Today's showtimes are embedded in __NEXT_DATA__
            all_shows: list = list(props.get("showtimes") or [])

            # Fetch upcoming dates via /api/getShowtimes (called inside browser context)
            dates_with_shows = props.get("datesWithShows") or []
            for date_iso in [d[:10] for d in dates_with_shows[1:]]:
                shows = self._fetch_date(page, theatre_code, date_iso)
                all_shows.extend(shows)

            return _parse_shows(self, theater, movie_ids, all_shows, theatre_code)
        finally:
            page.close()

    def _fetch_date(self, page, theatre_code: str, date_iso: str) -> list:
        """Call /api/getShowtimes for one date from within the browser context."""
        # Convert YYYY-MM-DD to MM-DD-YYYY required by the API
        parts = date_iso.split("-")
        if len(parts) != 3:
            return []
        api_date = f"{parts[1]}-{parts[2]}-{parts[0]}"
        api_url = (
            f"/api/getShowtimes?theatres={theatre_code}"
            f"&date={api_date}&hoCode=&ignoreCache=false&moviesOnly=false"
        )
        try:
            result = page.evaluate(
                f"""
                async () => {{
                    const resp = await fetch("{api_url}");
                    return resp.ok ? await resp.json() : null;
                }}
                """
            )
            if result:
                return result.get("shows") or []
        except Exception as exc:
            logger.warning("Regal: getShowtimes failed for %s on %s: %s", theatre_code, date_iso, exc)
        return []

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Single-theater scrape — launches its own browser."""
        if not theater.website:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Regal scraper requires playwright — skipping %s", theater.name)
            return []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_UA, locale="en-US")
            try:
                result = self._scrape_with_context(theater, movie_ids, context)
                db.session.commit()
                return result
            finally:
                browser.close()
