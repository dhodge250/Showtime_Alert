"""Database models for IMAX Alert application."""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app import db


# ---------------------------------------------------------------------------
# Lookup / Reference tables
# ---------------------------------------------------------------------------

class Role(db.Model):
    """User roles: admin, editor, user."""

    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)  # 'admin','editor','user'
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    users = db.relationship("User", back_populates="role", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name, "description": self.description}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Role {self.name}>"


class Chain(db.Model):
    """Theater chain / operator (AMC, Regal, Cinemark, …)."""

    __tablename__ = "chains"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    website = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theaters = db.relationship("Theater", back_populates="chain_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name, "website": self.website}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Chain {self.name}>"


class Country(db.Model):
    """Country lookup table."""

    __tablename__ = "countries"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    regions = db.relationship("Region", back_populates="country", lazy="dynamic")
    cities = db.relationship("City", back_populates="country", lazy="dynamic")
    theaters = db.relationship("Theater", back_populates="country_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Country {self.name}>"


class Region(db.Model):
    """State / Province / Region lookup table."""

    __tablename__ = "regions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    country_id = db.Column(db.Integer, db.ForeignKey("countries.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("name", "country_id", name="uq_region_country"),
    )

    country = db.relationship("Country", back_populates="regions")
    cities = db.relationship("City", back_populates="region", lazy="dynamic")
    theaters = db.relationship("Theater", back_populates="region_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "name": self.name,
            "country_id": self.country_id,
            "country_name": self.country.name if self.country else None,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Region {self.name}>"


class City(db.Model):
    """City lookup table."""

    __tablename__ = "cities"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    region_id = db.Column(db.Integer, db.ForeignKey("regions.id"), nullable=True)
    country_id = db.Column(db.Integer, db.ForeignKey("countries.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("name", "country_id", "region_id", name="uq_city_country_region"),
    )

    region = db.relationship("Region", back_populates="cities")
    country = db.relationship("Country", back_populates="cities")
    theaters = db.relationship("Theater", back_populates="city_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "name": self.name,
            "region_id": self.region_id,
            "region_name": self.region.name if self.region else None,
            "country_id": self.country_id,
            "country_name": self.country.name if self.country else None,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<City {self.name}>"


class AspectRatio(db.Model):
    """Screen aspect ratio options (1.43:1, 1.90:1, …)."""

    __tablename__ = "aspect_ratios"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(50), unique=True, nullable=False)  # e.g. "1.43:1"
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theaters = db.relationship(
        "Theater",
        foreign_keys="Theater.aspect_ratio_id",
        back_populates="aspect_ratio_ref",
        lazy="dynamic",
    )

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<AspectRatio {self.label}>"


class ProjectorType(db.Model):
    """Projector type lookup (IMAX with Laser, IMAX 70mm, …)."""

    __tablename__ = "projector_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theaters = db.relationship(
        "Theater",
        foreign_keys="Theater.projector_type_id",
        back_populates="projector_type_ref",
        lazy="dynamic",
    )

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<ProjectorType {self.name}>"


class Continent(db.Model):
    """Continent lookup table (Africa, Asia, Europe, …)."""

    __tablename__ = "continents"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theaters = db.relationship("Theater", back_populates="continent_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Continent {self.name}>"


class AudioSystem(db.Model):
    """Audio system lookup (IMAX 12-channel, IMAX 6-channel, …)."""

    __tablename__ = "audio_systems"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theaters = db.relationship("Theater", back_populates="audio_system_ref", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"id": self.id, "name": self.name}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<AudioSystem {self.name}>"


class Settings(db.Model):
    """App-wide key/value settings (stored in DB, editable via admin UI)."""

    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default="")
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {"key": self.key, "value": self.value}

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Settings {self.key}>"


# ---------------------------------------------------------------------------
# Core application tables
# ---------------------------------------------------------------------------

class Theater(db.Model):
    """Represents an IMAX theater location."""

    __tablename__ = "theaters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)

    # --- Legacy string columns (kept for backward compat; populated by crawler) ---
    chain = db.Column(db.String(100))
    address = db.Column(db.String(300))
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    country = db.Column(db.String(100), default="United States")
    screen_size = db.Column(db.String(100))     # aspect ratio string, e.g. "1.43:1"
    screen_dims = db.Column(db.String(200))     # legacy combined dims string
    projector_type = db.Column(db.String(100))
    audio_system = db.Column(db.String(100))

    # --- FK columns (new, nullable during migration) ---
    chain_id = db.Column(db.Integer, db.ForeignKey("chains.id"), nullable=True)
    country_id = db.Column(db.Integer, db.ForeignKey("countries.id"), nullable=True)
    region_id = db.Column(db.Integer, db.ForeignKey("regions.id"), nullable=True)
    city_id = db.Column(db.Integer, db.ForeignKey("cities.id"), nullable=True)
    aspect_ratio_id = db.Column(db.Integer, db.ForeignKey("aspect_ratios.id"), nullable=True)
    projector_type_id = db.Column(db.Integer, db.ForeignKey("projector_types.id"), nullable=True)
    audio_system_id = db.Column(db.Integer, db.ForeignKey("audio_systems.id"), nullable=True)
    continent_id = db.Column(db.Integer, db.ForeignKey("continents.id"), nullable=True)
    digital_projector_ar_id = db.Column(db.Integer, db.ForeignKey("aspect_ratios.id"), nullable=True)
    film_projector_type_id = db.Column(db.Integer, db.ForeignKey("projector_types.id"), nullable=True)
    film_projector_type = db.Column(db.String(100), nullable=True)  # raw string from CSV
    commercial_films = db.Column(db.String(20), nullable=True)      # 'Yes', 'Limited', 'No'

    # --- Physical screen dimensions (stored in meters) ---
    screen_width_m = db.Column(db.Float, nullable=True)
    screen_height_m = db.Column(db.Float, nullable=True)

    # --- Other fields ---
    zip_code = db.Column(db.String(20))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    website = db.Column(db.String(500))
    phone = db.Column(db.String(30))
    image_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    crawl_source = db.Column(db.String(100))
    last_crawled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # --- Relationships ---
    chain_ref = db.relationship("Chain", back_populates="theaters")
    country_ref = db.relationship("Country", back_populates="theaters")
    region_ref = db.relationship("Region", back_populates="theaters")
    city_ref = db.relationship("City", back_populates="theaters")
    aspect_ratio_ref = db.relationship(
        "AspectRatio",
        foreign_keys=[aspect_ratio_id],
        back_populates="theaters",
    )
    projector_type_ref = db.relationship(
        "ProjectorType",
        foreign_keys=[projector_type_id],
        back_populates="theaters",
    )
    audio_system_ref = db.relationship("AudioSystem", back_populates="theaters")
    continent_ref = db.relationship("Continent", back_populates="theaters")
    digital_projector_ar_ref = db.relationship("AspectRatio", foreign_keys=[digital_projector_ar_id])
    film_projector_type_ref = db.relationship("ProjectorType", foreign_keys=[film_projector_type_id])
    showtimes = db.relationship("Showtime", back_populates="theater", lazy="dynamic")
    alert_preferences = db.relationship(
        "AlertPreference", back_populates="theater", lazy="dynamic"
    )

    # ---------------------------------------------------------------------------
    # Resolved property helpers (prefer FK-resolved values, fall back to strings)
    # ---------------------------------------------------------------------------

    @property
    def chain_name(self):
        """Resolved chain name, preferring the FK table over the legacy string."""
        return self.chain_ref.name if self.chain_ref else (self.chain or "")

    @property
    def country_name(self):
        """Resolved country name, preferring the FK table over the legacy string."""
        return self.country_ref.name if self.country_ref else (self.country or "")

    @property
    def region_name(self):
        """Resolved region/state name, preferring the FK table over the legacy string."""
        return self.region_ref.name if self.region_ref else (self.state or "")

    @property
    def city_name(self):
        """Resolved city name, preferring the FK table over the legacy string."""
        return self.city_ref.name if self.city_ref else (self.city or "")

    @property
    def aspect_ratio_label(self):
        """Resolved aspect ratio label, preferring the FK table over the legacy string."""
        return (
            self.aspect_ratio_ref.label if self.aspect_ratio_ref
            else (self.screen_size or "")
        )

    @property
    def projector_type_name(self):
        """Resolved projector type name, preferring the FK table over the legacy string."""
        return (
            self.projector_type_ref.name if self.projector_type_ref
            else (self.projector_type or "")
        )

    @property
    def audio_system_name(self):
        """Resolved audio system name, preferring the FK table over the legacy string."""
        return (
            self.audio_system_ref.name if self.audio_system_ref
            else (self.audio_system or "")
        )

    @property
    def continent_name(self):
        """Resolved continent name from the FK table, or empty string."""
        return self.continent_ref.name if self.continent_ref else ""

    @property
    def digital_projector_ar_label(self):
        """Resolved digital projector aspect ratio label, or empty string."""
        return (
            self.digital_projector_ar_ref.label
            if self.digital_projector_ar_ref else ""
        )

    @property
    def film_projector_type_name(self):
        """Resolved film projector type name, preferring FK table over legacy string."""
        return (
            self.film_projector_type_ref.name if self.film_projector_type_ref
            else (self.film_projector_type or "")
        )

    @property
    def screen_width_ft(self):
        """Screen width converted to feet, or None if dimensions are not set."""
        if self.screen_width_m is None:
            return None
        return round(self.screen_width_m * 3.28084, 2)

    @property
    def screen_height_ft(self):
        """Screen height converted to feet, or None if dimensions are not set."""
        if self.screen_height_m is None:
            return None
        return round(self.screen_height_m * 3.28084, 2)

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "name": self.name,
            # Resolved strings (prefer FK tables)
            "chain": self.chain_name,
            "chain_id": self.chain_id,
            "country": self.country_name,
            "country_id": self.country_id,
            "state": self.region_name,
            "region_id": self.region_id,
            "city": self.city_name,
            "city_id": self.city_id,
            "screen_size": self.aspect_ratio_label,
            "aspect_ratio_id": self.aspect_ratio_id,
            "projector_type": self.projector_type_name,
            "projector_type_id": self.projector_type_id,
            "audio_system": self.audio_system_name,
            "audio_system_id": self.audio_system_id,
            # New CSV-seeded fields
            "continent": self.continent_name,
            "continent_id": self.continent_id,
            "digital_projector_ar": self.digital_projector_ar_label,
            "digital_projector_ar_id": self.digital_projector_ar_id,
            "film_projector_type": self.film_projector_type_name,
            "film_projector_type_id": self.film_projector_type_id,
            "commercial_films": self.commercial_films,
            # Screen dimensions
            "screen_width_m": self.screen_width_m,
            "screen_height_m": self.screen_height_m,
            "screen_width_ft": self.screen_width_ft,
            "screen_height_ft": self.screen_height_ft,
            # Legacy combined dims string (kept for display fallback)
            "screen_dims": self.screen_dims,
            # Other
            "address": self.address,
            "zip_code": self.zip_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "website": self.website,
            "phone": self.phone,
            "image_url": self.image_url,
            "crawl_source": self.crawl_source,
            "last_crawled_at": self.last_crawled_at.isoformat() if self.last_crawled_at else None,
            "is_active": self.is_active,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Theater {self.name}>"


class Movie(db.Model):
    """Represents a movie showing in IMAX format."""

    __tablename__ = "movies"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    poster_url = db.Column(db.String(500))      # TMDB poster path
    tmdb_id = db.Column(db.Integer, unique=True, nullable=True)
    release_date = db.Column(db.Date)
    genre = db.Column(db.String(100))
    runtime_minutes = db.Column(db.Integer)
    rating = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    showtimes = db.relationship("Showtime", back_populates="movie", lazy="dynamic")
    alert_preferences = db.relationship(
        "AlertPreference", back_populates="movie", lazy="dynamic"
    )
    alert_movies = db.relationship("AlertMovie", back_populates="movie", lazy="dynamic")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "image_url": self.image_url,
            "poster_url": self.poster_url,
            "tmdb_id": self.tmdb_id,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "genre": self.genre,
            "runtime_minutes": self.runtime_minutes,
            "rating": self.rating,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Movie {self.title}>"


class Showtime(db.Model):
    """Represents a specific IMAX showtime."""

    __tablename__ = "showtimes"

    id = db.Column(db.Integer, primary_key=True)
    theater_id = db.Column(db.Integer, db.ForeignKey("theaters.id"), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id"), nullable=False)
    show_datetime = db.Column(db.DateTime, nullable=False)
    tickets_available = db.Column(db.Boolean, default=True)
    tickets_url = db.Column(db.String(500))
    format_type = db.Column(db.String(100))
    first_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theater = db.relationship("Theater", back_populates="showtimes")
    movie = db.relationship("Movie", back_populates="showtimes")

    __table_args__ = (
        db.UniqueConstraint("theater_id", "movie_id", "show_datetime", name="uq_showtime"),
    )

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "theater_id": self.theater_id,
            "theater_name": self.theater.name if self.theater else None,
            "movie_id": self.movie_id,
            "movie_title": self.movie.title if self.movie else None,
            "show_datetime": self.show_datetime.isoformat(),
            "tickets_available": self.tickets_available,
            "tickets_url": self.tickets_url,
            "format_type": self.format_type,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<Showtime {self.movie_id} at {self.theater_id} on {self.show_datetime}>"


class User(db.Model, UserMixin):
    """Represents an app user with role-based access."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(300), unique=True)
    phone = db.Column(db.String(30))
    password_hash = db.Column(db.String(256))
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    measurement_unit = db.Column(db.String(10), default="metric")  # 'metric' or 'imperial'
    location_lat = db.Column(db.Float)
    location_lon = db.Column(db.Float)
    location_name = db.Column(db.String(300))
    location_address = db.Column(db.String(500))  # full address string for geocoding
    notify_email = db.Column(db.Boolean, default=True)
    notify_sms = db.Column(db.Boolean, default=False)
    force_password_change = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    role = db.relationship("Role", back_populates="users")
    alert_preferences = db.relationship(
        "AlertPreference", back_populates="user", lazy="dynamic"
    )
    notifications = db.relationship("Notification", back_populates="user", lazy="dynamic")

    def get_id(self):
        """Return the user ID as a string, required by Flask-Login."""
        return str(self.id)

    def set_password(self, password: str):
        """Hash and store *password*."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Return True if *password* matches the stored hash."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def role_name(self):
        """Return the user's role name, defaulting to 'user' when no role is set."""
        return self.role.name if self.role else "user"

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "role": self.role_name,
            "role_id": self.role_id,
            "is_active": self.is_active,
            "measurement_unit": self.measurement_unit or "metric",
            "location_lat": self.location_lat,
            "location_lon": self.location_lon,
            "location_name": self.location_name,
            "location_address": self.location_address,
            "notify_email": self.notify_email,
            "notify_sms": self.notify_sms,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return f"<User {self.email}>"


class AlertPreference(db.Model):
    """Tracks user alert preferences for theater/movie combinations.

    A preference may watch zero or more specific movies at a theater.
    Zero movies = "any movie" mode — all films found at the theater trigger
    notifications and the alert never auto-closes.
    Multiple movies are tracked individually via AlertMovie rows; the whole
    preference is marked sent only when every AlertMovie has fired.
    """

    __tablename__ = "alert_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    theater_id = db.Column(db.Integer, db.ForeignKey("theaters.id"), nullable=True)
    # Legacy single-movie column — kept for backward compat; always None on new rows.
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id"), nullable=True)
    alert_sent = db.Column(db.Boolean, default=False)
    alert_sent_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    # Optional cap on how many times this alert fires before auto-closing.
    # None = unlimited.
    max_notifications = db.Column(db.Integer, nullable=True)
    notifications_fired = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="alert_preferences")
    theater = db.relationship("Theater", back_populates="alert_preferences")
    movie = db.relationship("Movie", back_populates="alert_preferences")
    alert_movies = db.relationship(
        "AlertMovie",
        back_populates="alert",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="AlertMovie.created_at",
    )
    notifications = db.relationship(
        "Notification",
        foreign_keys="Notification.alert_preference_id",
        backref=db.backref("alert_preference", lazy="joined"),
        lazy="dynamic",
        order_by="Notification.sent_at.desc()",
    )

    @property
    def is_any_movie(self) -> bool:
        """True when no specific movies are being watched (scrape all films)."""
        return self.alert_movies.count() == 0

    @property
    def unsent_movies(self):
        """QuerySet of AlertMovie rows that have not yet fired."""
        return self.alert_movies.filter_by(alert_sent=False)

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        movies = [
            {
                "id": am.id,
                "movie_id": am.movie_id,
                "movie_title": am.movie.title if am.movie else "Unknown",
                "poster_url": (am.movie.poster_url or am.movie.image_url or "") if am.movie else "",
                "alert_sent": am.alert_sent,
                "alert_sent_at": am.alert_sent_at.isoformat() if am.alert_sent_at else None,
            }
            for am in self.alert_movies.all()
        ]
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user.name if self.user else None,
            "theater_id": self.theater_id,
            "theater_name": self.theater.name if self.theater else "Any",
            # Legacy field — kept for API compat but always None on new rows
            "movie_id": self.movie_id,
            "movie_title": self.movie.title if self.movie else ("Any" if not movies else None),
            "movies": movies,
            "alert_sent": self.alert_sent,
            "alert_sent_at": self.alert_sent_at.isoformat() if self.alert_sent_at else None,
            "is_active": self.is_active,
            "max_notifications": self.max_notifications,
            "notifications_fired": self.notifications_fired or 0,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return (
            f"<AlertPreference user={self.user_id} theater={self.theater_id} "
            f"movies={self.alert_movies.count()}>"
        )


class AlertMovie(db.Model):
    """Per-movie sent-state within an AlertPreference.

    Each row tracks whether a specific movie has triggered a notification for
    the parent alert.  When all rows are sent the parent AlertPreference is
    also marked sent.
    """

    __tablename__ = "alert_movies"

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.Integer, db.ForeignKey("alert_preferences.id"), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id"), nullable=False)
    alert_sent = db.Column(db.Boolean, default=False)
    alert_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint("alert_id", "movie_id", name="uq_alert_movie"),
    )

    alert = db.relationship("AlertPreference", back_populates="alert_movies")
    movie = db.relationship("Movie", back_populates="alert_movies")

    def to_dict(self):
        """Return a dict representation for JSON serialisation."""
        return {
            "id": self.id,
            "alert_id": self.alert_id,
            "movie_id": self.movie_id,
            "movie_title": self.movie.title if self.movie else "Unknown",
            "poster_url": (self.movie.poster_url or self.movie.image_url or "") if self.movie else "",
            "alert_sent": self.alert_sent,
            "alert_sent_at": self.alert_sent_at.isoformat() if self.alert_sent_at else None,
        }

    def __repr__(self):
        """Return a concise string representation."""
        return (
            f"<AlertMovie alert={self.alert_id} movie={self.movie_id} "
            f"sent={self.alert_sent}>"
        )


class Notification(db.Model):
    """Records sent notifications."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    alert_preference_id = db.Column(
        db.Integer, db.ForeignKey("alert_preferences.id"), nullable=True
    )
    showtime_id = db.Column(db.Integer, db.ForeignKey("showtimes.id"), nullable=True)
    # JSON-encoded list of ALL showtime IDs covered by this notification batch.
    # Used for deduplication of any-movie alerts without creating per-showtime rows.
    notified_showtime_ids = db.Column(db.Text, nullable=True)
    method = db.Column(db.String(20))
    message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text)

    user = db.relationship("User", back_populates="notifications")

    def __repr__(self):
        """Return a concise string representation."""
        return (
            f"<Notification to={self.user_id} method={self.method} "
            f"sent={self.sent_at}>"
        )
