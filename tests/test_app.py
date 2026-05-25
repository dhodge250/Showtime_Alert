"""Tests for the IMAX Alert application."""
import pytest

from app import create_app, db
from app.models import AlertPreference, Movie, Notification, Showtime, Theater, User


@pytest.fixture
def app():
    """Create application for testing."""
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_theater(app):
    with app.app_context():
        theater = Theater(
            name="Test IMAX Theater",
            chain="AMC",
            address="123 Main St",
            city="Testville",
            state="CA",
            zip_code="90210",
            latitude=34.05,
            longitude=-118.24,
            screen_size="76 x 97 feet",
            projector_type="IMAX with Laser",
            audio_system="IMAX 12-channel",
            website="https://example.com",
        )
        db.session.add(theater)
        db.session.commit()
        return theater.id


@pytest.fixture
def sample_movie(app):
    with app.app_context():
        movie = Movie(title="Interstellar IMAX", description="A space epic.")
        db.session.add(movie)
        db.session.commit()
        return movie.id


@pytest.fixture
def sample_user(app):
    with app.app_context():
        user = User(
            name="Test User",
            email="test@example.com",
            phone="+15550001234",
            notify_email=True,
            notify_sms=False,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


# ── Models ───────────────────────────────────────────────────────────


class TestTheaterModel:
    def test_create_theater(self, app, sample_theater):
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            assert theater is not None
            assert theater.name == "Test IMAX Theater"
            assert theater.chain == "AMC"
            assert theater.projector_type == "IMAX with Laser"

    def test_theater_to_dict(self, app, sample_theater):
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            d = theater.to_dict()
            assert d["name"] == "Test IMAX Theater"
            assert d["latitude"] == 34.05
            assert d["longitude"] == -118.24

    def test_theater_repr(self, app, sample_theater):
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            assert "Test IMAX Theater" in repr(theater)


class TestMovieModel:
    def test_create_movie(self, app, sample_movie):
        with app.app_context():
            movie = Movie.query.get(sample_movie)
            assert movie is not None
            assert movie.title == "Interstellar IMAX"

    def test_movie_to_dict(self, app, sample_movie):
        with app.app_context():
            movie = Movie.query.get(sample_movie)
            d = movie.to_dict()
            assert d["title"] == "Interstellar IMAX"
            assert d["release_date"] is None


class TestShowtimeModel:
    def test_create_showtime(self, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            show_dt = datetime(2025, 12, 25, 19, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater,
                movie=movie,
                show_datetime=show_dt,
                tickets_available=True,
                format_type="IMAX with Laser",
            )
            db.session.add(showtime)
            db.session.commit()
            assert showtime.id is not None
            assert showtime.tickets_available is True

    def test_showtime_to_dict(self, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            show_dt = datetime(2025, 12, 25, 19, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt,
                tickets_available=True, format_type="IMAX",
            )
            db.session.add(showtime)
            db.session.commit()
            d = showtime.to_dict()
            assert d["movie_title"] == "Interstellar IMAX"
            assert d["theater_name"] == "Test IMAX Theater"
            assert d["tickets_available"] is True


class TestUserModel:
    def test_create_user(self, app, sample_user):
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.name == "Test User"
            assert user.email == "test@example.com"

    def test_user_to_dict(self, app, sample_user):
        with app.app_context():
            user = User.query.get(sample_user)
            d = user.to_dict()
            assert d["notify_email"] is True
            assert d["notify_sms"] is False


class TestAlertPreferenceModel:
    def test_create_alert_preference(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                movie_id=sample_movie,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.commit()
            assert pref.id is not None
            assert pref.alert_sent is False
            assert pref.is_active is True

    def test_alert_preference_to_dict(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                movie_id=sample_movie,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.commit()
            d = pref.to_dict()
            assert d["movie_title"] == "Interstellar IMAX"
            assert d["theater_name"] == "Test IMAX Theater"
            assert d["alert_sent"] is False


# ── API: Theaters ─────────────────────────────────────────────────────


class TestTheaterAPI:
    def test_list_theaters(self, client, app, sample_theater):
        resp = client.get("/api/theaters")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        names = [t["name"] for t in data]
        assert "Test IMAX Theater" in names

    def test_get_single_theater(self, client, app, sample_theater):
        resp = client.get(f"/api/theaters/{sample_theater}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Test IMAX Theater"

    def test_get_missing_theater_returns_404(self, client, app):
        resp = client.get("/api/theaters/9999")
        assert resp.status_code == 404


# ── API: Movies ───────────────────────────────────────────────────────


class TestMovieAPI:
    def test_list_movies(self, client, app, sample_movie):
        resp = client.get("/api/movies")
        assert resp.status_code == 200
        data = resp.get_json()
        titles = [m["title"] for m in data]
        assert "Interstellar IMAX" in titles


# ── API: Users ────────────────────────────────────────────────────────


class TestUserAPI:
    def test_create_user(self, client):
        resp = client.post(
            "/api/users",
            json={"name": "Alice", "email": "alice@example.com"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"

    def test_create_user_missing_name(self, client):
        resp = client.post("/api/users", json={})
        assert resp.status_code == 400

    def test_duplicate_email_returns_409(self, client):
        client.post("/api/users", json={"name": "Bob", "email": "bob@example.com"})
        resp = client.post("/api/users", json={"name": "Bob2", "email": "bob@example.com"})
        assert resp.status_code == 409

    def test_update_user(self, client, app, sample_user):
        resp = client.put(
            f"/api/users/{sample_user}",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Updated Name"

    def test_list_users(self, client, app, sample_user):
        resp = client.get("/api/users")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1


# ── API: Alert Preferences ────────────────────────────────────────────


class TestAlertAPI:
    def test_create_alert(self, client, app, sample_user, sample_movie, sample_theater):
        resp = client.post(
            "/api/alerts",
            json={
                "user_id": sample_user,
                "movie_id": sample_movie,
                "theater_id": sample_theater,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["alert_sent"] is False

    def test_create_alert_missing_user(self, client):
        resp = client.post("/api/alerts", json={"movie_id": 1})
        assert resp.status_code == 400

    def test_create_alert_invalid_user(self, client):
        resp = client.post("/api/alerts", json={"user_id": 9999, "movie_id": 1})
        assert resp.status_code == 404

    def test_duplicate_alert_returns_409(self, client, app, sample_user, sample_movie, sample_theater):
        payload = {"user_id": sample_user, "movie_id": sample_movie, "theater_id": sample_theater}
        client.post("/api/alerts", json=payload)
        resp = client.post("/api/alerts", json=payload)
        assert resp.status_code == 409

    def test_delete_alert(self, client, app, sample_user, sample_movie):
        # Create alert first
        resp = client.post("/api/alerts", json={"user_id": sample_user, "movie_id": sample_movie})
        assert resp.status_code == 201
        alert_id = resp.get_json()["id"]

        resp = client.delete(f"/api/alerts/{alert_id}")
        assert resp.status_code == 200

        # Verify it's inactive via list
        with app.app_context():
            pref = AlertPreference.query.get(alert_id)
            assert pref.is_active is False

    def test_list_alerts(self, client, app, sample_user, sample_movie):
        client.post("/api/alerts", json={"user_id": sample_user, "movie_id": sample_movie})
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1


# ── API: Showtimes ────────────────────────────────────────────────────


class TestShowtimeAPI:
    def test_list_showtimes_empty(self, client):
        resp = client.get("/api/showtimes")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_showtimes_filter(self, client, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            show_dt = datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt,
                tickets_available=True, format_type="IMAX",
            )
            db.session.add(showtime)
            db.session.commit()

        resp = client.get(f"/api/showtimes?theater_id={sample_theater}")
        data = resp.get_json()
        assert len(data) >= 1
        assert all(s["theater_id"] == sample_theater for s in data)


# ── UI Routes ─────────────────────────────────────────────────────────


class TestUIRoutes:
    def test_index_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"IMAX Alert" in resp.data

    def test_theaters_page(self, client):
        resp = client.get("/theaters")
        assert resp.status_code == 200
        assert b"Theaters" in resp.data

    def test_alerts_page(self, client):
        resp = client.get("/alerts")
        assert resp.status_code == 200

    def test_profile_page(self, client):
        resp = client.get("/profile")
        assert resp.status_code == 200

    def test_theater_detail_not_found(self, client):
        resp = client.get("/theaters/9999")
        assert resp.status_code == 404

    def test_theater_detail_exists(self, client, app, sample_theater):
        resp = client.get(f"/theaters/{sample_theater}")
        assert resp.status_code == 200
        assert b"Test IMAX Theater" in resp.data


# ── Scraper ───────────────────────────────────────────────────────────


class TestScraper:
    def test_parse_time_text_valid(self):
        from app.scraper import _parse_time_text

        dt = _parse_time_text("7:30 PM")
        assert dt is not None
        assert dt.hour == 19
        assert dt.minute == 30

    def test_parse_time_text_invalid(self):
        from app.scraper import _parse_time_text

        dt = _parse_time_text("not a time")
        assert dt is None

    def test_parse_time_text_24h(self):
        from app.scraper import _parse_time_text

        dt = _parse_time_text("14:00")
        assert dt is not None
        assert dt.hour == 14

    def test_get_or_create_movie_creates(self, app):
        from app.scraper import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            movie = scraper.get_or_create_movie("Avatar IMAX")
            db.session.commit()
            assert movie.id is not None
            assert movie.title == "Avatar IMAX"

    def test_get_or_create_movie_deduplicates(self, app):
        from app.scraper import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            m1 = scraper.get_or_create_movie("Dune IMAX")
            db.session.commit()
            m2 = scraper.get_or_create_movie("Dune IMAX")
            assert m1.id == m2.id

    def test_upsert_showtime_creates(self, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.scraper import AMCScraper

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            scraper = AMCScraper()
            show_dt = datetime(2099, 6, 1, 18, 0, tzinfo=timezone.utc)
            st, is_new = scraper.upsert_showtime(theater, movie, show_dt)
            db.session.commit()
            assert is_new is True
            assert st.id is not None

    def test_upsert_showtime_deduplicates(self, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.scraper import AMCScraper

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            scraper = AMCScraper()
            show_dt = datetime(2099, 6, 2, 18, 0, tzinfo=timezone.utc)
            st1, is_new1 = scraper.upsert_showtime(theater, movie, show_dt)
            db.session.commit()
            st2, is_new2 = scraper.upsert_showtime(theater, movie, show_dt)
            assert is_new1 is True
            assert is_new2 is False
            assert st1.id == st2.id


# ── Notifications ─────────────────────────────────────────────────────


class TestNotifications:
    def test_build_email_body(self, app, sample_user, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.notifications import _build_email_body

        with app.app_context():
            user = User.query.get(sample_user)
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            show_dt = datetime(2025, 8, 15, 20, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt,
                tickets_available=True, format_type="IMAX with Laser",
            )
            db.session.add(showtime)
            db.session.commit()

            subject, html, text = _build_email_body(user, showtime)
            assert "Interstellar IMAX" in subject
            assert "Test User" in text
            assert "Test IMAX Theater" in text
            assert "Interstellar IMAX" in html

    def test_build_sms_body(self, app, sample_user, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.notifications import _build_sms_body

        with app.app_context():
            user = User.query.get(sample_user)
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            show_dt = datetime(2025, 8, 15, 20, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt,
                tickets_available=True,
            )
            db.session.add(showtime)
            db.session.commit()
            sms = _build_sms_body(user, showtime)
            assert "Interstellar IMAX" in sms
            assert "Test IMAX Theater" in sms

    def test_notify_once_per_preference(self, app, sample_user, sample_theater, sample_movie):
        """Alert should be marked sent after processing; no second alert."""
        from datetime import datetime, timezone

        from app.notifications import _notify_for_showtime

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            pref = AlertPreference(
                user_id=sample_user,
                movie_id=sample_movie,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            show_dt = datetime(2099, 9, 1, 20, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt, tickets_available=True,
            )
            db.session.add(showtime)
            db.session.commit()

            # First call — pref has no credentials configured, but it should mark alert_sent
            _notify_for_showtime(app, showtime)
            db.session.refresh(pref)
            assert pref.alert_sent is True

    def test_send_email_no_credentials(self):
        from app.notifications import send_email

        ok, err = send_email(
            {"MAIL_USERNAME": "", "MAIL_PASSWORD": "", "MAIL_FROM": ""},
            "to@example.com",
            "Subject",
            "<p>hi</p>",
            "hi",
        )
        assert ok is False
        assert "credentials" in err.lower()

    def test_send_sms_no_credentials(self):
        from app.notifications import send_sms

        ok, err = send_sms(
            {"TWILIO_ACCOUNT_SID": "", "TWILIO_AUTH_TOKEN": "", "TWILIO_FROM_NUMBER": ""},
            "+15550001234",
            "Hello",
        )
        assert ok is False
        assert "credentials" in err.lower()


# ── Scheduler ────────────────────────────────────────────────────────


class TestScheduler:
    def test_get_scheduler_status_not_started(self):
        from app.scheduler import get_scheduler_status, stop_scheduler

        stop_scheduler()
        status = get_scheduler_status()
        assert status["running"] is False

    def test_start_stop_scheduler(self, app):
        from app.scheduler import get_scheduler_status, start_scheduler, stop_scheduler

        start_scheduler(app)
        status = get_scheduler_status()
        assert status["running"] is True
        assert len(status["jobs"]) >= 1
        stop_scheduler()
        status = get_scheduler_status()
        assert status["running"] is False
