"""
IMAX theater web scraper.

Crawls IMAX theater chain websites to discover movies and showtimes.
Each chain has its own scraper class. Results are persisted to the database.

Scraping is demand-driven: only theaters and movies that have at least one
active, unsent AlertPreference are scraped.  Once an alert fires (alert_sent=True)
the corresponding movie/theater combination stops being scraped until a new
alert is created.
"""
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

        When a brand-new row is created and TMDB is configured, the movie is
        immediately enriched with TMDB metadata (tmdb_id, poster_url,
        release_date, runtime).  This prevents duplicate Movie rows: if the
        user later creates an alert via the TMDB search UI the app will find
        this row via tmdb_id rather than creating a second bare row.
        """
        movie = Movie.query.filter(Movie.title.ilike(title)).first()
        if not movie:
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
