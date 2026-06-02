from app.scrapers.base import BaseScraper, _parse_time_text
from app.models import Showtime, Theater


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
