import json
import logging
from datetime import date, datetime
from urllib.parse import quote

import requests

from app.scrapers.base import BaseScraper, _local_to_utc, _parse_time_text
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)


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
    def scrape_theater(self, theater: Theater, movie_ids: set) -> list[Showtime]:
        new_showtimes: list = []
        if not theater.website:
            return new_showtimes

        location_id = self._get_location_id(theater)
        if not location_id:
            return new_showtimes

        bookable_dates = self._get_bookable_dates(location_id)
        today = date.today()
        # Use all future bookable dates the API returns — no artificial cap.
        # The dates/bookable endpoint already limits the window to what the
        # theater has on sale, which can be months ahead for pre-sales.
        upcoming = [
            d for d in bookable_dates
            if date.fromisoformat(d) >= today
        ]

        logger.info(
            "Cineplex: %s — locationId=%s, %d bookable dates from today onwards",
            theater.name, location_id, len(upcoming),
        )

        for date_iso in upcoming:
            found = self._scrape_date(theater, location_id, date_iso, movie_ids)
            new_showtimes.extend(found)

        return new_showtimes

    def _get_location_id(self, theater: Theater) -> "int | None":
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
        theater: Theater,
        location_id: int,
        date_iso: str,
        movie_ids: set,
    ) -> list[Showtime]:
        """Fetch IMAX showtimes for one theater on one date."""
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
                            naive_local = _parse_time_text(start)
                            if not naive_local:
                                continue
                            show_dt = _local_to_utc(naive_local, theater)
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
