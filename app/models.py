"""Database models for IMAX Alert application."""
import json
import secrets
from datetime import datetime, timedelta, timezone

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
    venue_key = db.Column(db.String(100), unique=True, nullable=True)

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
    description = db.Column(db.Text, nullable=True)
    amenities = db.Column(db.Text, nullable=True)   # JSON list, e.g. '["Parking","Dining"]'
    seating_capacity = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    crawl_source = db.Column(db.String(100))
    last_crawled_at = db.Column(db.DateTime)
    on_demand_fetched_at = db.Column(db.DateTime, nullable=True)
    last_scraped_at = db.Column(db.DateTime, nullable=True)
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
    on_demand = db.Column(db.Boolean, default=False, nullable=False, server_default="0")

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
    timezone = db.Column(db.String(100), default="UTC")
    force_password_change = db.Column(db.Boolean, default=False)
    reset_token = db.Column(db.String(256), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    mfa_secret = db.Column(db.String(64), nullable=True)
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_recovery_codes = db.Column(db.Text, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
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

    def generate_reset_token(self, expiry_hours: int = 1) -> str:
        """Create a secure password-reset token, store its hash, and return the raw token."""
        import secrets
        from datetime import timedelta
        raw = secrets.token_urlsafe(32)
        self.reset_token = generate_password_hash(raw)
        # Store as naive UTC: SQLAlchemy/SQLite strips timezone info on read-back,
        # so using naive UTC avoids mismatched comparisons.
        self.reset_token_expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=expiry_hours)
        return raw

    def verify_reset_token(self, raw_token: str) -> bool:
        """Return True if *raw_token* matches the stored hash and has not expired."""
        if not self.reset_token or not self.reset_token_expiry:
            return False
        if datetime.now(timezone.utc).replace(tzinfo=None) > self.reset_token_expiry:
            return False
        return check_password_hash(self.reset_token, raw_token)

    def clear_reset_token(self):
        """Invalidate the reset token after it has been used."""
        self.reset_token = None
        self.reset_token_expiry = None

    # ── MFA helpers ──────────────────────────────────────────────────────

    def generate_mfa_secret(self) -> str:
        """Generate and store a new TOTP secret; return the raw base32 secret."""
        import pyotp
        self.mfa_secret = pyotp.random_base32()
        return self.mfa_secret

    def mfa_totp_uri(self, issuer: str = "IMAX Alert") -> str:
        """Return the otpauth:// URI for QR code generation."""
        import pyotp
        return pyotp.totp.TOTP(self.mfa_secret).provisioning_uri(
            name=self.email, issuer_name=issuer
        )

    def verify_totp(self, code: str) -> bool:
        """Return True if *code* is a valid current TOTP for this user's secret."""
        if not self.mfa_secret:
            return False
        import pyotp
        return pyotp.TOTP(self.mfa_secret).verify(code, valid_window=1)

    def generate_recovery_codes(self, count: int = 8) -> list[str]:
        """Generate *count* 8-character plaintext recovery codes, store hashed, return plaintext."""
        raw_codes = [secrets.token_hex(8).upper() for _ in range(count)]
        hashed = [generate_password_hash(c) for c in raw_codes]
        self.mfa_recovery_codes = json.dumps(hashed)
        return raw_codes

    def use_recovery_code(self, code: str) -> bool:
        """Consume a recovery code — return True and remove it if valid, else False."""
        if not self.mfa_recovery_codes:
            return False
        hashed_list = json.loads(self.mfa_recovery_codes)
        for i, h in enumerate(hashed_list):
            if check_password_hash(h, code.upper()):
                hashed_list.pop(i)
                self.mfa_recovery_codes = json.dumps(hashed_list)
                return True
        return False

    def clear_mfa(self):
        """Disable MFA and wipe all related fields."""
        self.mfa_enabled = False
        self.mfa_secret = None
        self.mfa_recovery_codes = None

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


class UserInvite(db.Model):
    """Pending email invitation for a new user."""

    __tablename__ = "user_invites"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(300), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=True)
    token_hash = db.Column(db.String(256), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    accepted_at = db.Column(db.DateTime, nullable=True)

    role = db.relationship("Role")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @staticmethod
    def create(email: str, role_id: int, created_by_id: int, expiry_hours: int = 48) -> tuple["UserInvite", str]:
        """Create and return (invite_record, raw_token). Token is NOT stored in DB."""
        raw = secrets.token_urlsafe(32)
        invite = UserInvite(
            email=email,
            role_id=role_id,
            token_hash=generate_password_hash(raw),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=expiry_hours),
            created_by_id=created_by_id,
        )
        return invite, raw

    def is_valid(self) -> bool:
        """Return True if invite has not expired and has not been accepted."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return self.accepted_at is None and now < self.expires_at

    def verify_token(self, raw_token: str) -> bool:
        return check_password_hash(self.token_hash, raw_token)

    @property
    def role_name(self) -> str:
        return self.role.name if self.role else "user"

    def __repr__(self):
        return f"<UserInvite {self.email}>"


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
    # Optional: only fire when a matching showtime exists on this specific date.
    # None = fire on any date (existing behaviour).
    target_date = db.Column(db.Date, nullable=True)
    # Optional buffer (days) around target_date — fires if showtime falls within
    # [target_date - buffer, target_date + buffer]. Ignored when target_date is None.
    target_date_buffer = db.Column(db.Integer, nullable=True)
    # Radius-based targeting: notify when a matching movie appears within radius_km
    # of the alert owner's saved location. When set, theater_id is ignored.
    radius_km = db.Column(db.Float, nullable=True)
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
    def target_date_range(self):
        """Return (date_from, date_to) when target_date is set, else (None, None)."""
        if not self.target_date:
            return None, None
        from datetime import timedelta
        buf = self.target_date_buffer or 0
        return self.target_date - timedelta(days=buf), self.target_date + timedelta(days=buf)

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
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "target_date_buffer": self.target_date_buffer,
            "radius_km": self.radius_km,
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


class LogEntry(db.Model):
    """Structured in-app activity and error log."""

    __tablename__ = "log_entries"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    level = db.Column(db.String(10), default="INFO")
    category = db.Column(db.String(30), index=True)
    message = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    details = db.Column(db.Text, nullable=True)

    user = db.relationship("User", backref=db.backref("log_entries", lazy="dynamic"))

    def __repr__(self):
        return f"<LogEntry [{self.level}] {self.category}: {self.message[:60]}>"


class ScraperStatus(db.Model):
    """Health check result for one scraper chain, written by the daily health-check job."""

    __tablename__ = "scraper_status"

    id = db.Column(db.Integer, primary_key=True)
    chain_name = db.Column(db.String(100), nullable=False, index=True)
    checked_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False)  # 'ok', 'warning', 'error'
    showtime_count = db.Column(db.Integer, nullable=True)
    theater_count = db.Column(db.Integer, nullable=True)
    error_class = db.Column(db.String(100), nullable=True)
    error_summary = db.Column(db.String(500), nullable=True)

    def __repr__(self):
        return f"<ScraperStatus chain={self.chain_name} status={self.status}>"


class BrowseSchedule(db.Model):
    """
    User-configured recurring task that scrapes all showtimes from every
    theater within a radius of the user's saved location.

    No alerts are sent — the data is stored for passive browsing.
    One schedule per user (enforced by unique constraint on user_id).
    Scrape coordination (deduplication, cooldown, concurrency) is handled
    entirely by the unified scraper coordinator.
    """

    __tablename__ = "browse_schedules"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    radius = db.Column(db.Float, nullable=False)
    radius_unit = db.Column(db.String(5), nullable=False, default="km")  # 'km' or 'miles'
    # Valid values: 30, 60, 360, 720, 1440, 10080 (minutes)
    frequency_minutes = db.Column(db.Integer, nullable=False, default=60)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_run = db.Column(db.DateTime, nullable=True)
    next_run = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    user = db.relationship("User", backref=db.backref("browse_schedule", uselist=False))

    FREQUENCY_LABELS = {
        30: "Every 30 minutes",
        60: "Hourly",
        360: "Every 6 hours",
        720: "Every 12 hours",
        1440: "Daily",
        10080: "Weekly",
    }

    def frequency_label(self) -> str:
        return self.FREQUENCY_LABELS.get(self.frequency_minutes, f"Every {self.frequency_minutes} min")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "radius": self.radius,
            "radius_unit": self.radius_unit,
            "frequency_minutes": self.frequency_minutes,
            "frequency_label": self.frequency_label(),
            "enabled": self.enabled,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (
            f"<BrowseSchedule user={self.user_id} radius={self.radius}{self.radius_unit} "
            f"freq={self.frequency_minutes}min enabled={self.enabled}>"
        )
