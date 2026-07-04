import logging
import re
from datetime import datetime, timezone

import requests

from app import db
from app.scrapers.base import (
    REQUEST_TIMEOUT,
    USER_AGENT,
    PlaywrightBatchScraper,
    _scrape_ctx,
    polite_get,
)
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)

_WAIT_MS = 7000
_THEATRE_CODE_RE = re.compile(r"-(\d{4})$")
_BASE_URL = "https://www.regmovies.com"


def _theatre_code_from_url(website: str) -> str:
    """Extract the 4-digit theatre code from a Regal theater URL slug."""
    m = _THEATRE_CODE_RE.search(website.rstrip("/"))
    return m.group(1) if m else ""


def _theatre_code_from_nd(nd: dict, website: str) -> str:
    """Search __NEXT_DATA__ in multiple locations for a 4-digit theatre code.

    Regal's Next.js structure varies by page type; theatreCode may live
    directly in pageProps or nested inside a theater/venue object.
    """
    props = (nd.get("props") or {}).get("pageProps") or {}

    # 1. Direct key (most theater pages)
    code = str(props.get("theatreCode") or "")
    if code.isdigit():
        return code

    # 2. Nested under common object keys
    for obj_key in ("theater", "theatre", "theatreData", "theaterData", "venue"):
        obj = props.get(obj_key) or {}
        if isinstance(obj, dict):
            for code_key in ("theatreCode", "code", "id"):
                val = str(obj.get(code_key) or "")
                if val.isdigit() and len(val) <= 6:
                    return val

    # 3. Fall back to extracting from the URL slug
    return _theatre_code_from_url(website)


def _parse_utc_showtime(utc_str: str) -> datetime | None:
    """
    Parse a Regal UtcShowTime string like '2026-06-16T16:00:00.000Z'
    into a UTC-aware datetime.
    """
    if not utc_str:
        return None
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
    on_demand = getattr(_scrape_ctx, "on_demand", False)
    all_formats = on_demand or getattr(_scrape_ctx, "browse_only", False)

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
                if not all_formats and not any("IMAX" in a.upper() for a in attrs):
                    continue

                show_dt = _parse_utc_showtime(perf.get("UtcShowTime", ""))
                if not show_dt:
                    continue

                perf_id = perf.get("PerformanceId", "")
                tickets_url = ""
                if perf_id and master_code and show_date:
                    api_date = f"{show_date[5:7]}-{show_date[8:10]}-{show_date[:4]}"
                    tickets_url = (
                        f"{_BASE_URL}/buy-tickets"
                        f"/{master_code}/{theatre_code}/{perf_id}/{api_date}"
                    )

                # Use first IMAX attr if present, else first attr, else "Standard"
                imax_attrs = [a for a in attrs if "IMAX" in a.upper()]
                format_type = (imax_attrs or attrs or ["Standard"])[0]

                stop_sales = perf.get("StopSales", False)
                showtime, is_new = scraper.upsert_showtime(
                    theater,
                    movie,
                    show_dt,
                    tickets_available=not stop_sales,
                    tickets_url=tickets_url,
                    format_type=format_type,
                )
                if is_new:
                    new_showtimes.append(showtime)

    return new_showtimes


