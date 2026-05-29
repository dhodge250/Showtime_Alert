"""
IMAX theater web scraper.

Crawls IMAX theater chain websites to discover movies and showtimes.
Each chain has its own scraper class. Results are persisted to the database.

Scraping is demand-driven: only theaters and movies that have at least one
active, unsent AlertPreference are scraped.  Once an alert fires (alert_sent=True)
the corresponding movie/theater combination stops being scraped until a new
alert is created.
"""
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app import db
from app.models import AlertMovie, AlertPreference, Movie, Showtime, Theater

logger = logging.getLogger(__name__)

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

def _get_active_targets() -> tuple[set[int], set[int]]:
    """
    Return (theater_id_set, movie_id_set) for all active, unsent alerts.

    movie_id_set is built from AlertMovie rows (new model) for prefs that still
    have unsent movies.  A None sentinel in movie_id_set means "any movie" —
    present when at least one active pref has zero AlertMovie rows.

    theater_id_set: None sentinel = "any theater" (alert has theater_id=None).
    """
    active_prefs = AlertPreference.query.filter_by(is_active=True, alert_sent=False).all()

    theater_ids: set = set()
    movie_ids: set = set()

    for pref in active_prefs:
        theater_ids.add(pref.theater_id)  # None = any theater

        am_count = pref.alert_movies.count()
        if am_count == 0:
            # "Any movie" alert — sentinel: scrape everything at this theater
            movie_ids.add(None)
        else:
            # Add only unsent movie IDs
            for am in pref.alert_movies.filter_by(alert_sent=False).all():
                movie_ids.add(am.movie_id)

    return theater_ids, movie_ids


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

        If no active alerts exist at all, no scraping is performed.  This keeps
        the scraper idle when no users are waiting for notifications, and stops
        scraping a movie/theater once its alert has been dispatched.
        """
        theater_ids, movie_ids = _get_active_targets()

        if not theater_ids and not movie_ids:
            logger.debug("%s: no active alerts — skipping scrape", self.chain_name)
            return []

        # Build theater query for this chain
        query = Theater.query.filter_by(chain=self.chain_name, is_active=True)

        # If None is NOT in theater_ids, we have an explicit list to filter by
        if None not in theater_ids:
            query = query.filter(Theater.id.in_(theater_ids))
        # If None IS in theater_ids, at least one alert has no theater filter
        # meaning any theater might be relevant — scrape all active theaters.

        theaters = query.all()
        if not theaters:
            return []

        new_showtimes: list[Showtime] = []
        for theater in theaters:
            try:
                results = self.scrape_theater(theater, movie_ids)
                new_showtimes.extend(results)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error scraping %s: %s", theater.name, exc)
        db.session.commit()
        return new_showtimes


class AMCScraper(BaseScraper):
    """Scraper for AMC Theatres IMAX showtimes."""

    chain_name = "AMC"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Scrape showtimes for one AMC theater and return newly inserted rows."""
        new_showtimes: list[Showtime] = []
        if not theater.website:
            return new_showtimes

        soup = self.fetch(theater.website)
        if not soup:
            return new_showtimes

        # AMC showtime pages list movies with show dates.
        # The selector paths below target AMC's public HTML structure.
        movie_sections = soup.select("div.ShowtimesByDate")
        if not movie_sections:
            # Fallback: look for any movie title links
            movie_sections = soup.select("div[class*='movie']")

        for section in movie_sections:
            title_tag = section.select_one("h2, h3, [class*='movieTitle']")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if not title:
                continue

            img_tag = section.select_one("img")
            image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

            movie = self.get_or_create_movie(title, image_url=image_url)
            if not self._movie_wanted(movie, movie_ids):
                continue

            time_links = section.select("a[class*='showtime'], a[class*='Showtime']")
            for link in time_links:
                time_text = link.get_text(strip=True)
                show_dt = _parse_time_text(time_text)
                if not show_dt:
                    continue
                tickets_url = link.get("href", "")
                if tickets_url and not tickets_url.startswith("http"):
                    tickets_url = "https://www.amctheatres.com" + tickets_url
                showtime, is_new = self.upsert_showtime(
                    theater, movie, show_dt, tickets_url=tickets_url, format_type="IMAX"
                )
                if is_new:
                    new_showtimes.append(showtime)

        return new_showtimes


