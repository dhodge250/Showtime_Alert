import logging
import re
from datetime import datetime
from typing import Optional

from app.scrapers.base import BaseScraper, _local_to_utc
from app.models import Showtime, Theater

logger = logging.getLogger(__name__)


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
    health_website = "royalbcmuseum.bc.ca"
    BASE_URL = "https://sales.royalbcmuseum.bc.ca"

    def _parse_atms_dt(self, text: str, theater: Theater) -> Optional[datetime]:
        """
        Parse ATMS date+time strings and return naive UTC.

        Formats:
        - Calendar: "Friday June 5, 2026 - 7:15 PM"
        - Listing:  "7:15 PM - May 28, 2026"
        """
        text = text.strip()
        for fmt in (
            "%A %B %d, %Y - %I:%M %p",
            "%I:%M %p - %b %d, %Y",
            "%I:%M %p - %B %d, %Y",
        ):
            try:
                naive_local = datetime.strptime(text, fmt)
                return _local_to_utc(naive_local, theater)
            except ValueError:
                continue
        return None

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
            show_dt = self._parse_atms_dt(date_str, theater)
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
            show_dt = self._parse_atms_dt(link_text, theater)
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
