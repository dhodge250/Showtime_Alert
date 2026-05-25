"""
IMAX theater web scraper.

Crawls IMAX theater chain websites to discover movies and showtimes.
Each chain has its own scraper class. Results are persisted to the database.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app import db
from app.models import Movie, Showtime, Theater

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
        """Return existing movie or create a new one."""
        movie = Movie.query.filter(Movie.title.ilike(title)).first()
        if not movie:
            movie = Movie(title=title, **kwargs)
            db.session.add(movie)
            db.session.flush()
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

    def scrape_theater(self, theater: Theater) -> list[Showtime]:
        """Scrape a single theater. Override in subclasses."""
        raise NotImplementedError

    def scrape_all(self) -> list[Showtime]:
        """Scrape all active theaters belonging to this chain."""
        theaters = Theater.query.filter_by(chain=self.chain_name, is_active=True).all()
        new_showtimes: list[Showtime] = []
        for theater in theaters:
            try:
                results = self.scrape_theater(theater)
                new_showtimes.extend(results)
            except Exception as exc:  # noqa: BLE001
                logger.error("Error scraping %s: %s", theater.name, exc)
        db.session.commit()
        return new_showtimes


class AMCScraper(BaseScraper):
    """Scraper for AMC Theatres IMAX showtimes."""

    chain_name = "AMC"

    def scrape_theater(self, theater: Theater) -> list[Showtime]:
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

    def scrape_theater(self, theater: Theater) -> list[Showtime]:
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

    def scrape_theater(self, theater: Theater) -> list[Showtime]:
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

    def scrape_theater(self, theater: Theater) -> list[Showtime]:
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


ALL_SCRAPERS: list[BaseScraper] = [
    AMCScraper(),
    RegalScraper(),
    CinemarkScraper(),
    TCLScraper(),
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