class RegalScraper(BaseScraper):
    """Scraper for Regal Cinemas IMAX showtimes."""

    chain_name = "Regal"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Scrape showtimes for one Regal theater and return newly inserted rows."""
        new_showtimes: list[Showtime] = []
        if not theater.website:
            return new_showtimes

        soup = self.fetch(theater.website)
        if not soup:
            return new_showtimes

        for film in soup.select("div.film-info, article[class*='movie']"):
            title_tag = film.select_one("h2, h3, .film-title")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            img_tag = film.select_one("img")
            image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

            movie = self.get_or_create_movie(title, image_url=image_url)
            if not self._movie_wanted(movie, movie_ids):
                continue

            for time_tag in film.select("a.showtime-anchor, .showtime-link"):
                time_text = time_tag.get_text(strip=True)
                show_dt = _parse_time_text(time_text)
                if not show_dt:
                    continue
                tickets_url = time_tag.get("href", "")
                showtime, is_new = self.upsert_showtime(
                    theater, movie, show_dt, tickets_url=tickets_url, format_type="IMAX"
                )
                if is_new:
                    new_showtimes.append(showtime)

        return new_showtimes


class CinemarkScraper(BaseScraper):
    """Scraper for Cinemark IMAX showtimes."""

    chain_name = "Cinemark"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Scrape showtimes for one Cinemark theater and return newly inserted rows."""
        new_showtimes: list[Showtime] = []
        if not theater.website:
            return new_showtimes

        soup = self.fetch(theater.website)
        if not soup:
            return new_showtimes

        for film in soup.select("div.movie-container, div[class*='MovieCard']"):
            title_tag = film.select_one("h2, h3, .movie-title")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            img_tag = film.select_one("img")
            image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

            movie = self.get_or_create_movie(title, image_url=image_url)
            if not self._movie_wanted(movie, movie_ids):
                continue

            for time_tag in film.select("button.showtime-btn, a[class*='showtime']"):
                time_text = time_tag.get_text(strip=True)
                show_dt = _parse_time_text(time_text)
                if not show_dt:
                    continue
                tickets_url = time_tag.get("href", "")
                showtime, is_new = self.upsert_showtime(
                    theater, movie, show_dt, tickets_url=tickets_url, format_type="IMAX"
                )
                if is_new:
                    new_showtimes.append(showtime)

        return new_showtimes


class TCLScraper(BaseScraper):
    """Scraper for TCL Chinese Theatre IMAX showtimes."""

    chain_name = "TCL"

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        """Scrape showtimes for the TCL Chinese Theatre and return newly inserted rows."""
        new_showtimes: list[Showtime] = []
        if not theater.website:
            return new_showtimes

        soup = self.fetch(theater.website)
        if not soup:
            return new_showtimes

        for film in soup.select("div.movie-listing, div[class*='film']"):
            title_tag = film.select_one("h2, h3, .title")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)

            img_tag = film.select_one("img")
            image_url = img_tag["src"] if img_tag and img_tag.get("src") else ""

            movie = self.get_or_create_movie(title, image_url=image_url)
            if not self._movie_wanted(movie, movie_ids):
                continue

            for time_tag in film.select("a[class*='time'], button[class*='time']"):
                time_text = time_tag.get_text(strip=True)
                show_dt = _parse_time_text(time_text)
                if not show_dt:
                    continue
                tickets_url = time_tag.get("href", "")
                showtime, is_new = self.upsert_showtime(
                    theater, movie, show_dt, tickets_url=tickets_url, format_type="IMAX with Laser"
                )
                if is_new:
                    new_showtimes.append(showtime)

        return new_showtimes