def _make_session(playwright_cookies: list) -> requests.Session:
    """
    Build a requests.Session seeded with cookies from a Playwright context.

    Cloudflare's cf_clearance cookie is IP+UA bound, so using the same UA
    here lets plain HTTP calls reuse the CF clearance earned by the browser.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _BASE_URL,
    })
    for cookie in playwright_cookies:
        session.cookies.set(
            cookie["name"], cookie["value"], domain=cookie.get("domain", "")
        )
    return session


class RegalScraper(PlaywrightBatchScraper):
    """
    Scraper for Regal Cinemas IMAX showtimes.

    Uses Playwright (headless Chromium) solely to load the theater page and
    bypass Cloudflare's initial JS challenge.  After the page loads, the
    Cloudflare cookies are extracted and handed to a requests.Session, which
    makes all subsequent /api/getShowtimes calls directly — avoiding
    Cloudflare's stricter blocking of in-browser fetch() inside Docker.
    """

    chain_name = "Regal"
    health_website = "regmovies.com"
    _user_agent = USER_AGENT

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
        finally:
            page.close()

        theatre_code = ""
        all_shows: list = []
        dates_with_shows: list = []

        if nd:
            theatre_code = _theatre_code_from_nd(nd, theater.website)
            props = (nd.get("props") or {}).get("pageProps") or {}
            all_shows = list(props.get("showtimes") or [])
            dates_with_shows = props.get("datesWithShows") or []
        else:
            logger.warning("Regal: no __NEXT_DATA__ on %s", theater.website)

        # Fallback: when website is not a regmovies.com URL (e.g. a mall or third-party
        # site) or the code isn't in __NEXT_DATA__, search the Regal theaters listing.
        if not theatre_code:
            logger.info(
                "Regal: theatreCode not found for %s via website — searching regmovies.com",
                theater.name,
            )
            theatre_code = self._find_code_on_regal_site(theater, context)
            if not theatre_code:
                logger.warning(
                    "Regal: could not determine theatreCode for %s", theater.name
                )
                return []
            # We have the code now but no showtimes from the theater page;
            # we'll fetch everything via the requests API below.
            all_shows = []
            dates_with_shows = []

        # Re-snapshot cookies here (after any fallback navigation) so the
        # requests.Session always carries the latest CF-clearance token.
        session = _make_session(context.cookies())

        from datetime import date as _date, timedelta
        browse_only = getattr(_scrape_ctx, "browse_only", False)
        today = _date.today()

        # Always start with whatever dates the page reported as having shows.
        # In browse mode also fill in the full 60-day window in case datesWithShows
        # was truncated (Regal sometimes only puts 1-2 dates in the SSR payload and
        # loads the full schedule client-side, which Playwright misses).
        future_dates: list[str] = [d[:10] for d in dates_with_shows[1:]] if dates_with_shows else []
        if browse_only:
            page_date_set = set(future_dates)
            for i in range(1, 61):
                d = (today + timedelta(days=i)).isoformat()
                if d not in page_date_set:
                    future_dates.append(d)
        elif not future_dates:
            future_dates = [(today + timedelta(days=i)).isoformat() for i in range(1, 15)]

        # When the SSR payload provided no today-showtimes (all_shows is empty —
        # happens on the fallback path where we clear it), include today in the API
        # probe so same-day shows aren't missed.
        if not all_shows and today.isoformat() not in set(future_dates):
            future_dates.insert(0, today.isoformat())

        for date_iso in future_dates:
            shows = self._fetch_date(session, theatre_code, date_iso)
            all_shows.extend(shows)

        return _parse_shows(self, theater, movie_ids, all_shows, theatre_code)

    def _find_code_on_regal_site(self, theater: Theater, context) -> str:
        """Navigate to regmovies.com/theaters and search __NEXT_DATA__ for this theater.

        Used when the theater's stored website URL is not on regmovies.com (mall
        sites, third-party listing pages, etc.) or when the theater page loads but
        doesn't expose theatreCode in its __NEXT_DATA__.
        """
        page = context.new_page()
        try:
            page.goto(f"{_BASE_URL}/theaters", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(_WAIT_MS)
            nd = page.evaluate("window.__NEXT_DATA__")
            if not nd:
                return ""

            props = (nd.get("props") or {}).get("pageProps") or {}
            # Regal's /theaters page stores the full list under several possible keys
            raw = (
                props.get("fullTheatreData")
                or props.get("theaters")
                or props.get("allTheaters")
                or props.get("theatres")
                or props.get("allTheatres")
            )
            # Unwrap if the API wraps the list in a dict (e.g. {"theatres": [...]})
            if isinstance(raw, dict):
                raw = (
                    raw.get("theatres") or raw.get("theaters")
                    or raw.get("theatreList") or raw.get("theaterList")
                    or []
                )
            theaters_list = raw if isinstance(raw, list) else []
            if not theaters_list:
                logger.warning(
                    "Regal: theaters listing page has no theater list; "
                    "pageProps keys: %s",
                    list(props.keys())[:20],
                )
                return ""

            # Match by word overlap between the stored name and the Regal name
            query_words = set(re.sub(r"[^a-z0-9]", " ", theater.name.lower()).split())
            query_words -= {"imax", "and", "the", "regal", "&"}

            for t in theaters_list:
                t_name = (
                    t.get("name") or t.get("theatreName") or t.get("displayName") or ""
                )
                t_words = set(re.sub(r"[^a-z0-9]", " ", t_name.lower()).split())
                t_words -= {"imax", "and", "the", "regal", "&"}
                # Require at least 2 meaningful words in common
                if len(query_words & t_words) >= 2:
                    code = str(
                        t.get("theatreCode") or t.get("code") or t.get("id") or ""
                    )
                    if code.isdigit() and len(code) <= 6:
                        logger.info(
                            "Regal: matched '%s' → '%s' (code %s)",
                            theater.name, t_name, code,
                        )
                        return code
        except Exception as exc:  # noqa: BLE001
            logger.warning("Regal: theaters listing search failed: %s", exc)
        finally:
            page.close()
        return ""

    def _fetch_date(
        self, session: requests.Session, theatre_code: str, date_iso: str
    ) -> list:
        """Call /api/getShowtimes for one date using the CF-authenticated session."""
        parts = date_iso.split("-")
        if len(parts) != 3:
            return []
        api_date = f"{parts[1]}-{parts[2]}-{parts[0]}"
        url = (
            f"{_BASE_URL}/api/getShowtimes"
            f"?theatres={theatre_code}&date={api_date}"
            f"&hoCode=&ignoreCache=false&moviesOnly=false"
        )
        try:
            r = polite_get(
                session, url, timeout=REQUEST_TIMEOUT,
                log_prefix=f"Regal: theatre {theatre_code} on {date_iso}",
            )
        except Exception as exc:
            logger.warning(
                "Regal: getShowtimes failed for %s on %s: %s",
                theatre_code, date_iso, exc,
            )
            return []
        if r is None:
            return []
        if r.ok:
            return r.json().get("shows") or []
        logger.warning(
            "Regal: getShowtimes returned HTTP %s for theatre %s on %s",
            r.status_code, theatre_code, date_iso,
        )
        return []

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Single-theater scrape — launches its own browser."""
        if not theater.website:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning(
                "Regal scraper requires playwright — skipping %s", theater.name
            )
            return []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            try:
                result = self._scrape_with_context(theater, movie_ids, context)
                db.session.commit()
                return result
            finally:
                browser.close()
