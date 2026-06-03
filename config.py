"""Configuration for IMAX Alert application."""
import os
from dotenv import load_dotenv

load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(basedir, 'imax_alert.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Email settings
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@imaxalert.com")

    # Twilio SMS settings
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

    # Showtime scraper schedule
    SCRAPER_INTERVAL_MINUTES = int(os.environ.get("SCRAPER_INTERVAL_MINUTES", 30))

    # Independent alert processor schedule
    ALERT_INTERVAL_MINUTES = int(os.environ.get("ALERT_INTERVAL_MINUTES", 15))

    # Venue crawler schedule — runs much less frequently than the showtime scraper
    # because the list of IMAX theaters changes rarely (new openings, closures).
    VENUE_CRAWL_INTERVAL_DAYS = int(os.environ.get("VENUE_CRAWL_INTERVAL_DAYS", 7))

    # Run the venue crawler once on startup if the theaters table is empty.
    # Set to "false" to disable the startup crawl (e.g. if seeding manually).
    VENUE_CRAWL_ON_EMPTY = os.environ.get("VENUE_CRAWL_ON_EMPTY", "true").lower() == "true"

    # Tie CSRF token lifetime to the session rather than a fixed 1-hour window.
    # Without this, Mobile Safari suspends background tabs long enough for the
    # default 3600 s limit to expire, causing "CSRF token expired" on next use.
    WTF_CSRF_TIME_LIMIT = None

    # Google Maps / Leaflet (no API key needed for Leaflet + OpenStreetMap)
    MAPS_ENABLED = True


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False


class TestingConfig(Config):
    """Testing configuration."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    VENUE_CRAWL_ON_EMPTY = False
    # Skip the 1927-row CSV upsert and incremental column migrations in tests —
    # create_all() builds the schema fresh, and tests supply their own fixture data.
    SKIP_CSV_SEED = True
    SKIP_MIGRATIONS = True
    # Disable rate limiting so repeated login calls across tests don't trip 429s.
    RATELIMIT_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
