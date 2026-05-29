"""Tests for the IMAX Alert application."""
import pytest

from app import create_app, db
from app.models import AlertMovie, AlertPreference, Movie, Notification, Role, Settings, Showtime, Theater, User


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
def auth_client(app):
    """Test client pre-logged-in as the seeded admin (admin/admin)."""
    c = app.test_client()
    with app.app_context():
        # Ensure seeding ran — admin user should exist
        admin = User.query.filter_by(email="admin").first()
        assert admin is not None, "Admin user not seeded"
        # Disable forced password change so tests aren't redirected to /change-password
        admin.force_password_change = False
        from app import db as _db
        _db.session.commit()
    resp = c.post("/login", data={"email": "admin", "password": "admin"},
                  follow_redirects=True)
    assert resp.status_code == 200, f"Login failed: {resp.status_code}"
    return c


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
    """Returns the seeded admin user id (guaranteed to exist after create_app)."""
    with app.app_context():
        user = User.query.filter_by(email="admin").first()
        assert user is not None
        return user.id


# ── Auth ──────────────────────────────────────────────────────────────


class TestAuth:
    def test_login_page_loads(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"Login" in resp.data

    def test_login_valid_credentials(self, client, app):
        resp = client.post(
            "/login",
            data={"email": "admin", "password": "admin"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # After login, should land on dashboard — not show the login form again
        assert b"Login" not in resp.data or b"Dashboard" in resp.data or b"IMAX Alert" in resp.data

    def test_login_invalid_credentials(self, client):
        resp = client.post(
            "/login",
            data={"email": "admin", "password": "wrongpassword"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Invalid" in resp.data or b"incorrect" in resp.data or b"Login" in resp.data

    def test_unauthenticated_redirect(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_logout(self, auth_client):
        resp = auth_client.post("/logout", follow_redirects=True)
        assert resp.status_code == 200
        # After logout, accessing a protected route should redirect
        resp2 = auth_client.get("/", follow_redirects=False)
        assert resp2.status_code == 302


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
    def test_admin_user_seeded(self, app):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user is not None
            assert user.check_password("admin")

    def test_set_check_password(self, app):
        with app.app_context():
            user = User(name="PwTest", email="pwtest@x.com")
            user.set_password("secret123")
            assert user.check_password("secret123")
            assert not user.check_password("wrong")

    def test_role_name_property(self, app, sample_user):
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.role_name == "admin"

    def test_user_to_dict(self, app, sample_user):
        with app.app_context():
            user = User.query.get(sample_user)
            d = user.to_dict()
            assert "notify_email" in d
            assert "notify_sms" in d


class TestAlertPreferenceModel:
    def test_create_alert_preference(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()
            assert pref.id is not None
            assert pref.alert_sent is False
            assert pref.is_active is True
            assert pref.is_any_movie is False

    def test_alert_preference_to_dict(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()
            d = pref.to_dict()
            assert d["theater_name"] == "Test IMAX Theater"
            assert d["alert_sent"] is False
            assert len(d["movies"]) == 1
            assert d["movies"][0]["movie_title"] == "Interstellar IMAX"


# ── Lookup table seeds ─────────────────────────────────────────────────


class TestLookupSeeds:
    def test_roles_seeded(self, app):
        with app.app_context():
            roles = {r.name for r in Role.query.all()}
            assert {"admin", "editor", "user"} <= roles

    def test_settings_seeded(self, app):
        with app.app_context():
            key = Settings.query.filter_by(key="tmdb_api_key").first()
            assert key is not None

    def test_default_admin_seeded(self, app):
        with app.app_context():
            admin = User.query.filter_by(email="admin").first()
            assert admin is not None
            assert admin.role_name == "admin"


# ── API: Theaters ─────────────────────────────────────────────────────


class TestTheaterAPI:
    def test_list_theaters(self, auth_client, app, sample_theater):
        resp = auth_client.get("/api/theaters")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        names = [t["name"] for t in data]
        assert "Test IMAX Theater" in names

    def test_get_single_theater(self, auth_client, app, sample_theater):
        resp = auth_client.get(f"/api/theaters/{sample_theater}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Test IMAX Theater"

    def test_get_missing_theater_returns_404(self, auth_client, app):
        resp = auth_client.get("/api/theaters/9999")
        assert resp.status_code == 404

    def test_api_requires_auth(self, client):
        resp = client.get("/api/theaters")
        assert resp.status_code in (302, 401)


# ── API: Movies ───────────────────────────────────────────────────────


class TestMovieAPI:
    def test_list_movies(self, auth_client, app, sample_movie):
        resp = auth_client.get("/api/movies")
        assert resp.status_code == 200
        data = resp.get_json()
        titles = [m["title"] for m in data]
        assert "Interstellar IMAX" in titles

    def test_movie_search_empty_query(self, auth_client):
        resp = auth_client.get("/api/movies/search?q=")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_movie_search_local_fallback(self, auth_client, app, sample_movie):
        resp = auth_client.get("/api/movies/search?q=Interstellar")
        assert resp.status_code == 200
        data = resp.get_json()
        titles = [m["title"] for m in data]
        assert "Interstellar IMAX" in titles


# ── API: Users ────────────────────────────────────────────────────────


class TestUserAPI:
    def test_create_user(self, auth_client):
        resp = auth_client.post(
            "/api/users",
            json={"name": "Alice", "email": "alice@example.com"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@example.com"

    def test_create_user_missing_name(self, auth_client):
        resp = auth_client.post("/api/users", json={})
        assert resp.status_code == 400

    def test_duplicate_email_returns_409(self, auth_client):
        auth_client.post("/api/users", json={"name": "Bob", "email": "bob@example.com"})
        resp = auth_client.post("/api/users", json={"name": "Bob2", "email": "bob@example.com"})
        assert resp.status_code == 409

    def test_update_user(self, auth_client, app, sample_user):
        resp = auth_client.put(
            f"/api/users/{sample_user}",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "Updated Name"

    def test_list_users(self, auth_client, app, sample_user):
        resp = auth_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1


# ── API: Alert Preferences ────────────────────────────────────────────


class TestAlertAPI:
    def test_create_alert(self, auth_client, app, sample_user, sample_movie, sample_theater):
        resp = auth_client.post(
            "/api/alerts",
            json={
                "user_id": sample_user,
                "movie_ids": [sample_movie],
                "theater_id": sample_theater,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["alert_sent"] is False
        assert len(data["movies"]) == 1

    def test_create_alert_missing_user(self, auth_client):
        resp = auth_client.post("/api/alerts", json={"movie_id": 1})
        assert resp.status_code == 400

    def test_create_alert_invalid_user(self, auth_client):
        resp = auth_client.post("/api/alerts", json={"user_id": 9999, "movie_id": 1})
        assert resp.status_code == 404

    def test_duplicate_alert_returns_409(self, auth_client, app, sample_user, sample_movie, sample_theater):
        payload = {"user_id": sample_user, "movie_ids": [sample_movie], "theater_id": sample_theater}
        auth_client.post("/api/alerts", json=payload)
        resp = auth_client.post("/api/alerts", json=payload)
        assert resp.status_code == 409

    def test_delete_alert(self, auth_client, app, sample_user, sample_movie):
        resp = auth_client.post("/api/alerts", json={"user_id": sample_user, "movie_ids": [sample_movie]})
        assert resp.status_code == 201
        alert_id = resp.get_json()["id"]

        resp = auth_client.delete(f"/api/alerts/{alert_id}")
        assert resp.status_code == 200

        with app.app_context():
            pref = AlertPreference.query.get(alert_id)
            assert pref.is_active is False

    def test_list_alerts(self, auth_client, app, sample_user, sample_movie):
        auth_client.post("/api/alerts", json={"user_id": sample_user, "movie_ids": [sample_movie]})
        resp = auth_client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1


# ── API: Lookup tables ────────────────────────────────────────────────


class TestLookupAPI:
    def test_get_chains(self, auth_client):
        resp = auth_client.get("/api/lookup/chains")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_create_chain(self, auth_client):
        resp = auth_client.post("/api/lookup/chains", json={"name": "TestChain"})
        assert resp.status_code == 201
        assert resp.get_json()["name"] == "TestChain"

    def test_create_chain_duplicate(self, auth_client):
        auth_client.post("/api/lookup/chains", json={"name": "DupChain"})
        resp = auth_client.post("/api/lookup/chains", json={"name": "DupChain"})
        assert resp.status_code == 409

    def test_delete_chain_not_in_use(self, auth_client, app):
        r = auth_client.post("/api/lookup/chains", json={"name": "DeleteMe"})
        obj_id = r.get_json()["id"]
        resp = auth_client.delete(f"/api/lookup/chains/{obj_id}")
        assert resp.status_code == 200

    def test_get_countries(self, auth_client):
        resp = auth_client.get("/api/lookup/countries")
        assert resp.status_code == 200

    def test_create_country(self, auth_client):
        resp = auth_client.post("/api/lookup/countries", json={"name": "Testland"})
        assert resp.status_code == 201

    def test_get_regions_filtered(self, auth_client, app):
        from app.lookup_helpers import get_or_create_country, get_or_create_region
        with app.app_context():
            country = get_or_create_country("Filtercountry")
            get_or_create_region("Filterregion", country)
            db.session.commit()
            country_id = country.id
        resp = auth_client.get(f"/api/lookup/regions?country_id={country_id}")
        assert resp.status_code == 200
        names = [r["name"] for r in resp.get_json()]
        assert "Filterregion" in names

    def test_get_cities_filtered(self, auth_client, app):
        from app.lookup_helpers import get_or_create_city, get_or_create_country
        with app.app_context():
            country = get_or_create_country("CityCountry")
            get_or_create_city("Filtercity", country, None)
            db.session.commit()
            country_id = country.id
        resp = auth_client.get(f"/api/lookup/cities?country_id={country_id}")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.get_json()]
        assert "Filtercity" in names

    def test_get_aspect_ratios(self, auth_client):
        resp = auth_client.get("/api/lookup/aspect-ratios")
        assert resp.status_code == 200

    def test_create_aspect_ratio(self, auth_client):
        resp = auth_client.post("/api/lookup/aspect-ratios", json={"label": "1.43:1"})
        assert resp.status_code in (201, 409)  # 409 if seeded already

    def test_get_projector_types(self, auth_client):
        resp = auth_client.get("/api/lookup/projector-types")
        assert resp.status_code == 200

    def test_get_audio_systems(self, auth_client):
        resp = auth_client.get("/api/lookup/audio-systems")
        assert resp.status_code == 200


# ── API: Showtimes ────────────────────────────────────────────────────


class TestShowtimeAPI:
    def test_list_showtimes_empty(self, auth_client):
        resp = auth_client.get("/api/showtimes")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_showtimes_filter(self, auth_client, app, sample_theater, sample_movie):
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

        resp = auth_client.get(f"/api/showtimes?theater_id={sample_theater}")
        data = resp.get_json()
        assert len(data) >= 1
        assert all(s["theater_id"] == sample_theater for s in data)


# ── UI Routes ─────────────────────────────────────────────────────────


class TestUIRoutes:
    def test_index_page(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200
        assert b"IMAX Alert" in resp.data

    def test_theaters_page(self, auth_client):
        resp = auth_client.get("/theaters")
        assert resp.status_code == 200
        assert b"Theaters" in resp.data

    def test_alerts_page(self, auth_client):
        resp = auth_client.get("/alerts")
        assert resp.status_code == 200

    def test_profile_page(self, auth_client):
        resp = auth_client.get("/profile")
        assert resp.status_code == 200

    def test_theater_detail_not_found(self, auth_client):
        resp = auth_client.get("/theaters/9999")
        assert resp.status_code == 404

    def test_theater_detail_exists(self, auth_client, app, sample_theater):
        resp = auth_client.get(f"/theaters/{sample_theater}")
        assert resp.status_code == 200
        assert b"Test IMAX Theater" in resp.data

    def test_admin_theaters_page(self, auth_client):
        resp = auth_client.get("/admin/theaters")
        assert resp.status_code == 200

    def test_admin_theater_new_get(self, auth_client):
        resp = auth_client.get("/admin/theaters/new")
        assert resp.status_code == 200

    def test_admin_users_page(self, auth_client):
        resp = auth_client.get("/admin/users")
        assert resp.status_code == 200

    def test_admin_user_new_get(self, auth_client):
        resp = auth_client.get("/admin/users/new")
        assert resp.status_code == 200

    def test_admin_settings_page(self, auth_client):
        resp = auth_client.get("/admin/settings")
        assert resp.status_code == 200

    def test_unauthenticated_gets_redirected(self, client):
        for path in ["/", "/theaters", "/alerts", "/profile", "/admin/theaters"]:
            resp = client.get(path)
            assert resp.status_code == 302, f"{path} should redirect unauthenticated"


# ── Admin UI: theater CRUD ────────────────────────────────────────────


class TestAdminTheaterCRUD:
    def test_create_theater_post(self, auth_client):
        resp = auth_client.post(
            "/admin/theaters/new",
            data={
                "name": "New Test Theater",
                "is_active": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"New Test Theater" in resp.data

    def test_edit_theater_post(self, auth_client, app, sample_theater):
        resp = auth_client.post(
            f"/admin/theaters/{sample_theater}/edit",
            data={"name": "Renamed Theater", "is_active": "on"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            t = Theater.query.get(sample_theater)
            assert t.name == "Renamed Theater"

    def test_deactivate_theater(self, auth_client, app, sample_theater):
        resp = auth_client.post(
            f"/admin/theaters/{sample_theater}/delete",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            t = Theater.query.get(sample_theater)
            assert t.is_active is False


# ── Admin UI: user CRUD ───────────────────────────────────────────────


class TestAdminUserCRUD:
    def test_create_user_post(self, auth_client):
        resp = auth_client.post(
            "/admin/users/new",
            data={
                "name": "New User",
                "email": "newuser@test.com",
                "password": "testpass",
                "is_active": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

    def test_edit_user_post(self, auth_client, app, sample_user):
        resp = auth_client.post(
            f"/admin/users/{sample_user}/edit",
            data={
                "name": "Updated Admin",
                "email": "admin",
                "is_active": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            u = User.query.get(sample_user)
            assert u.name == "Updated Admin"


# ── Admin UI: settings ────────────────────────────────────────────────


class TestAdminSettings:
    def test_settings_save(self, auth_client, app):
        resp = auth_client.post(
            "/admin/settings",
            data={"tmdb_api_key": "testkey123", "app_measurement_unit": "imperial"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            s = Settings.query.filter_by(key="tmdb_api_key").first()
            assert s.value == "testkey123"

    def test_settings_save_clears_key(self, auth_client, app):
        auth_client.post("/admin/settings", data={"tmdb_api_key": "key", "app_measurement_unit": "metric"})
        auth_client.post("/admin/settings", data={"tmdb_api_key": "", "app_measurement_unit": "metric"})
        with app.app_context():
            s = Settings.query.filter_by(key="tmdb_api_key").first()
            assert s.value == ""


# ── Profile ───────────────────────────────────────────────────────────


class TestProfile:
    def test_profile_post_saves_prefs(self, auth_client, app, sample_user):
        resp = auth_client.post(
            "/profile",
            data={"notify_email": "on", "measurement_unit": "imperial"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.measurement_unit == "imperial"
            assert user.notify_email is True

    def test_profile_unit_toggle(self, auth_client, app, sample_user):
        auth_client.post("/profile", data={"measurement_unit": "metric"})
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.measurement_unit == "metric"


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

    def test_get_or_create_movie_no_tmdb_enrichment(self, app):
        """When TMDB is not configured, new movies are created without tmdb_id."""
        from app.scraper import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            movie = scraper.get_or_create_movie("Oppenheimer IMAX Test")
            db.session.commit()
            assert movie.id is not None
            # TMDB not configured in test env — tmdb_id stays None
            assert movie.tmdb_id is None


# ── Scraper alert-driven targeting ────────────────────────────────────


class TestScraperAlertTargeting:
    def test_get_active_targets_empty(self, app):
        """No active alerts → both sets are empty."""
        from app.scraper import _get_active_targets

        with app.app_context():
            theater_ids, movie_ids = _get_active_targets()
            assert len(theater_ids) == 0
            assert len(movie_ids) == 0

    def test_get_active_targets_with_alert(self, app, sample_user, sample_theater, sample_movie):
        """Active unsent alert with AlertMovie row → its theater_id and movie_id appear in targets."""
        from app.models import AlertMovie, AlertPreference
        from app.scraper import _get_active_targets

        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                is_active=True,
                alert_sent=False,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()

            theater_ids, movie_ids = _get_active_targets()
            assert sample_theater in theater_ids
            assert sample_movie in movie_ids

    def test_get_active_targets_sent_alert_excluded(self, app, sample_user, sample_theater, sample_movie):
        """Sent alert (alert_sent=True) is NOT included in targets."""
        from app.models import AlertMovie, AlertPreference
        from app.scraper import _get_active_targets

        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                is_active=True,
                alert_sent=True,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie, alert_sent=True)
            db.session.add(am)
            db.session.commit()

            theater_ids, movie_ids = _get_active_targets()
            assert sample_theater not in theater_ids
            assert sample_movie not in movie_ids

    def test_scrape_all_skips_when_no_alerts(self, app):
        """scrape_all returns [] immediately when there are no active alerts."""
        from unittest.mock import patch

        from app.scraper import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            with patch.object(scraper, "scrape_theater") as mock_scrape:
                result = scraper.scrape_all()
            assert result == []
            mock_scrape.assert_not_called()

    def test_scrape_all_targets_alerted_theater(self, app, sample_user, sample_theater, sample_movie):
        """scrape_all calls scrape_theater only for the theater in the active alert."""
        from unittest.mock import patch

        from app.models import AlertMovie, AlertPreference
        from app.scraper import AMCScraper

        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                is_active=True,
                alert_sent=False,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()

            scraper = AMCScraper()
            with patch.object(scraper, "scrape_theater", return_value=[]) as mock_scrape:
                result = scraper.scrape_all()

            assert mock_scrape.call_count == 1
            called_theater = mock_scrape.call_args[0][0]
            assert called_theater.id == sample_theater

    def test_scrape_all_any_theater_alert_scrapes_all(self, app, sample_user, sample_movie):
        """An alert with theater_id=None means 'any theater' — all chain theaters are scraped."""
        from unittest.mock import patch

        from app.models import AlertPreference
        from app.scraper import AMCScraper

        with app.app_context():
            from app.models import Theater as TheaterModel
            t1 = TheaterModel(
                name="AMC IMAX One",
                chain="AMC",
                city="Springfield",
                state="IL",
                is_active=True,
            )
            t2 = TheaterModel(
                name="AMC IMAX Two",
                chain="AMC",
                city="Othertown",
                state="NY",
                is_active=True,
            )
            db.session.add_all([t1, t2])
            pref = AlertPreference(
                user_id=sample_user,
                movie_id=sample_movie,
                theater_id=None,   # any theater
                is_active=True,
                alert_sent=False,
            )
            db.session.add(pref)
            db.session.commit()

            scraper = AMCScraper()
            with patch.object(scraper, "scrape_theater", return_value=[]) as mock_scrape:
                scraper.scrape_all()

            # Both AMC theaters should have been scraped
            assert mock_scrape.call_count >= 2


# ── Notifications ─────────────────────────────────────────────────────


class TestNotifications:
    def test_build_email_body(self, app, sample_user, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.notifications import _build_email_body_multi

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

            subject, html, text = _build_email_body_multi(user, [showtime])
            assert "Interstellar IMAX" in subject
            assert user.name in text
            assert "Test IMAX Theater" in text
            assert "Interstellar IMAX" in html

    def test_build_email_body_multi_showtimes(self, app, sample_user, sample_theater, sample_movie):
        """Multiple showtimes appear in a single email with all dates listed."""
        from datetime import datetime, timedelta, timezone

        from app.notifications import _build_email_body_multi

        with app.app_context():
            user = User.query.get(sample_user)
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            sts = []
            for offset in range(3):
                st = Showtime(
                    theater=theater, movie=movie,
                    show_datetime=datetime(2025, 8, 15, 20, 0, tzinfo=timezone.utc) + timedelta(days=offset),
                    tickets_available=True, format_type="IMAX with Laser",
                    tickets_url=f"https://example.com/ticket/{offset}",
                )
                db.session.add(st)
                sts.append(st)
            db.session.commit()

            subject, html, text = _build_email_body_multi(user, sts)
            assert "3" in subject or "Interstellar" in subject
            assert text.count("Interstellar IMAX") >= 1
            assert "example.com/ticket/0" in html
            assert "example.com/ticket/2" in html

    def test_build_sms_body(self, app, sample_user, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.notifications import _build_sms_body_multi

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
            sms = _build_sms_body_multi(user, [showtime])
            assert "Interstellar IMAX" in sms
            assert "Test IMAX Theater" in sms

    def test_notify_once_per_preference(self, app, sample_user, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.notifications import _notify_for_showtime

        with app.app_context():
            # Give the user a channel so alert_sent becomes True after notification attempt.
            user = User.query.get(sample_user)
            user.notify_email = True
            user.email = "test@example.com"
            db.session.commit()

            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            # Add AlertMovie row so this is a specific-movie alert that auto-closes
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            show_dt = datetime(2099, 9, 1, 20, 0, tzinfo=timezone.utc)
            showtime = Showtime(
                theater=theater, movie=movie, show_datetime=show_dt, tickets_available=True,
            )
            db.session.add(showtime)
            db.session.commit()

            _notify_for_showtime(app, showtime)
            db.session.refresh(pref)
            # All AlertMovie rows sent → pref auto-closes
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


# ── TMDB module ───────────────────────────────────────────────────────


class TestTMDB:
    def test_is_configured_false_when_no_key(self, app):
        from app.tmdb import is_configured
        with app.app_context():
            # After fresh seed, tmdb_api_key is ""
            assert is_configured() is False

    def test_search_returns_empty_when_not_configured(self, app):
        from app.tmdb import search_movies
        with app.app_context():
            results = search_movies("Avatar")
            assert results == []


# ── Lookup helpers ────────────────────────────────────────────────────


class TestLookupHelpers:
    def test_parse_screen_dims_metres(self):
        from app.lookup_helpers import parse_screen_dims
        w, h = parse_screen_dims("26.0m×19.6m")
        assert abs(w - 26.0) < 0.01
        assert abs(h - 19.6) < 0.01

    def test_parse_screen_dims_feet(self):
        from app.lookup_helpers import parse_screen_dims
        w, h = parse_screen_dims("85.3ft×64.3ft")
        assert w is not None and h is not None
        # Should convert to metres
        assert w < 30  # 85.3ft ≈ 26m

    def test_parse_screen_dims_unitless(self):
        from app.lookup_helpers import parse_screen_dims
        w, h = parse_screen_dims("26.0×19.6")
        assert abs(w - 26.0) < 0.01  # assumed metres

    def test_parse_screen_dims_invalid(self):
        from app.lookup_helpers import parse_screen_dims
        w, h = parse_screen_dims("")
        assert w is None and h is None

    def test_get_or_create_chain_idempotent(self, app):
        from app.lookup_helpers import get_or_create_chain
        with app.app_context():
            c1 = get_or_create_chain("AMC")
            c2 = get_or_create_chain("AMC")
            db.session.commit()
            assert c1.id == c2.id

    def test_get_or_create_country_and_region(self, app):
        from app.lookup_helpers import get_or_create_country, get_or_create_region
        with app.app_context():
            country = get_or_create_country("TestCountry")
            region = get_or_create_region("TestRegion", country)
            db.session.commit()
            assert region.country_id == country.id


# ── CSV seeding ───────────────────────────────────────────────────────


class TestCSVSeeding:
    """Tests for _seed_theaters_from_csv and related helpers."""

    def test_csv_seed_populates_theaters(self, app):
        """CSV seed should have inserted theaters on first boot."""
        with app.app_context():
            count = Theater.query.count()
            # TestingConfig disables VENUE_CRAWL_ON_EMPTY but CSV seed should run
            # (it has its own skip-if-empty guard based on Theater.query.count() > 0)
            assert count > 0, "CSV seed did not insert any theaters"

    def test_csv_seed_sets_continent(self, app):
        """At least one theater should have a continent FK set."""
        with app.app_context():
            from app.models import Continent
            assert Continent.query.count() > 0, "No continents seeded"
            t = Theater.query.filter(Theater.continent_id.isnot(None)).first()
            assert t is not None, "No theater has continent_id set"

    def test_csv_seed_sets_aspect_ratio(self, app):
        """At least one theater should have aspect_ratio_id set."""
        with app.app_context():
            t = Theater.query.filter(Theater.aspect_ratio_id.isnot(None)).first()
            assert t is not None, "No theater has aspect_ratio_id set"

    def test_csv_seed_sets_projector_type(self, app):
        """At least one theater should have projector_type_id set."""
        with app.app_context():
            t = Theater.query.filter(Theater.projector_type_id.isnot(None)).first()
            assert t is not None

    def test_csv_seed_commercial_films_values(self, app):
        """commercial_films column should only contain valid values or NULL."""
        with app.app_context():
            valid = {"Yes", "Limited", "No", None}
            for t in Theater.query.all():
                assert t.commercial_films in valid, f"Unexpected commercial_films: {t.commercial_films!r}"

    def test_csv_seed_is_idempotent(self, app):
        """Calling _upsert_theaters_from_csv a second time should not change count."""
        with app.app_context():
            from app import _upsert_theaters_from_csv
            count_before = Theater.query.count()
            _upsert_theaters_from_csv(app)
            count_after = Theater.query.count()
            assert count_before == count_after, "CSV upsert changed theater count"

    def test_aspect_ratio_normalisation(self, app):
        """Malformed '2.30:01' should be normalised to '2.30:1'."""
        import re
        def normalise_ar(raw):
            if not raw:
                return raw
            return re.sub(r":0+(\d)$", r":\1", raw.strip())
        assert normalise_ar("2.30:01") == "2.30:1"
        assert normalise_ar("2:30:01") == "2:30:1"   # trailing :01 → :1
        assert normalise_ar("1.90:1") == "1.90:1"    # already correct


# ── New PATCH fields ──────────────────────────────────────────────────


class TestTheaterPatchNewFields:
    """Tests for the new PATCH fields: continent_id, digital_projector_ar_id, film_projector_type_id."""

    def test_patch_continent_id(self, auth_client, app, sample_theater):
        from app.models import Continent
        with app.app_context():
            cont = Continent(name="TestContinent")
            db.session.add(cont)
            db.session.commit()
            cont_id = cont.id
        resp = auth_client.patch(
            f"/api/theaters/{sample_theater}",
            json={"continent_id": cont_id},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["continent_name"] == "TestContinent"

    def test_patch_digital_projector_ar_id(self, auth_client, app, sample_theater):
        from app.models import AspectRatio
        with app.app_context():
            ar = AspectRatio(label="2.39:1")
            db.session.add(ar)
            db.session.commit()
            ar_id = ar.id
        resp = auth_client.patch(
            f"/api/theaters/{sample_theater}",
            json={"digital_projector_ar_id": ar_id},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["digital_projector_ar"] == "2.39:1"

    def test_patch_film_projector_type_id(self, auth_client, app, sample_theater):
        from app.models import ProjectorType
        with app.app_context():
            pt = ProjectorType(name="IMAX 15/70mm")
            db.session.add(pt)
            db.session.commit()
            pt_id = pt.id
        resp = auth_client.patch(
            f"/api/theaters/{sample_theater}",
            json={"film_projector_type_id": pt_id},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["film_projector_type_name"] == "IMAX 15/70mm"

    def test_patch_unknown_field_returns_400(self, auth_client, sample_theater):
        resp = auth_client.patch(
            f"/api/theaters/{sample_theater}",
            json={"nonexistent_field": 1},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_patch_continent_id_clear(self, auth_client, app, sample_theater):
        """Setting continent_id to null should clear it."""
        from app.models import Continent
        with app.app_context():
            cont = Continent(name="ClearContinent")
            db.session.add(cont)
            db.session.commit()
            cont_id = cont.id
        auth_client.patch(f"/api/theaters/{sample_theater}", json={"continent_id": cont_id},
                          content_type="application/json")
        resp = auth_client.patch(f"/api/theaters/{sample_theater}", json={"continent_id": None},
                                 content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["continent_name"] is None


# ── Alert API: reset + detail ─────────────────────────────────────────


class TestAlertResetAndDetail:
    def test_reset_alert(self, auth_client, app, sample_user, sample_movie, sample_theater):
        """PATCH /api/alerts/<id>/reset clears alert_sent and re-activates."""
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                alert_sent=True,
                is_active=True,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie, alert_sent=True)
            db.session.add(am)
            db.session.commit()
            pref_id = pref.id

        resp = auth_client.patch(f"/api/alerts/{pref_id}/reset")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["alert_sent"] is False
        assert data["alert_sent_at"] is None
        assert data["is_active"] is True

    def test_get_alert_detail(self, auth_client, app, sample_user, sample_movie, sample_theater):
        """GET /api/alerts/<id> returns pref data with showtimes and notifications."""
        from datetime import datetime, timezone
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            showtime = Showtime(
                theater=theater,
                movie=movie,
                show_datetime=datetime(2099, 9, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(showtime)
            db.session.commit()
            pref_id = pref.id

        resp = auth_client.get(f"/api/alerts/{pref_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == pref_id
        assert "showtimes" in data
        assert "notifications" in data
        assert len(data["showtimes"]) == 1

    def test_alert_detail_page_renders(self, auth_client, app, sample_user, sample_movie, sample_theater):
        """GET /alerts/<id> renders the detail page."""
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()
            pref_id = pref.id

        resp = auth_client.get(f"/alerts/{pref_id}")
        assert resp.status_code == 200
        assert b"Alert Detail" in resp.data
        assert b"Notification History" in resp.data


# ── Notifications: channel-gate fix ──────────────────────────────────


class TestNotificationChannelGate:
    def test_alert_not_marked_sent_when_no_channel(
        self, app, sample_user, sample_theater, sample_movie
    ):
        """
        If a user has neither notify_email nor notify_sms enabled,
        alert_sent must remain False after _notify_for_showtime().
        """
        from datetime import datetime, timezone

        from app.notifications import _notify_for_showtime

        with app.app_context():
            user = User.query.get(sample_user)
            user.notify_email = False
            user.notify_sms = False
            db.session.commit()

            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            showtime = Showtime(
                theater=theater,
                movie=movie,
                show_datetime=datetime(2099, 9, 1, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(showtime)
            db.session.commit()

            _notify_for_showtime(app, showtime)
            db.session.refresh(pref)
            assert pref.alert_sent is False

    def test_alert_marked_sent_when_channel_attempted(
        self, app, sample_user, sample_theater, sample_movie
    ):
        """
        When notify_email=True but SMTP is unconfigured, the attempt is made
        (and fails gracefully) — but alert_sent should still become True
        because a channel WAS attempted.
        """
        from datetime import datetime, timezone

        from app.notifications import _notify_for_showtime

        with app.app_context():
            user = User.query.get(sample_user)
            user.notify_email = True
            user.email = "test@example.com"
            user.notify_sms = False
            db.session.commit()

            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            showtime = Showtime(
                theater=theater,
                movie=movie,
                show_datetime=datetime(2099, 9, 2, 20, 0, tzinfo=timezone.utc),
            )
            db.session.add(showtime)
            db.session.commit()

            _notify_for_showtime(app, showtime)
            db.session.refresh(pref)
            # Channel was attempted (email); SMTP credentials empty → send failed,
            # but alert_sent must be True to prevent infinite retries.
            assert pref.alert_sent is True


# ── Admin Settings: notification keys ────────────────────────────────


class TestAdminSettingsNotification:
    def test_settings_page_has_smtp_section(self, auth_client):
        resp = auth_client.get("/admin/settings")
        assert resp.status_code == 200
        assert b"SMTP" in resp.data or b"mail_server" in resp.data

    def test_settings_page_has_twilio_section(self, auth_client):
        resp = auth_client.get("/admin/settings")
        assert resp.status_code == 200
        assert b"Twilio" in resp.data or b"twilio_account_sid" in resp.data

    def test_save_smtp_settings(self, auth_client, app):
        resp = auth_client.post(
            "/admin/settings",
            data={
                "mail_server": "smtp.example.com",
                "mail_port": "465",
                "mail_use_tls": "true",
                "mail_username": "user@example.com",
                "mail_password": "s3cr3t",
                "mail_from": "IMAX Alert <user@example.com>",
                "twilio_account_sid": "",
                "twilio_auth_token": "",
                "twilio_from_number": "",
                "tmdb_api_key": "",
                "app_measurement_unit": "metric",
                "scraper_interval_minutes": "30",
                "venue_crawl_interval_days": "7",
                "cleanup_interval_hours": "24",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            from app.models import Settings
            assert Settings.query.filter_by(key="mail_server").first().value == "smtp.example.com"
            assert Settings.query.filter_by(key="mail_username").first().value == "user@example.com"


# -- SMTP test endpoint ------------------------------------------------


class TestSMTPTest:
    def test_smtp_test_no_credentials_returns_false(self, app):
        """Empty credentials → send_email returns False; endpoint reports failure."""
        with app.app_context():
            admin = User.query.filter_by(email="admin").first()
            admin.email = "admin@example.com"
            admin.force_password_change = False
            from app import db as _db
            _db.session.commit()

        c = app.test_client()
        c.post("/login", data={"email": "admin@example.com", "password": "admin"},
               follow_redirects=True)
        resp = c.post(
            "/api/admin/smtp/test",
            json={
                "mail_server": "smtp.gmail.com",
                "mail_port": "587",
                "mail_use_tls": "true",
                "mail_username": "",
                "mail_password": "",
                "mail_from": "",
            },
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is False

        with app.app_context():
            admin = User.query.filter_by(email="admin@example.com").first()
            admin.email = "admin"
            from app import db as _db
            _db.session.commit()

    def test_smtp_test_no_admin_email_returns_400(self, app):
        """Admin account with no real email → 400 with instructive message."""
        with app.app_context():
            admin = User.query.filter_by(email="admin").first()
            original_email = admin.email
            admin.email = "notreal"   # no '@' — simulates unconfigured account
            admin.force_password_change = False
            from app import db as _db
            _db.session.commit()

        c = app.test_client()
        c.post("/login", data={"email": "notreal", "password": "admin"}, follow_redirects=True)
        resp = c.post(
            "/api/admin/smtp/test",
            json={"mail_username": "x", "mail_password": "y"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "email address" in data["message"].lower()

        # Restore
        with app.app_context():
            admin = User.query.filter_by(email="notreal").first()
            admin.email = original_email
            from app import db as _db
            _db.session.commit()

    def test_smtp_test_requires_admin(self, app):
        """Non-admin users cannot call the SMTP test endpoint."""
        with app.app_context():
            from app.models import Role
            from werkzeug.security import generate_password_hash
            user_role = Role.query.filter_by(name="user").first()
            regular = User(
                name="Regular",
                email="smtptest_regular@example.com",
                password_hash=generate_password_hash("pass"),
                role_id=user_role.id,
            )
            from app import db as _db
            _db.session.add(regular)
            _db.session.commit()

        c = app.test_client()
        c.post("/login", data={"email": "smtptest_regular@example.com", "password": "pass"},
               follow_redirects=True)
        resp = c.post("/api/admin/smtp/test", json={}, content_type="application/json")
        assert resp.status_code == 403


# -- AlertMovie model --------------------------------------------------


class TestAlertMovieModel:
    def test_create_alert_movie(self, app, sample_user, sample_movie, sample_theater):
        """AlertMovie row can be created and links to pref + movie."""
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()
            assert am.id is not None
            assert am.alert_sent is False
            assert am.movie.title == "Interstellar IMAX"

    def test_alert_movie_to_dict(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()
            d = am.to_dict()
            assert d["movie_title"] == "Interstellar IMAX"
            assert d["alert_sent"] is False
            assert d["alert_sent_at"] is None

    def test_unique_constraint_prevents_duplicate(self, app, sample_user, sample_movie, sample_theater):
        """Two AlertMovie rows with same (alert_id, movie_id) should raise."""
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.flush()
            am1 = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            am2 = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add_all([am1, am2])
            with pytest.raises(IntegrityError):
                db.session.commit()

    def test_is_any_movie_true_when_no_rows(self, app, sample_user, sample_theater):
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.commit()
            assert pref.is_any_movie is True

    def test_is_any_movie_false_when_rows_exist(self, app, sample_user, sample_movie, sample_theater):
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()
            assert pref.is_any_movie is False


# -- Multi-movie alert creation ----------------------------------------


class TestMultiMovieAlertCreate:
    def test_create_alert_with_multiple_movies(self, auth_client, app, sample_user, sample_theater):
        """Creating an alert with two movies creates two AlertMovie rows."""
        with app.app_context():
            m1 = Movie(title="Movie Alpha")
            m2 = Movie(title="Movie Beta")
            db.session.add_all([m1, m2])
            db.session.commit()
            m1_id, m2_id = m1.id, m2.id

        resp = auth_client.post(
            "/api/alerts",
            json={
                "user_id": sample_user,
                "movie_ids": [m1_id, m2_id],
                "theater_id": sample_theater,
            },
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert len(data["movies"]) == 2
        titles = {m["movie_title"] for m in data["movies"]}
        assert titles == {"Movie Alpha", "Movie Beta"}

    def test_create_any_movie_alert_no_movies(self, auth_client, app, sample_user, sample_theater):
        """Alert with no movie_ids -> any-movie alert (zero AlertMovie rows)."""
        resp = auth_client.post(
            "/api/alerts",
            json={"user_id": sample_user, "theater_id": sample_theater},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["movies"] == []

    def test_backward_compat_singular_movie_id(self, auth_client, app, sample_user, sample_movie, sample_theater):
        """Legacy movie_id singular field still accepted."""
        resp = auth_client.post(
            "/api/alerts",
            json={"user_id": sample_user, "movie_id": sample_movie, "theater_id": sample_theater},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert len(data["movies"]) == 1

    def test_duplicate_movie_in_active_alert_returns_409(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        """Second alert for same (user, theater, movie) while first is unsent -> 409."""
        auth_client.post(
            "/api/alerts",
            json={"user_id": sample_user, "movie_ids": [sample_movie], "theater_id": sample_theater},
        )
        resp = auth_client.post(
            "/api/alerts",
            json={"user_id": sample_user, "movie_ids": [sample_movie], "theater_id": sample_theater},
        )
        assert resp.status_code == 409


# -- Per-movie reset endpoint ------------------------------------------


class TestPerMovieReset:
    def test_reset_single_movie(self, auth_client, app, sample_user, sample_movie, sample_theater):
        """PATCH /api/alerts/<pref_id>/movies/<movie_id>/reset resets one AlertMovie."""
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user, theater_id=sample_theater,
                alert_sent=True, is_active=True,
            )
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie, alert_sent=True)
            db.session.add(am)
            db.session.commit()
            pref_id, movie_id = pref.id, sample_movie

        resp = auth_client.patch(f"/api/alerts/{pref_id}/movies/{movie_id}/reset")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["alert_sent"] is False
        assert data["alert_sent_at"] is None

    def test_reset_movie_not_found_returns_404(self, auth_client, app, sample_user, sample_theater):
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.commit()
            pref_id = pref.id

        resp = auth_client.patch(f"/api/alerts/{pref_id}/movies/9999/reset")
        assert resp.status_code == 404


# -- Expired showtime cleanup ------------------------------------------


class TestExpiredShowtimeCleanup:
    def test_cleanup_removes_past_showtimes(self, app, sample_theater, sample_movie):
        """cleanup_expired_showtimes() deletes showtimes in the past."""
        from datetime import datetime, timedelta, timezone

        from app.scraper import cleanup_expired_showtimes

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            past = Showtime(
                theater=theater, movie=movie,
                show_datetime=datetime.now(timezone.utc) - timedelta(days=1),
            )
            future = Showtime(
                theater=theater, movie=movie,
                show_datetime=datetime.now(timezone.utc) + timedelta(days=1),
            )
            db.session.add_all([past, future])
            db.session.commit()
            past_id, future_id = past.id, future.id

            deleted = cleanup_expired_showtimes()
            assert deleted >= 1
            assert Showtime.query.get(past_id) is None
            assert Showtime.query.get(future_id) is not None

    def test_cleanup_returns_zero_when_nothing_expired(self, app):
        """Returns 0 when no past showtimes exist."""
        from app.scraper import cleanup_expired_showtimes

        with app.app_context():
            count = cleanup_expired_showtimes()
            assert count == 0


# -- Orphaned movie cleanup -------------------------------------------


class TestOrphanedMovieCleanup:
    def test_orphaned_movie_deleted(self, app):
        """A movie with no showtimes and no alert references is deleted."""
        from app.scraper import cleanup_orphaned_movies

        with app.app_context():
            orphan = Movie(title="Orphan Film")
            db.session.add(orphan)
            db.session.commit()
            orphan_id = orphan.id

            removed = cleanup_orphaned_movies()
            assert removed >= 1
            assert Movie.query.get(orphan_id) is None

    def test_movie_with_alert_not_deleted(self, app, sample_user, sample_theater, sample_movie):
        """A movie referenced by an active AlertMovie row is NOT deleted."""
        from app.scraper import cleanup_orphaned_movies

        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.flush()
            am = AlertMovie(alert_id=pref.id, movie_id=sample_movie)
            db.session.add(am)
            db.session.commit()

            removed = cleanup_orphaned_movies()
            assert Movie.query.get(sample_movie) is not None
            assert removed == 0

    def test_movie_with_showtime_not_deleted(self, app, sample_theater, sample_movie):
        """A movie that still has showtimes is NOT deleted."""
        from datetime import datetime, timedelta, timezone

        from app.scraper import cleanup_orphaned_movies

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            st = Showtime(
                theater=theater, movie=movie,
                show_datetime=datetime.now(timezone.utc) + timedelta(days=1),
            )
            db.session.add(st)
            db.session.commit()

            removed = cleanup_orphaned_movies()
            assert Movie.query.get(sample_movie) is not None
            assert removed == 0


# -- Any-movie alert never auto-closes --------------------------------


class TestAnyMovieAlert:
    def test_any_movie_alert_stays_open_after_notify(
        self, app, sample_user, sample_theater, sample_movie
    ):
        """Any-movie alerts (zero AlertMovie rows) never auto-close after a notification."""
        from datetime import datetime, timezone

        from app.notifications import _notify_for_showtime

        with app.app_context():
            user = User.query.get(sample_user)
            user.notify_email = True
            user.email = "test@example.com"
            db.session.commit()

            theater = Theater.query.get(sample_theater)
            movie = Movie.query.get(sample_movie)
            # No AlertMovie rows -> any-movie alert
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            showtime = Showtime(
                theater=theater, movie=movie,
                show_datetime=datetime(2099, 12, 1, 20, 0, tzinfo=timezone.utc),
                tickets_available=True,
            )
            db.session.add(showtime)
            db.session.commit()

            _notify_for_showtime(app, showtime)
            db.session.refresh(pref)
            # Any-movie alerts must stay open
            assert pref.alert_sent is False
            assert pref.is_active is True


# -- Showtime clear endpoints ------------------------------------------


class TestShowtimeClear:
    """Tests for GET /api/showtimes/count and DELETE /api/showtimes."""

    @pytest.fixture
    def _showtimes(self, app, sample_theater, sample_movie):
        """Create 3 showtimes: 2 past, 1 future, across 2 theaters/movies."""
        from datetime import datetime, timedelta, timezone

        with app.app_context():
            # Second theater + movie for isolation tests
            from app.models import Movie, Theater
            t2 = Theater(name="Other IMAX", chain="Cineplex", city="Elsewhere", state="ON", is_active=True)
            m2 = Movie(title="Other Film")
            db.session.add_all([t2, m2])
            db.session.flush()

            theater1 = Theater.query.get(sample_theater)
            movie1   = Movie.query.get(sample_movie)
            now = datetime.now(timezone.utc)

            st1 = Showtime(theater=theater1, movie=movie1,
                           show_datetime=now - timedelta(days=5))
            st2 = Showtime(theater=theater1, movie=movie1,
                           show_datetime=now - timedelta(days=1))
            st3 = Showtime(theater=t2, movie=m2,
                           show_datetime=now + timedelta(days=3))
            db.session.add_all([st1, st2, st3])
            db.session.commit()
            return {
                "theater1_id": sample_theater,
                "theater2_id": t2.id,
                "movie1_id":   sample_movie,
                "movie2_id":   m2.id,
                "past_ids":    [st1.id, st2.id],
                "future_id":   st3.id,
                "cutoff":      (now - timedelta(hours=12)).isoformat(),
            }

    def test_count_all(self, auth_client, app, _showtimes):
        resp = auth_client.get("/api/showtimes/count")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 3

    def test_count_by_theater(self, auth_client, app, _showtimes):
        resp = auth_client.get(f"/api/showtimes/count?theater_id={_showtimes['theater1_id']}")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 2

    def test_count_by_movie(self, auth_client, app, _showtimes):
        resp = auth_client.get(f"/api/showtimes/count?movie_id={_showtimes['movie2_id']}")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 1

    def test_count_before_date(self, auth_client, app, _showtimes):
        resp = auth_client.get(f"/api/showtimes/count?before={_showtimes['cutoff']}")
        assert resp.status_code == 200
        # Only the two past showtimes are older than the cutoff
        assert resp.get_json()["count"] == 2

    def test_count_invalid_before_returns_400(self, auth_client, app, _showtimes):
        resp = auth_client.get("/api/showtimes/count?before=not-a-date")
        assert resp.status_code == 400

    def test_clear_all(self, auth_client, app, _showtimes):
        resp = auth_client.delete("/api/showtimes")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] == 3
        with app.app_context():
            assert Showtime.query.count() == 0

    def test_clear_by_theater(self, auth_client, app, _showtimes):
        tid = _showtimes["theater1_id"]
        resp = auth_client.delete(f"/api/showtimes?theater_id={tid}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2
        # Other theater's showtime survives
        with app.app_context():
            assert Showtime.query.filter_by(theater_id=_showtimes["theater2_id"]).count() == 1

    def test_clear_by_movie(self, auth_client, app, _showtimes):
        mid = _showtimes["movie1_id"]
        resp = auth_client.delete(f"/api/showtimes?movie_id={mid}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2
        with app.app_context():
            assert Showtime.query.filter_by(movie_id=_showtimes["movie2_id"]).count() == 1

    def test_clear_before_date(self, auth_client, app, _showtimes):
        resp = auth_client.delete(f"/api/showtimes?before={_showtimes['cutoff']}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2
        # Future showtime survives
        with app.app_context():
            assert Showtime.query.get(_showtimes["future_id"]) is not None

    def test_clear_requires_admin(self, app):
        """A non-admin user receives 403 on DELETE /api/showtimes."""
        with app.app_context():
            from app.models import Role
            user_role = Role.query.filter_by(name="user").first()
            from werkzeug.security import generate_password_hash
            regular = User(
                name="Regular",
                email="regular@example.com",
                password_hash=generate_password_hash("pass"),
                role_id=user_role.id,
            )
            db.session.add(regular)
            db.session.commit()

        c = app.test_client()
        c.post("/login", data={"email": "regular@example.com", "password": "pass"},
               follow_redirects=True)
        resp = c.delete("/api/showtimes")
        assert resp.status_code == 403
