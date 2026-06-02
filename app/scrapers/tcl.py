from app.scrapers.base import BaseScraper, _parse_time_text
from app.models import Showtime, Theater


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
                    theater, movie, show_dt, tickets_url=tickets_url,
                    format_type="IMAX with Laser"
                )
                if is_new:
                    new_showtimes.append(showtime)

        return new_showtimes