class CineplexScraper(BaseScraper):
    """
    Scraper for Cineplex IMAX showtimes (Canada).

    Uses the Cineplex theatrical REST API (apis.cineplex.com) directly with
    requests — no browser automation needed. The API key is a public
    subscription key embedded in the Cineplex website JS bundle.

    Flow per theater:
      1. Fetch __NEXT_DATA__ from theater.website to get the Cineplex locationId.
      2. Call dates/bookable to find dates with showtimes.
      3. For each upcoming date, call showtimes and filter for IMAX experiences.
    """

    chain_name = "Cineplex"

    _API_BASE = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"
    _API_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "ocp-apim-subscription-key": "dcdac5601d864addbc2675a2e96cb1f8",
        "referer": "https://www.cineplex.com/",
        "cctoken": "undefined",
    }
    # How many days ahead to scrape (matching the bookable window we care about).
    _DAYS_AHEAD = 14

    def scrape_theater(self, theater: "Theater", movie_ids: set) -> list["Showtime"]:
        new_showtimes: list = []
        if not theater.website:
            return new_showtimes

        location_id = self._get_location_id(theater)
        if not location_id:
            return new_showtimes

        bookable_dates = self._get_bookable_dates(location_id)
        today = date.today()
        upcoming = [
            d for d in bookable_dates
            if 0 <= (date.fromisoformat(d) - today).days < self._DAYS_AHEAD
        ]

        logger.info(
            "Cineplex: %s — locationId=%s, %d bookable dates in next %d days",
            theater.name, location_id, len(upcoming), self._DAYS_AHEAD,
        )

        for date_iso in upcoming:
            found = self._scrape_date(theater, location_id, date_iso, movie_ids)
            new_showtimes.extend(found)

        return new_showtimes

    def _get_location_id(self, theater: "Theater") -> "int | None":
        """Return the Cineplex locationId (theatreId) from __NEXT_DATA__."""
        try:
            soup = self.fetch(theater.website)
            if not soup:
                return None
            tag = soup.find("script", {"id": "__NEXT_DATA__"})
            if not tag:
                logger.warning("Cineplex: no __NEXT_DATA__ on %s", theater.website)
                return None
            nd = json.loads(tag.string)
            return nd["props"]["pageProps"]["theatreDetails"]["theatreId"]
        except Exception as exc:
            logger.error("Cineplex: failed to get locationId for %s: %s", theater.name, exc)
            return None

    def _get_bookable_dates(self, location_id: int) -> list[str]:
        """Return YYYY-MM-DD strings for dates that have bookable showtimes."""
        try:
            r = requests.get(
                f"{self._API_BASE}/dates/bookable?locationId={location_id}",
                headers=self._API_HEADERS, timeout=10,
            )
            if r.status_code != 200:
                logger.warning("Cineplex: dates/bookable returned %s", r.status_code)
                return []
            return [d[:10] for d in r.json()]
        except Exception as exc:
            logger.error("Cineplex: dates/bookable failed: %s", exc)
            return []

    def _scrape_date(
        self,
        theater: "Theater",
        location_id: int,
        date_iso: str,
        movie_ids: set,
    ) -> list["Showtime"]:
        """Fetch IMAX showtimes for one theater on one date."""
        from urllib.parse import quote

        new_showtimes: list = []
        try:
            d = date.fromisoformat(date_iso)
            date_param = quote(f"{d.month}/{d.day}/{d.year}", safe="")
            url = (
                f"{self._API_BASE}/showtimes"
                f"?language=en&locationId={location_id}&date={date_param}"
            )
            r = requests.get(url, headers=self._API_HEADERS, timeout=15)
            if r.status_code != 200:
                return []

            data = r.json()
            if not data:
                return []

            for day_entry in data[0].get("dates", []):
                for movie in day_entry.get("movies", []):
                    title = movie.get("name", "")
                    if not title:
                        continue
                    for exp in movie.get("experiences", []):
                        types = exp.get("experienceTypes", [])
                        if not any("IMAX" in t.upper() for t in types):
                            continue
                        for session in exp.get("sessions", []):
                            if session.get("isInThePast"):
                                continue
                            start = session.get("showStartDateTime", "")
                            show_dt = _parse_time_text(start)
                            if not show_dt:
                                continue
                            movie_obj = self.get_or_create_movie(title)
                            if not self._movie_wanted(movie_obj, movie_ids):
                                continue
                            tickets_url = (
                                session.get("ticketingUrl")
                                or session.get("ticketingRedesignUrl")
                                or ""
                            )
                            showtime, is_new = self.upsert_showtime(
                                theater, movie_obj, show_dt,
                                tickets_url=tickets_url,
                                format_type="IMAX",
                            )
                            if is_new:
                                new_showtimes.append(showtime)
        except Exception as exc:
            logger.error(
                "Cineplex: showtimes fetch failed for %s on %s: %s",
                theater.name, date_iso, exc,
            )
        return new_showtimes


