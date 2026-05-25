"""Database models for IMAX Alert application."""
from datetime import datetime, timezone

from app import db


class Theater(db.Model):
    """Represents an IMAX theater location."""

    __tablename__ = "theaters"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    chain = db.Column(db.String(100))  # e.g. AMC, Regal, Cinemark
    address = db.Column(db.String(300))
    city = db.Column(db.String(100))
    state = db.Column(db.String(50))
    zip_code = db.Column(db.String(20))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    screen_size = db.Column(db.String(100))   # e.g. "76 x 97 feet"
    projector_type = db.Column(db.String(100))  # e.g. "IMAX with Laser", "IMAX 70mm"
    audio_system = db.Column(db.String(100))  # e.g. "IMAX 12-channel"
    website = db.Column(db.String(500))
    phone = db.Column(db.String(30))
    image_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    showtimes = db.relationship("Showtime", back_populates="theater", lazy="dynamic")
    alert_preferences = db.relationship(
        "AlertPreference", back_populates="theater", lazy="dynamic"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "chain": self.chain,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "screen_size": self.screen_size,
            "projector_type": self.projector_type,
            "audio_system": self.audio_system,
            "website": self.website,
            "phone": self.phone,
            "image_url": self.image_url,
        }

    def __repr__(self):
        return f"<Theater {self.name}>"


class Movie(db.Model):
    """Represents a movie showing in IMAX format."""

    __tablename__ = "movies"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    release_date = db.Column(db.Date)
    genre = db.Column(db.String(100))
    runtime_minutes = db.Column(db.Integer)
    rating = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    showtimes = db.relationship("Showtime", back_populates="movie", lazy="dynamic")
    alert_preferences = db.relationship(
        "AlertPreference", back_populates="movie", lazy="dynamic"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "image_url": self.image_url,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "genre": self.genre,
            "runtime_minutes": self.runtime_minutes,
            "rating": self.rating,
        }

    def __repr__(self):
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
    format_type = db.Column(db.String(100))  # e.g. "IMAX", "IMAX 3D", "IMAX with Laser"
    first_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    theater = db.relationship("Theater", back_populates="showtimes")
    movie = db.relationship("Movie", back_populates="showtimes")

    __table_args__ = (
        db.UniqueConstraint("theater_id", "movie_id", "show_datetime", name="uq_showtime"),
    )

    def to_dict(self):
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
        return f"<Showtime {self.movie_id} at {self.theater_id} on {self.show_datetime}>"


class User(db.Model):
    """Represents an app user."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(300), unique=True)
    phone = db.Column(db.String(30))
    location_lat = db.Column(db.Float)
    location_lon = db.Column(db.Float)
    location_name = db.Column(db.String(300))
    notify_email = db.Column(db.Boolean, default=True)
    notify_sms = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    alert_preferences = db.relationship(
        "AlertPreference", back_populates="user", lazy="dynamic"
    )
    notifications = db.relationship("Notification", back_populates="user", lazy="dynamic")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "location_lat": self.location_lat,
            "location_lon": self.location_lon,
            "location_name": self.location_name,
            "notify_email": self.notify_email,
            "notify_sms": self.notify_sms,
        }

    def __repr__(self):
        return f"<User {self.email}>"


class AlertPreference(db.Model):
    """Tracks user alert preferences for theater/movie combinations."""

    __tablename__ = "alert_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    theater_id = db.Column(db.Integer, db.ForeignKey("theaters.id"), nullable=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.id"), nullable=True)
    # If theater_id is None, alert for any theater for the movie
    # If movie_id is None, alert for any movie at the theater
    alert_sent = db.Column(db.Boolean, default=False)
    alert_sent_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="alert_preferences")
    theater = db.relationship("Theater", back_populates="alert_preferences")
    movie = db.relationship("Movie", back_populates="alert_preferences")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "user_name": self.user.name if self.user else None,
            "theater_id": self.theater_id,
            "theater_name": self.theater.name if self.theater else "Any",
            "movie_id": self.movie_id,
            "movie_title": self.movie.title if self.movie else "Any",
            "alert_sent": self.alert_sent,
            "alert_sent_at": self.alert_sent_at.isoformat() if self.alert_sent_at else None,
            "is_active": self.is_active,
        }

    def __repr__(self):
        return f"<AlertPreference user={self.user_id} movie={self.movie_id} theater={self.theater_id}>"


class Notification(db.Model):
    """Records sent notifications."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    alert_preference_id = db.Column(
        db.Integer, db.ForeignKey("alert_preferences.id"), nullable=True
    )
    showtime_id = db.Column(db.Integer, db.ForeignKey("showtimes.id"), nullable=True)
    method = db.Column(db.String(20))  # "email" or "sms"
    message = db.Column(db.Text)
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text)

    user = db.relationship("User", back_populates="notifications")

    def __repr__(self):
        return f"<Notification to={self.user_id} method={self.method} sent={self.sent_at}>"
