"""Configuration for IMAX Alert application."""
import os
import tempfile
import uuid
from dotenv import load_dotenv

load_dotenv()

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Base configuration."""

    # Dev-only fallback — create_app() refuses to boot in production with this value.
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

    # Tie CSRF token lifetime to the session rather than a fixed 1-hour window.
    # Without this, Mobile Safari suspends background tabs long enough for the
    # default 3600 s limit to expire, causing "CSRF token expired" on next use.
    WTF_CSRF_TIME_LIMIT = None


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False


class TestingConfig(Config):
    """Testing configuration."""

    TESTING = True
    # The coordinator dispatches chain batches to worker threads (see
    # queue_theaters_for_scrape), each opening its own connection. A plain
    # ":memory:" DB is private per thread (SingletonThreadPool), and sharing
    # one via StaticPool + check_same_thread=False hands both worker threads
    # the *same* raw sqlite3.Connection object — concurrent execute()/commit()
    # calls on it race and can silently drop a commit. A temp file-based DB
    # (same as production, which never uses :memory:) gives each thread its
    # own connection, safely serialized by SQLite's normal file locking.
    SQLALCHEMY_DATABASE_URI = (
        f"sqlite:///{os.path.join(tempfile.gettempdir(), f'imax_alert_test_{uuid.uuid4().hex}.db')}"
    )
    WTF_CSRF_ENABLED = False
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