class RoyalBCMuseumScraper(BaseScraper):
    """
    Scraper for IMAX Victoria at the Royal BC Museum.

    The ticketing site runs on the ATMS (Vantix) platform.  Each film on the
    listing page either exposes its showtimes inline (1–3 dates) or links to a
    separate calendar page (/DateSelection.aspx?item=NNN) when many dates exist.

    Theater record requirements
    ---------------------------
    chain   : "Royal BC Museum"   (must match chain_name below)
    website : https://sales.royalbcmuseum.bc.ca/Default.aspx?tagid=3
    """

    chain_name = "Royal BC Museum"
    BASE_URL = "https://sales.royalbcmuseum.bc.ca"

    # ------------------------------------------------------------------ #
    # Internal datetime parser for ATMS-specific formats                  #
    # ------------------------------------------------------------------ #

    def _parse_atms_dt(self, text: str) -> Optional[datetime]:
        """
        Parse ATMS date+time strings into a UTC-aware datetime.

        Two formats are encountered:
        - Calendar page  data-scheduleDate : "Friday June 5, 2026 - 7:15 PM"
        - Listing page   link text         : "7:15 PM - May 28, 2026"
        """
        text = text.strip()
        for fmt in (
            "%A %B %d, %Y - %I:%M %p",   # calendar: "Friday June 5, 2026 - 7:15 PM"
            "%I:%M %p - %b %d, %Y",       # listing:  "7:15 PM - May 28, 2026"
            "%I:%M %p - %B %d, %Y",       # listing:  "7:15 PM - May 28, 2026" (full month)
        ):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Showtime helpers                                                     #
    # ------------------------------------------------------------------ #

    def _showtimes_from_calendar(self, item_href: str, theater, movie) -> list:
        """Fetch the ?v=All calendar page for an item and return new Showtime objects."""
        new = []
        cal_url = self.BASE_URL + item_href
        cal_url += "&v=All" if "?" in cal_url else "?v=All"

        cal_soup = self.fetch(cal_url)
        if not cal_soup:
            return new

        for ev in cal_soup.select("div#CalendarContainer div.EventListing"):
            ticket_a = ev.select_one("a.PrimaryAction.js-select-date")
            if not ticket_a:
                continue
            date_str = ticket_a.get("data-scheduledate", "")
            show_dt = self._parse_atms_dt(date_str)
            if not show_dt:
                logger.debug("RoyalBCMuseum: could not parse calendar date %r", date_str)
                continue
            tickets_url = self.BASE_URL + ticket_a["href"]
            st, is_new = self.upsert_showtime(
                theater, movie, show_dt,
                tickets_url=tickets_url, format_type="IMAX",
            )
            if is_new:
                new.append(st)
        return new

    def _showtimes_from_inline(self, button_links, theater, movie) -> list:
        """Parse inline listing-page show links (e.g. '7:15 PM - May 28, 2026')."""
        new = []
        for link in button_links:
            href = link.get("href", "")
            link_text = link.get_text(strip=True)
            show_dt = self._parse_atms_dt(link_text)
            if not show_dt:
                logger.debug("RoyalBCMuseum: could not parse inline date %r", link_text)
                continue
            tickets_url = (self.BASE_URL + href) if href.startswith("/") else href
            st, is_new = self.upsert_showtime(
                theater, movie, show_dt,
                tickets_url=tickets_url, format_type="IMAX",
            )
            if is_new:
                new.append(st)
        return new

    # ------------------------------------------------------------------ #
    # Main entry point                                                     #
    # ------------------------------------------------------------------ #

    def scrape_theater(self, theater: Theater, movie_ids: set) -> list:
        new_showtimes: list = []
        if not theater.website:
            return new_showtimes

        soup = self.fetch(theater.website)
        if not soup:
            return new_showtimes

        # Only process listings inside #LeftSide to avoid sidebar noise
        for listing in soup.select("div#LeftSide div.EventListing"):
            h2 = listing.select_one("h2")
            if not h2:
                continue

            raw_title = h2.get_text(strip=True)
            # Strip "IMAX: " prefix so TMDB matching works cleanly
            title = re.sub(r"^IMAX:\s*", "", raw_title, flags=re.IGNORECASE).strip()
            if not title:
                continue

            img_tag = listing.select_one("img")
            image_url = ""
            if img_tag and img_tag.get("src"):
                src = img_tag["src"]
                image_url = src if src.startswith("http") else self.BASE_URL + src

            movie = self.get_or_create_movie(title, image_url=image_url)
            if not self._movie_wanted(movie, movie_ids):
                continue

            button_links = listing.select("div.ButtonArea a.PrimaryAction")
            # Partition links: calendar links vs inline showtime links
            calendar_links = [a for a in button_links if "DateSelection.aspx" in a.get("href", "")]
            inline_links   = [a for a in button_links if "DateSelection.aspx" not in a.get("href", "")]

            for cal_link in calendar_links:
                new_showtimes.extend(
                    self._showtimes_from_calendar(cal_link["href"], theater, movie)
                )

            if inline_links:
                new_showtimes.extend(
                    self._showtimes_from_inline(inline_links, theater, movie)
                )

        return new_showtimes


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
    Attempt to parse a showtime text string into a datetime.

    Tries multiple common formats used by theater chains.
    Returns None if parsing fails.
    """
    text = text.strip().upper()
    # Formats that include a date component
    date_formats = [
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    # Formats that have only a time component (no year/month/day)
    time_only_formats = [
        "%I:%M %p",
        "%I:%M%p",
        "%H:%M",
        "%I %p",
    ]
    today = datetime.now(timezone.utc).date()

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    for fmt in time_only_formats:
        try:
            parsed = datetime.strptime(text, fmt)
            # Attach today's date and UTC timezone
            parsed = parsed.replace(
                year=today.year, month=today.month, day=today.day, tzinfo=timezone.utc
            )
            return parsed
        except ValueError:
            continue

    return None


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


ALL_SCRAPERS: list[BaseScraper] = [
    AMCScraper(),
    RegalScraper(),
    CinemarkScraper(),
    TCLScraper(),
    RoyalBCMuseumScraper(),
    CineplexScraper(),
]


def run_all_scrapers() -> list[Showtime]:
    """Run every scraper and return all newly discovered showtimes."""
    all_new: list[Showtime] = []
    for scraper in ALL_SCRAPERS:
        try:
            new = scraper.scrape_all()
            all_new.extend(new)
            logger.info("%s: %d new showtimes found", scraper.chain_name, len(new))
        except Exception as exc:  # noqa: BLE001
            logger.error("Scraper %s failed: %s", scraper.chain_name, exc)
    return all_new
