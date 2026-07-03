"""Tests for the IMAX Alert application."""
import pytest

from app import create_app, db
from app.models import AlertMovie, AlertPreference, Movie, Notification, Role, Settings, Showtime, Theater, User, UserInvite


@pytest.fixture(scope="session")
def _app_session():
    """Create the Flask app once for the entire test session.

    Avoids repeating the expensive parts of create_app() — Flask extension
    init, SQLAlchemy engine setup, WAL mode config — for every test.
    """
    application = create_app("testing")
    yield application


@pytest.fixture
def app(_app_session):
    """Per-test: push an app context and reset the DB to a clean seeded state.

    drop_all + create_all + three lightweight seeders is far cheaper than
    a full create_app() call (skips 1927-row CSV upsert and 29 migration
    column checks via SKIP_CSV_SEED / SKIP_MIGRATIONS in TestingConfig).
    """
    from app import (  # noqa: PLC0415
        _seed_default_settings,
        _seed_lookup_tables,
        _seed_roles_and_admin,
    )

    with _app_session.app_context():
        db.drop_all()
        db.create_all()
        _seed_roles_and_admin()
        _seed_lookup_tables()
        _seed_default_settings()
        yield _app_session
        db.session.remove()


@pytest.fixture(autouse=True)
def _zero_geocode_delay(monkeypatch):
    """Zero out the Nominatim rate-limit sleep so any test that exercises
    geocode_venue doesn't pay the 1.1-second artificial delay."""
    import app.venue_crawler as vc  # noqa: PLC0415
    monkeypatch.setattr(vc, "GEOCODE_DELAY_SECONDS", 0)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(app):
    """Test client pre-logged-in as the seeded admin (admin/admin).

    The app fixture already pushes an app context, so no inner context
    push is needed here — doing so would call db.session.remove() on exit
    and tear down the session the outer context is using.
    """
    # Ensure seeding ran — admin user should exist
    admin = User.query.filter_by(email="admin").first()
    assert admin is not None, "Admin user not seeded"
    # Disable forced password change so tests aren't redirected to /change-password
    admin.force_password_change = False
    db.session.commit()
    c = app.test_client()
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
        resp = auth_client.get("/settings/account")
        assert resp.status_code == 200

    def test_profile_redirects_to_settings(self, auth_client):
        resp = auth_client.get("/profile")
        assert resp.status_code == 301
        assert "/settings/account" in resp.headers["Location"]

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
    def test_create_user_post(self, auth_client, app):
        resp = auth_client.post(
            "/admin/users/new",
            data={
                "name": "New User",
                "email": "newuser@test.com",
                "password": "ValidPass1!",
                "is_active": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            assert User.query.filter_by(email="newuser@test.com").first() is not None

    def test_create_user_without_password_shows_error(self, auth_client):
        resp = auth_client.post(
            "/admin/users/new",
            data={"name": "No Pass", "email": "nopass@test.com", "is_active": "on"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"password is required" in resp.data.lower()
        # Confirm no user was created
        assert b"nopass@test.com" not in resp.data or b"required" in resp.data.lower()

    def test_create_user_weak_password_preserves_form_data(self, auth_client):
        resp = auth_client.post(
            "/admin/users/new",
            data={
                "name": "Preserved Name",
                "email": "preserved@test.com",
                "password": "weak",
                "is_active": "on",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Password must" in resp.data
        assert b"Preserved Name" in resp.data
        assert b"preserved@test.com" in resp.data

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
            "/settings/account",
            data={"notify_email": "on", "measurement_unit": "imperial"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.measurement_unit == "imperial"
            assert user.notify_email is True

    def test_profile_unit_toggle(self, auth_client, app, sample_user):
        auth_client.post("/settings/account", data={"measurement_unit": "metric"})
        with app.app_context():
            user = User.query.get(sample_user)
            assert user.measurement_unit == "metric"


# ── Scraper ───────────────────────────────────────────────────────────


class TestScraper:
    def test_parse_time_text_valid(self):
        from app.scrapers import _parse_time_text

        dt = _parse_time_text("7:30 PM")
        assert dt is not None
        assert dt.hour == 19
        assert dt.minute == 30

    def test_parse_time_text_invalid(self):
        from app.scrapers import _parse_time_text

        dt = _parse_time_text("not a time")
        assert dt is None

    def test_parse_time_text_24h(self):
        from app.scrapers import _parse_time_text

        dt = _parse_time_text("14:00")
        assert dt is not None
        assert dt.hour == 14

    def test_get_or_create_movie_creates(self, app):
        from app.scrapers import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            movie = scraper.get_or_create_movie("Avatar IMAX")
            db.session.commit()
            assert movie.id is not None
            # "Avatar IMAX" → format suffix stripped → stored as "Avatar"
            assert movie.title == "Avatar"

    def test_get_or_create_movie_deduplicates(self, app):
        from app.scrapers import AMCScraper

        with app.app_context():
            scraper = AMCScraper()
            m1 = scraper.get_or_create_movie("Dune IMAX")
            db.session.commit()
            m2 = scraper.get_or_create_movie("Dune IMAX")
            assert m1.id == m2.id

    def test_upsert_showtime_creates(self, app, sample_theater, sample_movie):
        from datetime import datetime, timezone

        from app.scrapers import AMCScraper

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

        from app.scrapers import AMCScraper

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
        from app.scrapers import AMCScraper

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
        """No active alerts → empty dict."""
        from app.scrapers import _get_active_targets

        with app.app_context():
            targets = _get_active_targets()
            assert targets == {}

    def test_get_active_targets_with_alert(self, app, sample_user, sample_theater, sample_movie):
        """Active unsent alert → theater_id key with the movie_id in its set."""
        from app.models import AlertMovie, AlertPreference
        from app.scrapers import _get_active_targets

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

            targets = _get_active_targets()
            assert sample_theater in targets
            assert sample_movie in targets[sample_theater]

    def test_get_active_targets_sent_alert_excluded(self, app, sample_user, sample_theater, sample_movie):
        """Sent alert (alert_sent=True) is NOT included in targets."""
        from app.models import AlertMovie, AlertPreference
        from app.scrapers import _get_active_targets

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

            targets = _get_active_targets()
            assert sample_theater not in targets

    def test_scrape_all_skips_when_no_alerts(self, app):
        """scrape_all returns [] immediately when there are no active alerts."""
        from unittest.mock import patch

        from app.scrapers import AMCScraper

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
        from app.scrapers.base import BaseScraper

        class _StubScraper(BaseScraper):
            chain_name = "AMC"
            def scrape_theater(self, theater, movie_ids):
                return []

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

            scraper = _StubScraper()
            with patch.object(scraper, "scrape_theater", return_value=[]) as mock_scrape:
                result = scraper.scrape_all()

            assert mock_scrape.call_count == 1
            called_theater = mock_scrape.call_args[0][0]
            assert called_theater.id == sample_theater

    def test_scrape_all_any_theater_alert_scrapes_all(self, app, sample_user, sample_movie):
        """An alert with theater_id=None means 'any theater' — all chain theaters are scraped."""
        from unittest.mock import patch

        from app.models import AlertPreference
        from app.scrapers.base import BaseScraper

        class _StubScraper(BaseScraper):
            chain_name = "AMC"
            def scrape_theater(self, theater, movie_ids):
                return []

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

            scraper = _StubScraper()
            with patch.object(scraper, "scrape_theater", return_value=[]) as mock_scrape:
                scraper.scrape_all()

            # Both AMC theaters should have been scraped
            assert mock_scrape.call_count >= 2


# ── AMC Scraper ───────────────────────────────────────────────────────

_AMC_SAMPLE_HTML = """
<main aria-label="Filtered Showtime Results">
  <section aria-label="Showtimes for Avengers: Secret Wars">
    <ul aria-label="Test Theater, Avengers: Secret Wars Showtimes by Features and Accesibility">
      <li role="listitem" aria-label="IMAX at AMC Showtimes">
        <ul aria-label="Showtime Group Results">
          <li><div role="group"><a href="/showtimes/111">7:00pm</a></div></li>
          <li><div role="group"><a href="/showtimes/222">10:00pm</a></div></li>
        </ul>
      </li>
    </ul>
  </section>
  <section aria-label="Showtimes for Regular Movie">
    <ul aria-label="Test Theater, Regular Movie Showtimes by Features and Accesibility">
      <li role="listitem" aria-label="undefined Showtimes">
        <ul aria-label="Showtime Group Results">
          <li><div role="group"><a href="/showtimes/999">5:00pm</a></div></li>
        </ul>
      </li>
    </ul>
  </section>
</main>
"""


class TestAMCScraper:
    def test_showtimes_url_appends_suffix(self):
        from app.scrapers.amc import _showtimes_url

        assert _showtimes_url("https://www.amctheatres.com/movie-theatres/city/amc-foo-15") == \
            "https://www.amctheatres.com/movie-theatres/city/amc-foo-15/showtimes"

    def test_showtimes_url_idempotent(self):
        from app.scrapers.amc import _showtimes_url

        url = "https://www.amctheatres.com/movie-theatres/city/amc-foo-15/showtimes"
        assert _showtimes_url(url) == url

    def test_parse_page_extracts_imax_showtimes(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.amc import AMCScraper, _parse_page

        soup = BeautifulSoup(_AMC_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = AMCScraper()
            results = _parse_page(theater, {None}, soup, scraper)

        assert len(results) == 2
        urls = {st.tickets_url for st in results}
        assert "https://www.amctheatres.com/showtimes/111" in urls
        assert "https://www.amctheatres.com/showtimes/222" in urls
        assert all(st.format_type == "IMAX" for st in results)

    def test_parse_page_skips_non_imax(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.amc import AMCScraper, _parse_page

        soup = BeautifulSoup(_AMC_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = AMCScraper()
            results = _parse_page(theater, {None}, soup, scraper)
            movie_titles = {st.movie.title for st in results}

        assert "Regular Movie" not in movie_titles
        assert "Avengers: Secret Wars" in movie_titles

    def test_parse_page_deduplicates_on_reparse(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.amc import AMCScraper, _parse_page

        soup = BeautifulSoup(_AMC_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = AMCScraper()
            first = _parse_page(theater, {None}, soup, scraper)
            db.session.commit()
            second = _parse_page(theater, {None}, soup, scraper)

        assert len(first) == 2
        assert len(second) == 0  # already inserted — upsert returns is_new=False


# ── Regal Scraper ─────────────────────────────────────────────────────

_REGAL_SAMPLE_SHOWS = [
    {
        "TheatreCode": "1010",
        "AdvertiseShowDate": "2026-06-16T00:00:00",
        "UtcDate": "2026-06-16T07:00:00.000Z",
        "Film": [
            {
                "Title": "Avengers: Secret Wars",
                "MasterMovieCode": "HO00099999",
                "Performances": [
                    {
                        "PerformanceId": 111111,
                        "PerformanceAttributes": ["CC", "DV", "IMAX", "Laser", "2D"],
                        "PerformanceGroup": "IMAX",
                        "CalendarShowTime": "2026-06-16T19:00:00",
                        "UtcShowTime": "2026-06-17T02:00:00.000Z",
                        "UnixTime": 1781578800000,
                        "StopSales": False,
                    },
                    {
                        "PerformanceId": 222222,
                        "PerformanceAttributes": ["CC", "DV", "IMAX", "Laser", "2D"],
                        "PerformanceGroup": "IMAX",
                        "CalendarShowTime": "2026-06-16T22:25:00",
                        "UtcShowTime": "2026-06-17T05:25:00.000Z",
                        "UnixTime": 1781590700000,
                        "StopSales": False,
                    },
                ],
            },
            {
                "Title": "Regular Movie",
                "MasterMovieCode": "HO00088888",
                "Performances": [
                    {
                        "PerformanceId": 999999,
                        "PerformanceAttributes": ["CC", "DV", "2D", "Stadium"],
                        "PerformanceGroup": "",
                        "CalendarShowTime": "2026-06-16T18:00:00",
                        "UtcShowTime": "2026-06-17T01:00:00.000Z",
                        "UnixTime": 1781575200000,
                        "StopSales": False,
                    },
                ],
            },
        ],
        "time": 1781570000000,
    }
]


class TestRegalScraper:
    def test_theatre_code_from_url(self):
        from app.scrapers.regal import _theatre_code_from_url

        assert _theatre_code_from_url("https://www.regmovies.com/theatres/regal-irvine-spectrum-1010") == "1010"
        assert _theatre_code_from_url("https://www.regmovies.com/theatres/regal-goldstream-0601") == "0601"

    def test_parse_utc_showtime(self):
        from app.scrapers.regal import _parse_utc_showtime
        from datetime import timezone

        dt = _parse_utc_showtime("2026-06-17T02:00:00.000Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026
        assert dt.month == 6
        assert dt.day == 17
        assert dt.hour == 2

    def test_parse_utc_showtime_invalid(self):
        from app.scrapers.regal import _parse_utc_showtime

        assert _parse_utc_showtime("") is None
        assert _parse_utc_showtime("not-a-date") is None

    def test_parse_shows_extracts_imax(self, app, sample_theater):
        from app.scrapers.regal import RegalScraper, _parse_shows

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = RegalScraper()
            results = _parse_shows(scraper, theater, {None}, _REGAL_SAMPLE_SHOWS, "1010")

        assert len(results) == 2
        assert all(st.format_type == "IMAX" for st in results)
        urls = {st.tickets_url for st in results}
        assert any("111111" in u for u in urls)
        assert any("222222" in u for u in urls)

    def test_parse_shows_skips_non_imax(self, app, sample_theater):
        from app.scrapers.regal import RegalScraper, _parse_shows

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = RegalScraper()
            results = _parse_shows(scraper, theater, {None}, _REGAL_SAMPLE_SHOWS, "1010")
            titles = {st.movie.title for st in results}

        assert "Regular Movie" not in titles
        assert "Avengers: Secret Wars" in titles

    def test_parse_shows_deduplicates(self, app, sample_theater):
        from app.scrapers.regal import RegalScraper, _parse_shows

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = RegalScraper()
            first = _parse_shows(scraper, theater, {None}, _REGAL_SAMPLE_SHOWS, "1010")
            db.session.commit()
            second = _parse_shows(scraper, theater, {None}, _REGAL_SAMPLE_SHOWS, "1010")

        assert len(first) == 2
        assert len(second) == 0


# ── Cinemark Scraper ──────────────────────────────────────────────────

_CINEMARK_SAMPLE_HTML = """
<div id="showTimes">
  <div id="showtimesInner">
    <div class="showtimeMovieBlock 109523">
      <div class="showtimeMovie clearfix">
        <h3>Avengers: Secret Wars</h3>
        <div class="movieBlockShowtimes col-xs-12 col-sm-10">
          <div class="showtimeMovieTimes clearfix">
            <div class="row" role="list">
              <div class="showtime" data-print-type-name="IMAX 2D" role="listitem">
                <p aria-disabled="true" class="off past">12:20pm<span></span></p>
              </div>
              <div class="showtime" data-print-type-name="IMAX 2D" role="listitem">
                <a aria-label="Select 7:10 PM showtime for Monday, June 15, 2026"
                   class="showtime-link"
                   data-print-type-name="IMAX 2D"
                   href="/TicketSeatMap/?TheaterId=444&amp;ShowtimeId=706168&amp;CinemarkMovieId=109523&amp;Showtime=2026-06-15T19:10:00">7:10pm</a>
              </div>
              <div class="showtime" data-print-type-name="IMAX 2D" role="listitem">
                <a aria-label="Select 10:35 PM showtime for Monday, June 15, 2026"
                   class="showtime-link"
                   data-print-type-name="IMAX 2D"
                   href="/TicketSeatMap/?TheaterId=444&amp;ShowtimeId=706169&amp;CinemarkMovieId=109523&amp;Showtime=2026-06-15T22:35:00">10:35pm</a>
              </div>
            </div>
          </div>
          <div class="showtimeMovieTimes clearfix">
            <div class="row" role="list">
              <div class="showtime" data-print-type-name="Standard Format Luxury Lounger" role="listitem">
                <a class="showtime-link" href="/TicketSeatMap/?TheaterId=444&amp;ShowtimeId=999999">5:00pm</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="showtimeMovieBlock 107529">
      <div class="showtimeMovie clearfix">
        <h3>Regular Movie</h3>
        <div class="movieBlockShowtimes col-xs-12 col-sm-10">
          <div class="showtimeMovieTimes clearfix">
            <div class="row" role="list">
              <div class="showtime" data-print-type-name="Standard Format Luxury Lounger" role="listitem">
                <a class="showtime-link" href="/TicketSeatMap/?TheaterId=444&amp;ShowtimeId=888888">8:00pm</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
"""

_CINEMARK_MAIN_HTML = """
<html><body>
<script>
  var currentTheaterId = 444;
  var currentShowdate = "2026-06-15 00:00:00";
</script>
<ul class="carousel">
  <li class="carousel__item carousel__item--showdate item-selected">
    <a class="showdate-link" data-datevalue="2026-06-15" href="javascript:void(0)">Today</a>
  </li>
  <li class="carousel__item carousel__item--showdate">
    <a class="showdate-link" data-datevalue="2026-06-16" href="javascript:void(0)">Tues 6/16</a>
  </li>
  <li class="carousel__item carousel__item--showdate">
    <a class="showdate-link" data-datevalue="2026-06-17" href="javascript:void(0)">Wed 6/17</a>
  </li>
</ul>
""" + _CINEMARK_SAMPLE_HTML + "</body></html>"


class TestCinemarkScraper:
    def test_extract_theater_id(self):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import _extract_theater_id

        soup = BeautifulSoup(_CINEMARK_MAIN_HTML, "lxml")
        assert _extract_theater_id(soup) == "444"

    def test_extract_theater_id_missing(self):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import _extract_theater_id

        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        assert _extract_theater_id(soup) == ""

    def test_parse_showtime_dt(self):
        from app.scrapers.cinemark import _parse_showtime_dt

        # No theater → returns naive local datetime; caller supplies tz conversion
        dt = _parse_showtime_dt("2026-06-15", "7:10pm")
        assert dt is not None
        assert dt.tzinfo is None
        assert dt.hour == 19
        assert dt.minute == 10

        dt2 = _parse_showtime_dt("2026-06-15", "10:35pm")
        assert dt2 is not None
        assert dt2.hour == 22
        assert dt2.minute == 35

    def test_parse_showtime_dt_invalid(self):
        from app.scrapers.cinemark import _parse_showtime_dt

        assert _parse_showtime_dt("2026-06-15", "") is None
        assert _parse_showtime_dt("bad-date", "7:10pm") is None

    def test_parse_imax_showtimes_extracts_imax(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import CinemarkScraper, _parse_imax_showtimes

        soup = BeautifulSoup(_CINEMARK_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = CinemarkScraper()
            results = _parse_imax_showtimes(scraper, theater, {None}, soup, "2026-06-15")

        assert len(results) == 2
        # "IMAX 2D" is normalised to "IMAX" by upsert_showtime (only 3D keeps its suffix)
        assert all(st.format_type == "IMAX" for st in results)
        urls = {st.tickets_url for st in results}
        assert any("706168" in u for u in urls)
        assert any("706169" in u for u in urls)

    def test_parse_imax_showtimes_skips_past(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import CinemarkScraper, _parse_imax_showtimes

        soup = BeautifulSoup(_CINEMARK_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = CinemarkScraper()
            results = _parse_imax_showtimes(scraper, theater, {None}, soup, "2026-06-15")

        # 12:20pm is a past showtime (no link) — only 7:10pm and 10:35pm should appear
        times = {st.show_datetime.hour for st in results}
        assert 12 not in times
        assert 19 in times
        assert 22 in times

    def test_parse_imax_showtimes_skips_non_imax(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import CinemarkScraper, _parse_imax_showtimes

        soup = BeautifulSoup(_CINEMARK_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = CinemarkScraper()
            results = _parse_imax_showtimes(scraper, theater, {None}, soup, "2026-06-15")
            titles = {st.movie.title for st in results}

        assert "Regular Movie" not in titles
        assert "Avengers: Secret Wars" in titles

    def test_parse_imax_showtimes_deduplicates(self, app, sample_theater):
        from bs4 import BeautifulSoup
        from app.scrapers.cinemark import CinemarkScraper, _parse_imax_showtimes

        soup = BeautifulSoup(_CINEMARK_SAMPLE_HTML, "lxml")
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = CinemarkScraper()
            first = _parse_imax_showtimes(scraper, theater, {None}, soup, "2026-06-15")
            db.session.commit()
            second = _parse_imax_showtimes(scraper, theater, {None}, soup, "2026-06-15")

        assert len(first) == 2
        assert len(second) == 0


_TCL_NEXT_DATA_HTML = """
<html><head>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"environment":{"gasToken":"test-gas-token-abc123","cmsConfig":{"salesChannel":"Web","apiUrl":"https://cms-api-www.tclchinesetheatres.com"},"cloudEnvironment":"live"}}},"page":"/","buildId":"testBuild"}
</script>
</head><body></body></html>
"""

_TCL_SCREENING_DATES = [
    {
        "businessDate": "2026-06-16",
        "filmScreenings": [
            {"filmId": "HO00001455", "sites": [{"siteId": "0001", "showtimeAttributeIds": ["0000000001", "0000000009"]}]},
            {"filmId": "HO00001442", "sites": [{"siteId": "0001", "showtimeAttributeIds": ["0000000001", "0000000004"]}]},
        ],
    },
    {
        "businessDate": "2026-06-17",
        "filmScreenings": [
            {"filmId": "HO00001442", "sites": [{"siteId": "0001", "showtimeAttributeIds": ["0000000001", "0000000004"]}]},
        ],
    },
]

_TCL_SHOWTIMES_RESPONSE = {
    "businessDate": "2026-06-16",
    "showtimes": [
        {
            "id": "0001-75735",
            "schedule": {"businessDate": "2026-06-16", "startsAt": "2026-06-16T18:30:00-07:00"},
            "filmId": "HO00001455",
            "siteId": "0001",
            "attributeIds": ["0000000001", "0000000009"],
            "isSoldOut": False,
        },
        {
            "id": "0001-75736",
            "schedule": {"businessDate": "2026-06-16", "startsAt": "2026-06-16T21:55:00-07:00"},
            "filmId": "HO00001455",
            "siteId": "0001",
            "attributeIds": ["0000000001", "0000000009"],
            "isSoldOut": True,
        },
        {
            "id": "0001-75700",
            "schedule": {"businessDate": "2026-06-16", "startsAt": "2026-06-16T14:00:00-07:00"},
            "filmId": "HO00001442",
            "siteId": "0001",
            "attributeIds": ["0000000001", "0000000004"],
            "isSoldOut": False,
        },
    ],
    "relatedData": {
        "films": [
            {"id": "HO00001455", "title": {"text": "(IMAX) Disclosure Day", "translations": []}},
            {"id": "HO00001442", "title": {"text": "Non-IMAX Film", "translations": []}},
        ],
        "attributes": [
            {"id": "0000000001", "name": {"text": "2D"}},
            {"id": "0000000004", "name": {"text": "Dolby Atmos"}},
            {"id": "0000000009", "name": {"text": "IMAX"}},
        ],
    },
}


class TestTCLScraper:
    def test_fetch_gas_token_extracts_token(self):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import _fetch_gas_token

        mock_page = MagicMock()
        mock_page.content.return_value = _TCL_NEXT_DATA_HTML
        mock_ctx = MagicMock()
        mock_ctx.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_ctx
        mock_pw = MagicMock()
        mock_pw.__enter__ = MagicMock(return_value=mock_pw)
        mock_pw.__exit__ = MagicMock(return_value=False)
        mock_pw.chromium.launch.return_value = mock_browser
        with patch("playwright.sync_api.sync_playwright", return_value=mock_pw):
            token = _fetch_gas_token()
        assert token == "test-gas-token-abc123"

    def test_fetch_gas_token_missing_next_data(self):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import _fetch_gas_token

        mock_page = MagicMock()
        mock_page.content.return_value = "<html><body>no next data here</body></html>"
        mock_ctx = MagicMock()
        mock_ctx.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_ctx
        mock_pw = MagicMock()
        mock_pw.__enter__ = MagicMock(return_value=mock_pw)
        mock_pw.__exit__ = MagicMock(return_value=False)
        mock_pw.chromium.launch.return_value = mock_browser
        with patch("playwright.sync_api.sync_playwright", return_value=mock_pw):
            token = _fetch_gas_token()
        assert token == ""

    def test_imax_dates_filters_correctly(self):
        from app.scrapers.tcl import _imax_dates

        dates = _imax_dates(_TCL_SCREENING_DATES)
        assert dates == ["2026-06-16"]

    def test_imax_dates_no_imax(self):
        from app.scrapers.tcl import _imax_dates

        dates = _imax_dates(_TCL_SCREENING_DATES[1:])  # only the non-IMAX date
        assert dates == []

    def test_title_prefix_stripped(self):
        import re
        from app.scrapers.tcl import _TITLE_PREFIX_RE

        assert _TITLE_PREFIX_RE.sub("", "(IMAX) Disclosure Day") == "Disclosure Day"
        assert _TITLE_PREFIX_RE.sub("", "(DBOX) The Odyssey") == "The Odyssey"
        assert _TITLE_PREFIX_RE.sub("", "Regular Movie") == "Regular Movie"

    def test_scrape_date_extracts_imax(self, app, sample_theater):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import TCLScraper
        import json

        mock_resp = MagicMock()
        mock_resp.json.return_value = _TCL_SHOWTIMES_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = TCLScraper()
            with patch("app.scrapers.tcl.requests.get", return_value=mock_resp):
                results = scraper._scrape_date(theater, {None}, {}, "2026-06-16", False)

        assert len(results) == 2
        assert all(st.format_type == "IMAX" for st in results)
        titles = {st.movie.title for st in results}
        # Prefix is stripped: "(IMAX) Disclosure Day" → "Disclosure Day"
        assert "Disclosure Day" in titles
        assert "(IMAX) Disclosure Day" not in titles

    def test_scrape_date_skips_non_imax(self, app, sample_theater):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import TCLScraper

        mock_resp = MagicMock()
        mock_resp.json.return_value = _TCL_SHOWTIMES_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = TCLScraper()
            with patch("app.scrapers.tcl.requests.get", return_value=mock_resp):
                results = scraper._scrape_date(theater, {None}, {}, "2026-06-16", False)
            titles = {st.movie.title for st in results}

        assert "Non-IMAX Film" not in titles

    def test_scrape_date_tickets_available_flag(self, app, sample_theater):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import TCLScraper

        mock_resp = MagicMock()
        mock_resp.json.return_value = _TCL_SHOWTIMES_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = TCLScraper()
            with patch("app.scrapers.tcl.requests.get", return_value=mock_resp):
                results = scraper._scrape_date(theater, {None}, {}, "2026-06-16", False)

        available = {st.tickets_available for st in results}
        assert True in available   # 18:30 showtime is not sold out
        assert False in available  # 21:55 showtime is sold out

    def test_scrape_date_deduplicates(self, app, sample_theater):
        from unittest.mock import patch, MagicMock
        from app.scrapers.tcl import TCLScraper

        mock_resp = MagicMock()
        mock_resp.json.return_value = _TCL_SHOWTIMES_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        with app.app_context():
            theater = Theater.query.get(sample_theater)
            scraper = TCLScraper()
            with patch("app.scrapers.tcl.requests.get", return_value=mock_resp):
                first = scraper._scrape_date(theater, {None}, {}, "2026-06-16", False)
                db.session.commit()
                second = scraper._scrape_date(theater, {None}, {}, "2026-06-16", False)

        assert len(first) == 2
        assert len(second) == 0


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
        from unittest.mock import patch

        from app.notifications import _notify_for_showtime

        with app.app_context():
            # Give the user an email channel and mock delivery so the alert
            # auto-closes after a successful notification.
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

            with patch("app.notifications.send_email", return_value=(True, "")):
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


# ── Scraper coordinator ───────────────────────────────────────────────


class TestScraperCoordinator:
    """Unit tests for the three safeguards in queue_theaters_for_scrape."""

    def _amc_scraper(self):
        import app.scrapers as coord_mod
        return next(s for s in coord_mod.ALL_SCRAPERS if s.chain_name == "AMC")

    def test_inflight_theater_is_skipped(self, app, sample_theater):
        from unittest.mock import patch
        import app.scrapers as coord_mod

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            amc = self._amc_scraper()

            with coord_mod._inflight_lock:
                coord_mod._scraping_in_flight.add(theater.id)
            try:
                with patch.object(amc, "scrape_theaters_batch", return_value=([], set())) as mock_batch:
                    result = coord_mod.queue_theaters_for_scrape({theater.id})
                assert result == []
                mock_batch.assert_not_called()
            finally:
                with coord_mod._inflight_lock:
                    coord_mod._scraping_in_flight.discard(theater.id)

    def test_cooldown_skips_unless_force(self, app, sample_theater):
        from unittest.mock import patch
        from datetime import datetime
        import app.scrapers as coord_mod

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            theater.last_scraped_at = datetime.utcnow()
            db.session.commit()
            amc = self._amc_scraper()

            # Within cooldown window → skipped
            with patch.object(amc, "scrape_theaters_batch", return_value=([], set())) as mock_batch:
                coord_mod.queue_theaters_for_scrape({theater.id}, force=False)
            mock_batch.assert_not_called()

            # force=True bypasses cooldown → scrape is attempted
            with patch.object(amc, "scrape_theaters_batch", return_value=([], set())) as mock_batch:
                coord_mod.queue_theaters_for_scrape({theater.id}, force=True)
            mock_batch.assert_called_once()

    def test_semaphore_timeout_clears_inflight_and_skips_last_scraped(self, app, sample_theater):
        from unittest.mock import patch, MagicMock
        import threading
        import app.scrapers as coord_mod

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            theater.last_scraped_at = None
            db.session.commit()

            mock_sem = MagicMock(spec=threading.Semaphore)
            mock_sem.acquire.return_value = False

            with patch("app.scrapers._get_semaphore", return_value=mock_sem):
                coord_mod.queue_theaters_for_scrape({theater.id})

            db.session.refresh(theater)
            assert theater.last_scraped_at is None
            with coord_mod._inflight_lock:
                assert theater.id not in coord_mod._scraping_in_flight

    def test_failing_theater_in_batch_does_not_advance_last_scraped(self, app):
        """
        One theater raising inside scrape_theater() must not block last_scraped_at
        from advancing for the other theater in the same chain batch.
        """
        from unittest.mock import patch
        import app.scrapers as coord_mod
        from app.models import Theater as TheaterModel

        with app.app_context():
            ok_theater = TheaterModel(
                name="Cinemark IMAX Success", chain="Cinemark",
                city="Springfield", state="IL", is_active=True,
            )
            bad_theater = TheaterModel(
                name="Cinemark IMAX Failure", chain="Cinemark",
                city="Othertown", state="NY", is_active=True,
            )
            db.session.add_all([ok_theater, bad_theater])
            db.session.commit()

            cinemark = next(s for s in coord_mod.ALL_SCRAPERS if s.chain_name == "Cinemark")

            def _scrape_theater(theater, movie_ids):
                if theater.id == bad_theater.id:
                    raise RuntimeError("scrape failed")
                return []

            with patch.object(cinemark, "scrape_theater", side_effect=_scrape_theater):
                coord_mod.queue_theaters_for_scrape(
                    {ok_theater.id, bad_theater.id}, force=True
                )

            db.session.refresh(ok_theater)
            db.session.refresh(bad_theater)
            assert ok_theater.last_scraped_at is not None
            assert bad_theater.last_scraped_at is None


# ── BrowseSchedule.compute_next_run ───────────────────────────────────


class TestComputeNextRun:
    """Unit tests for BrowseSchedule.compute_next_run() timezone/scheduling logic."""

    def _schedule(self, freq, hour=8, dow=None):
        from app.models import BrowseSchedule
        return BrowseSchedule(
            user_id=0,
            radius=50.0,
            radius_unit="km",
            frequency_minutes=freq,
            preferred_hour=hour,
            preferred_day_of_week=dow,
        )

    def test_subdaily_ignores_preferred_hour(self, app):
        """Sub-daily frequencies just add the interval; preferred_hour is irrelevant."""
        from datetime import datetime
        with app.app_context():
            s = self._schedule(freq=60, hour=8)
            base = datetime(2026, 6, 26, 10, 0, 0)  # 10:00 UTC
            result = s.compute_next_run(base, "America/New_York")
            assert result == datetime(2026, 6, 26, 11, 0, 0)

    def test_daily_future_hour_same_day(self, app):
        """Daily: preferred hour still ahead today → schedules later today."""
        from datetime import datetime
        with app.app_context():
            # 08:00 UTC = 04:00 ET (EDT, UTC-4); preferred_hour=10 ET is 14:00 UTC today
            s = self._schedule(freq=1440, hour=10)
            base = datetime(2026, 6, 26, 8, 0, 0)  # 04:00 ET
            result = s.compute_next_run(base, "America/New_York")
            assert result == datetime(2026, 6, 26, 14, 0, 0)

    def test_daily_past_hour_schedules_tomorrow(self, app):
        """Daily: preferred hour already passed today → schedules for tomorrow."""
        from datetime import datetime
        with app.app_context():
            # 20:00 UTC = 16:00 ET (EDT); preferred_hour=8 ET already passed → next day
            s = self._schedule(freq=1440, hour=8)
            base = datetime(2026, 6, 26, 20, 0, 0)  # 16:00 ET
            result = s.compute_next_run(base, "America/New_York")
            assert result == datetime(2026, 6, 27, 12, 0, 0)  # 08:00 ET next day = 12:00 UTC

    def test_weekly_correct_day(self, app):
        """Weekly: schedules the next occurrence of the preferred day."""
        from datetime import datetime
        with app.app_context():
            # 2026-06-26 is a Friday (weekday=4). preferred_dow=0 (Monday) → next Monday.
            # Monday 2026-06-29 at 08:00 ET = 12:00 UTC (EDT, UTC-4)
            s = self._schedule(freq=10080, hour=8, dow=0)
            base = datetime(2026, 6, 26, 14, 0, 0)  # Friday 10:00 ET
            result = s.compute_next_run(base, "America/New_York")
            assert result == datetime(2026, 6, 29, 12, 0, 0)

    def test_weekly_same_day_past_hour_advances_one_week(self, app):
        """Weekly: preferred day is today but hour has passed → schedules 7 days out."""
        from datetime import datetime
        with app.app_context():
            # 2026-06-26 is Friday (weekday=4). preferred_dow=4 (Friday) at 08:00 ET.
            # Current local time is 10:00 ET → hour already passed → next Friday.
            s = self._schedule(freq=10080, hour=8, dow=4)
            base = datetime(2026, 6, 26, 14, 0, 0)  # Friday 10:00 ET (UTC-4)
            result = s.compute_next_run(base, "America/New_York")
            assert result == datetime(2026, 7, 3, 12, 0, 0)  # next Friday 08:00 ET

    def test_invalid_timezone_falls_back_to_utc(self, app):
        """An unrecognised timezone string silently falls back to UTC."""
        from datetime import datetime
        with app.app_context():
            s = self._schedule(freq=1440, hour=9)
            base = datetime(2026, 6, 26, 20, 0, 0)  # 20:00 UTC; 09:00 already past in UTC
            result = s.compute_next_run(base, "Not/ATimezone")
            # Falls back to UTC: 09:00 UTC passed → tomorrow 09:00 UTC
            assert result == datetime(2026, 6, 27, 9, 0, 0)

    def test_null_preferred_hour_defaults_to_8(self, app):
        """NULL preferred_hour falls back to 8, matching compute_next_run's documented default."""
        from datetime import datetime
        with app.app_context():
            s = self._schedule(freq=1440, hour=None)
            base = datetime(2026, 6, 26, 6, 0, 0)  # 06:00 UTC; 08:00 UTC still ahead
            result = s.compute_next_run(base, "UTC")
            assert result == datetime(2026, 6, 26, 8, 0, 0)


# ── Browse schedule ───────────────────────────────────────────────────


class TestBrowseSchedule:
    """Unit tests for run_browse_schedules() job function."""

    def _make_schedule(self, user_id, enabled=True, next_run_offset_min=-1,
                       radius=50.0, radius_unit="km", frequency_minutes=60):
        """Create a BrowseSchedule row with next_run in the past by default."""
        from datetime import datetime, timedelta
        from app.models import BrowseSchedule
        now = datetime.utcnow()
        schedule = BrowseSchedule(
            user_id=user_id,
            radius=radius,
            radius_unit=radius_unit,
            frequency_minutes=frequency_minutes,
            enabled=enabled,
            next_run=now + timedelta(minutes=next_run_offset_min),
        )
        db.session.add(schedule)
        db.session.commit()
        return schedule

    def test_no_due_schedules_returns_empty(self, app, sample_user):
        from app.scrapers import run_browse_schedules
        with app.app_context():
            # next_run is 60 minutes in the future — not due yet
            self._make_schedule(sample_user, next_run_offset_min=60)
            result = run_browse_schedules()
            assert result == []

    def test_skipped_schedule_no_location_does_not_advance_next_run(self, app, sample_user):
        from datetime import datetime
        from app.scrapers import run_browse_schedules
        from app.models import BrowseSchedule, User

        with app.app_context():
            user = User.query.get(sample_user)
            user.location_lat = None
            user.location_lon = None
            db.session.commit()

            schedule = self._make_schedule(sample_user, next_run_offset_min=-5)
            original_next_run = schedule.next_run

            run_browse_schedules()

            db.session.refresh(schedule)
            # next_run must not have advanced — user had no location
            assert schedule.next_run == original_next_run
            assert schedule.last_run is None

    def test_processed_schedule_advances_next_run(self, app, sample_user, sample_theater):
        from unittest.mock import patch
        from datetime import datetime
        from app.scrapers import run_browse_schedules
        from app.models import BrowseSchedule, User, Theater

        with app.app_context():
            user = User.query.get(sample_user)
            user.location_lat = 34.05
            user.location_lon = -118.24
            db.session.commit()

            # Theater within 1 km of user (same coords)
            theater = Theater.query.get(sample_theater)
            theater.latitude = 34.05
            theater.longitude = -118.24
            db.session.commit()

            schedule = self._make_schedule(sample_user, next_run_offset_min=-5)
            original_next_run = schedule.next_run

            with patch("app.scrapers.queue_theaters_for_scrape", return_value=[]):
                run_browse_schedules()

            db.session.refresh(schedule)
            assert schedule.last_run is not None
            assert schedule.next_run > original_next_run

    def test_theater_union_across_users(self, app, sample_user, sample_theater):
        """Overlapping radii from two users produce one combined theater set."""
        from unittest.mock import patch, call
        from app.scrapers import run_browse_schedules
        from app.models import User, Theater, Role

        with app.app_context():
            # Reuse existing user and theater
            user1 = User.query.get(sample_user)
            user1.location_lat = 34.05
            user1.location_lon = -118.24
            db.session.commit()

            theater = Theater.query.get(sample_theater)
            theater.latitude = 34.05
            theater.longitude = -118.24
            db.session.commit()

            # Create a second user with a slightly different location
            role = Role.query.filter_by(name="user").first()
            user2 = User(name="User Two", email="user2@test.com",
                         location_lat=34.06, location_lon=-118.24,
                         role_id=role.id if role else None)
            user2.set_password("password")
            db.session.add(user2)
            db.session.commit()

            self._make_schedule(user1.id, next_run_offset_min=-5)
            self._make_schedule(user2.id, next_run_offset_min=-5)

            captured = []
            def fake_queue(theater_ids, **kwargs):
                captured.append(set(theater_ids))
                return []

            with patch("app.scrapers.queue_theaters_for_scrape", side_effect=fake_queue):
                run_browse_schedules()

            # queue_theaters_for_scrape called exactly once with the union of both sets
            assert len(captured) == 1
            assert theater.id in captured[0]


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


@pytest.fixture
def csv_seeded_app(app):
    """Like app but also runs the CSV theater upsert.

    TestingConfig sets SKIP_CSV_SEED=True so the per-test app fixture skips
    the 1927-row upsert for speed.  Tests in TestCSVSeeding explicitly test
    that function, so they use this fixture to run it on demand.
    """
    from app import _upsert_theaters_from_csv  # noqa: PLC0415
    _upsert_theaters_from_csv(app)
    return app


class TestCSVSeeding:
    """Tests for _upsert_theaters_from_csv and related helpers."""

    def test_csv_seed_populates_theaters(self, csv_seeded_app):
        """CSV upsert should insert theaters from the seed file."""
        count = Theater.query.count()
        assert count > 0, "CSV seed did not insert any theaters"

    def test_csv_seed_sets_continent(self, csv_seeded_app):
        """At least one theater should have a continent FK set."""
        from app.models import Continent  # noqa: PLC0415
        assert Continent.query.count() > 0, "No continents seeded"
        t = Theater.query.filter(Theater.continent_id.isnot(None)).first()
        assert t is not None, "No theater has continent_id set"

    def test_csv_seed_sets_aspect_ratio(self, csv_seeded_app):
        """At least one theater should have aspect_ratio_id set."""
        t = Theater.query.filter(Theater.aspect_ratio_id.isnot(None)).first()
        assert t is not None, "No theater has aspect_ratio_id set"

    def test_csv_seed_sets_projector_type(self, csv_seeded_app):
        """At least one theater should have projector_type_id set."""
        t = Theater.query.filter(Theater.projector_type_id.isnot(None)).first()
        assert t is not None

    def test_csv_seed_commercial_films_values(self, csv_seeded_app):
        """commercial_films column should only contain valid values or NULL."""
        valid = {"Yes", "Limited", "No", None}
        for t in Theater.query.all():
            assert t.commercial_films in valid, f"Unexpected: {t.commercial_films!r}"

    def test_csv_seed_is_idempotent(self, csv_seeded_app):
        """Calling _upsert_theaters_from_csv a second time should not change count."""
        from app import _upsert_theaters_from_csv  # noqa: PLC0415
        count_before = Theater.query.count()
        _upsert_theaters_from_csv(csv_seeded_app)
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

    def test_alert_not_closed_when_delivery_fails(
        self, app, sample_user, sample_theater, sample_movie
    ):
        """
        When notify_email=True but SMTP is unconfigured the send fails —
        alert_sent must remain False so the alert retries on the next cycle.
        notifications_fired must not be incremented on a failed delivery.
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
            # Delivery failed — alert must stay open to retry, counter unchanged.
            assert pref.alert_sent is False
            assert (pref.notifications_fired or 0) == 0


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


# -- Scraper health check ----------------------------------------------


class TestHealthCheck:
    """Regression tests for #270/#271/#272 — probe data must never persist,
    counts must reflect parsed (not new) showtimes, and probes must respect
    the in-flight registry."""

    def _stub_scraper(self, showtimes_to_upsert):
        """Build a BaseScraper stub whose scrape_theater upserts the given
        (title, dt) pairs and then commits internally — like Regal does."""
        from app.scrapers.base import BaseScraper

        class StubScraper(BaseScraper):
            chain_name = "AMC"  # matches sample_theater fixture chain

            def scrape_theater(self, theater, movie_ids):
                new = []
                for title, dt in showtimes_to_upsert:
                    movie = self.get_or_create_movie(title)
                    st, is_new = self.upsert_showtime(theater, movie, dt)
                    if is_new:
                        new.append(st)
                db.session.commit()  # internal commit — the Regal leak path
                return new

        return StubScraper()

    def test_probe_persists_nothing_despite_internal_commit(self, app, sample_theater):
        from datetime import datetime, timedelta
        from app.scrapers.health import run_health_check

        future = datetime.utcnow() + timedelta(days=3)
        scraper = self._stub_scraper([("Probe Only Movie", future)])

        result = run_health_check(scraper)

        assert result["status"] == "ok"
        assert result["showtime_count"] == 1
        # The probe's movie and showtime must NOT be in the DB.
        assert Movie.query.filter(Movie.title.ilike("Probe Only Movie")).first() is None
        assert Showtime.query.count() == 0

    def test_probe_counts_already_existing_showtimes(self, app, sample_theater, sample_movie):
        """#271: a theater whose showtimes already exist must report ok, not
        a false 'no showtimes found' warning."""
        from datetime import datetime, timedelta
        from app.scrapers.health import run_health_check

        future = datetime.utcnow() + timedelta(days=3)
        movie = Movie.query.get(sample_movie)
        theater = Theater.query.get(sample_theater)
        db.session.add(Showtime(theater_id=theater.id, movie_id=movie.id,
                                show_datetime=future))
        db.session.commit()

        scraper = self._stub_scraper([(movie.title, future)])
        result = run_health_check(scraper)

        assert result["status"] == "ok"
        assert result["showtime_count"] == 1
        assert Showtime.query.count() == 1  # unchanged

    def test_probe_does_not_mutate_existing_rows(self, app, sample_theater, sample_movie):
        """#270: the probe must not clear browse_only/on_demand provenance flags."""
        from datetime import datetime, timedelta
        from app.scrapers.health import run_health_check

        future = datetime.utcnow() + timedelta(days=3)
        st = Showtime(theater_id=sample_theater, movie_id=sample_movie,
                      show_datetime=future, browse_only=True)
        db.session.add(st)
        db.session.commit()
        movie = Movie.query.get(sample_movie)

        scraper = self._stub_scraper([(movie.title, future)])
        run_health_check(scraper)

        db.session.expire_all()
        st = Showtime.query.first()
        assert st.browse_only is True

    def test_probe_skips_inflight_theater(self, app, sample_theater):
        """#272: probe must not scrape a theater another job has claimed."""
        from datetime import datetime, timedelta
        import app.scrapers as coord_mod
        from app.scrapers.health import run_health_check

        future = datetime.utcnow() + timedelta(days=3)
        scraper = self._stub_scraper([("Probe Only Movie", future)])

        with coord_mod._inflight_lock:
            coord_mod._scraping_in_flight.add(sample_theater)
        try:
            result = run_health_check(scraper)
        finally:
            with coord_mod._inflight_lock:
                coord_mod._scraping_in_flight.discard(sample_theater)

        assert result["status"] == "warning"
        assert result["error_class"] == "Busy"
        assert Showtime.query.count() == 0


class TestUpsertTicketsUrl:
    def test_update_refreshes_tickets_url(self, app, sample_theater, sample_movie):
        """#254: a later scrape providing a URL must update the stored row."""
        from datetime import datetime, timedelta
        from app.scrapers.base import BaseScraper

        future = datetime.utcnow() + timedelta(days=3)
        theater = Theater.query.get(sample_theater)
        movie = Movie.query.get(sample_movie)
        scraper = BaseScraper()

        st, is_new = scraper.upsert_showtime(theater, movie, future, tickets_url="")
        db.session.commit()
        assert is_new and st.tickets_url == ""

        st2, is_new2 = scraper.upsert_showtime(
            theater, movie, future, tickets_url="https://example.com/buy"
        )
        db.session.commit()
        assert not is_new2
        assert st2.tickets_url == "https://example.com/buy"

        # An empty URL on a later scrape must NOT wipe the stored one.
        st3, _ = scraper.upsert_showtime(theater, movie, future, tickets_url="")
        db.session.commit()
        assert st3.tickets_url == "https://example.com/buy"


# -- Expired showtime cleanup ------------------------------------------


class TestExpiredShowtimeCleanup:
    def test_cleanup_removes_past_showtimes(self, app, sample_theater, sample_movie):
        """cleanup_expired_showtimes() deletes showtimes in the past."""
        from datetime import datetime, timedelta, timezone

        from app.scrapers import cleanup_expired_showtimes

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
        from app.scrapers import cleanup_expired_showtimes

        with app.app_context():
            count = cleanup_expired_showtimes()
            assert count == 0


# -- Orphaned movie cleanup -------------------------------------------


class TestOrphanedMovieCleanup:
    def test_orphaned_movie_deleted(self, app):
        """A movie with no showtimes and no alert references is deleted."""
        from app.scrapers import cleanup_orphaned_movies

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
        from app.scrapers import cleanup_orphaned_movies

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

        from app.scrapers import cleanup_orphaned_movies

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


# ── Alert target date & buffer ────────────────────────────────────────


class TestAlertTargetDate:
    """Tests for the optional target_date / target_date_buffer fields on AlertPreference."""

    # ── Model & property ─────────────────────────────────────────────────

    def test_model_stores_target_date(self, app, sample_user, sample_theater):
        from datetime import date
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=date(2026, 7, 25),
            )
            db.session.add(pref)
            db.session.commit()
            fetched = AlertPreference.query.get(pref.id)
            assert fetched.target_date == date(2026, 7, 25)
            assert fetched.target_date_buffer is None

    def test_model_stores_buffer(self, app, sample_user, sample_theater):
        from datetime import date
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=date(2026, 7, 25),
                target_date_buffer=3,
            )
            db.session.add(pref)
            db.session.commit()
            fetched = AlertPreference.query.get(pref.id)
            assert fetched.target_date_buffer == 3

    def test_target_date_range_no_buffer(self, app, sample_user, sample_theater):
        from datetime import date
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=date(2026, 7, 25),
            )
            db.session.add(pref)
            db.session.commit()
            date_from, date_to = pref.target_date_range
            assert date_from == date(2026, 7, 25)
            assert date_to == date(2026, 7, 25)

    def test_target_date_range_with_buffer(self, app, sample_user, sample_theater):
        from datetime import date
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=date(2026, 7, 25),
                target_date_buffer=2,
            )
            db.session.add(pref)
            db.session.commit()
            date_from, date_to = pref.target_date_range
            assert date_from == date(2026, 7, 23)
            assert date_to == date(2026, 7, 27)

    def test_target_date_range_none_when_no_date(self, app, sample_user, sample_theater):
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater)
            db.session.add(pref)
            db.session.commit()
            date_from, date_to = pref.target_date_range
            assert date_from is None
            assert date_to is None

    def test_to_dict_includes_target_date(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=date(2026, 7, 25),
                target_date_buffer=2,
            )
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()
            d = pref.to_dict()
            assert d["target_date"] == "2026-07-25"
            assert d["target_date_buffer"] == 2

    # ── API ──────────────────────────────────────────────────────────────

    def test_api_creates_alert_with_target_date(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        resp = auth_client.post("/api/alerts", json={
            "user_id": sample_user,
            "movie_ids": [sample_movie],
            "theater_id": sample_theater,
            "target_date": "2026-07-25",
            "target_date_buffer": 2,
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["target_date"] == "2026-07-25"
        assert data["target_date_buffer"] == 2

    def test_api_rejects_invalid_target_date(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        resp = auth_client.post("/api/alerts", json={
            "user_id": sample_user,
            "movie_ids": [sample_movie],
            "theater_id": sample_theater,
            "target_date": "not-a-date",
        })
        assert resp.status_code == 400

    def test_same_date_is_duplicate(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        payload = {
            "user_id": sample_user,
            "movie_ids": [sample_movie],
            "theater_id": sample_theater,
            "target_date": "2026-07-25",
        }
        r1 = auth_client.post("/api/alerts", json=payload)
        assert r1.status_code == 201
        r2 = auth_client.post("/api/alerts", json=payload)
        assert r2.status_code == 409

    def test_different_dates_are_not_duplicates(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        base = {
            "user_id": sample_user,
            "movie_ids": [sample_movie],
            "theater_id": sample_theater,
        }
        r1 = auth_client.post("/api/alerts", json={**base, "target_date": "2026-07-25"})
        r2 = auth_client.post("/api/alerts", json={**base, "target_date": "2026-07-26"})
        assert r1.status_code == 201
        assert r2.status_code == 201

    def test_undated_and_dated_are_not_duplicates(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        base = {"user_id": sample_user, "movie_ids": [sample_movie], "theater_id": sample_theater}
        r1 = auth_client.post("/api/alerts", json=base)
        r2 = auth_client.post("/api/alerts", json={**base, "target_date": "2026-07-25"})
        assert r1.status_code == 201
        assert r2.status_code == 201

    def test_overlap_warning_when_undated_alert_exists(
        self, auth_client, app, sample_user, sample_movie, sample_theater
    ):
        base = {"user_id": sample_user, "movie_ids": [sample_movie], "theater_id": sample_theater}
        auth_client.post("/api/alerts", json=base)
        r2 = auth_client.post("/api/alerts", json={**base, "target_date": "2026-07-25"})
        assert r2.status_code == 201
        assert "warning" in r2.get_json()
        assert "undated alert" in r2.get_json()["warning"].lower()

    # ── Notification processor ────────────────────────────────────────────

    def _make_pref_with_showtime(self, app, sample_user, sample_theater, sample_movie, show_date, target_date, buffer=None):
        """Helper: create an alert pref + a showtime on show_date, return (pref_id, showtime_id)."""
        from datetime import datetime, timezone
        with app.app_context():
            pref = AlertPreference(
                user_id=sample_user,
                theater_id=sample_theater,
                target_date=target_date,
                target_date_buffer=buffer,
            )
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            show_dt = datetime(show_date.year, show_date.month, show_date.day, 19, 0, tzinfo=timezone.utc)
            st = Showtime(theater_id=sample_theater, movie_id=sample_movie, show_datetime=show_dt)
            db.session.add(st)
            db.session.commit()
            return pref.id, st.id

    def test_processor_fires_on_target_date(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        from app.notifications import _get_matching_showtimes_for_pref
        target = date(2026, 7, 25)
        pref_id, _ = self._make_pref_with_showtime(
            app, sample_user, sample_theater, sample_movie,
            show_date=target, target_date=target,
        )
        with app.app_context():
            pref = AlertPreference.query.get(pref_id)
            candidates = Showtime.query.filter_by(theater_id=sample_theater).all()
            matching = _get_matching_showtimes_for_pref(pref, candidates)
            assert len(matching) == 1

    def test_processor_does_not_fire_on_wrong_date(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        from app.notifications import _get_matching_showtimes_for_pref
        pref_id, _ = self._make_pref_with_showtime(
            app, sample_user, sample_theater, sample_movie,
            show_date=date(2026, 7, 20),
            target_date=date(2026, 7, 25),
        )
        with app.app_context():
            pref = AlertPreference.query.get(pref_id)
            candidates = Showtime.query.filter_by(theater_id=sample_theater).all()
            matching = _get_matching_showtimes_for_pref(pref, candidates)
            assert len(matching) == 0

    def test_processor_fires_within_buffer(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        from app.notifications import _get_matching_showtimes_for_pref
        # showtime is 2 days before target — within ±3 buffer
        pref_id, _ = self._make_pref_with_showtime(
            app, sample_user, sample_theater, sample_movie,
            show_date=date(2026, 7, 23),
            target_date=date(2026, 7, 25),
            buffer=3,
        )
        with app.app_context():
            pref = AlertPreference.query.get(pref_id)
            candidates = Showtime.query.filter_by(theater_id=sample_theater).all()
            matching = _get_matching_showtimes_for_pref(pref, candidates)
            assert len(matching) == 1

    def test_processor_does_not_fire_outside_buffer(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        from app.notifications import _get_matching_showtimes_for_pref
        # showtime is 5 days before target — outside ±3 buffer
        pref_id, _ = self._make_pref_with_showtime(
            app, sample_user, sample_theater, sample_movie,
            show_date=date(2026, 7, 20),
            target_date=date(2026, 7, 25),
            buffer=3,
        )
        with app.app_context():
            pref = AlertPreference.query.get(pref_id)
            candidates = Showtime.query.filter_by(theater_id=sample_theater).all()
            matching = _get_matching_showtimes_for_pref(pref, candidates)
            assert len(matching) == 0

    def test_processor_fires_on_any_date_when_no_target(self, app, sample_user, sample_theater, sample_movie):
        from datetime import date
        from app.notifications import _get_matching_showtimes_for_pref
        pref_id, _ = self._make_pref_with_showtime(
            app, sample_user, sample_theater, sample_movie,
            show_date=date(2026, 7, 20),
            target_date=None,
        )
        with app.app_context():
            pref = AlertPreference.query.get(pref_id)
            candidates = Showtime.query.filter_by(theater_id=sample_theater).all()
            matching = _get_matching_showtimes_for_pref(pref, candidates)
            assert len(matching) == 1


# ── Cineplex scraper: date window ─────────────────────────────────────


class TestCineplexScraperDateWindow:
    """Tests that the Cineplex scraper uses all bookable dates, not a 14-day cap."""

    def test_scrape_includes_dates_beyond_14_days(self, app, sample_theater):
        """Showtimes returned by the API more than 14 days out must not be dropped."""
        from datetime import date, datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from app.scrapers.cineplex import CineplexScraper

        today = date.today()
        # Simulate a date 30 days ahead — previously skipped by the 14-day cap.
        far_date = today + timedelta(days=30)
        far_date_iso = far_date.isoformat()

        fake_session_data = [{
            "dates": [{
                "movies": [{
                    "name": "The Odyssey",
                    "experiences": [{
                        "experienceTypes": ["IMAX", "70mm"],
                        "sessions": [{
                            "isInThePast": False,
                            "showStartDateTime": f"{far_date_iso}T19:00:00",
                            "ticketingUrl": "https://example.com/tickets",
                        }],
                    }],
                }],
            }],
        }]

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            theater.website = "https://www.cineplex.com/theatre/test"
            db.session.commit()

            scraper = CineplexScraper()
            with patch.object(scraper, "_get_location_id", return_value=9999), \
                 patch.object(scraper, "_get_bookable_dates", return_value=[far_date_iso]):
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = fake_session_data
                with patch("requests.get", return_value=mock_resp):
                    # {None} is the "any movie" sentinel used by _movie_wanted
                    result = scraper.scrape_theater(theater, {None})
            assert len(result) == 1, "Showtime 30 days out should be included"
            assert result[0].show_datetime.date() == far_date

    def test_scrape_skips_past_dates(self, app, sample_theater):
        """Dates before today returned by bookable endpoint are skipped."""
        from datetime import date, timedelta
        from unittest.mock import patch

        from app.scrapers.cineplex import CineplexScraper

        yesterday = (date.today() - timedelta(days=1)).isoformat()

        with app.app_context():
            theater = Theater.query.get(sample_theater)
            theater.website = "https://www.cineplex.com/theatre/test"
            db.session.commit()

            scraper = CineplexScraper()
            with patch.object(scraper, "_get_location_id", return_value=9999), \
                 patch.object(scraper, "_get_bookable_dates", return_value=[yesterday]):
                result = scraper.scrape_theater(theater, set())
            assert result == [], "Past dates should be skipped without calling the showtimes API"


# ── Admin logs: UTC timestamp attribute ──────────────────────────────


class TestAdminLogsTimestamp:
    """Tests that activity log entries render with a data-utc attribute for browser-side TZ conversion."""

    def test_log_rows_have_data_utc_attribute(self, auth_client, app):
        from app.log_utils import write_log
        with app.app_context():
            write_log("system", "Test log entry for timestamp test")

        resp = auth_client.get("/admin/logs")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'data-utc="' in html, "Each log timestamp cell must have a data-utc attribute"

    def test_log_utc_value_is_iso_format(self, auth_client, app):
        """The data-utc value should be a parseable ISO datetime string."""
        import re
        from app.log_utils import write_log
        with app.app_context():
            write_log("system", "ISO format test entry")

        resp = auth_client.get("/admin/logs")
        html = resp.data.decode()
        # Extract first data-utc value and verify it looks like an ISO datetime
        match = re.search(r'data-utc="([^"]+)"', html)
        assert match, "No data-utc attribute found in log page HTML"
        utc_val = match.group(1)
        # Should be parseable as a datetime (YYYY-MM-DDTHH:MM:SS or similar)
        assert "T" in utc_val, f"Expected ISO datetime with T separator, got: {utc_val}"


# ── v1.14: Password complexity (#24) ─────────────────────────────────


class TestPasswordComplexity:
    """Unit tests for the validate_password_strength helper."""

    def test_too_short(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("Abc1!") is not None

    def test_too_long(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("Abcdef1!" * 17) is not None  # 136 chars

    def test_missing_uppercase(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("abc123!!") is not None

    def test_missing_lowercase(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("ABC123!!") is not None

    def test_missing_digit(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("Abcdef!!") is not None

    def test_missing_special(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("Abcdef12") is not None

    def test_valid_password(self, app):
        from app.auth import validate_password_strength
        with app.app_context():
            assert validate_password_strength("Abcdef1!") is None

    def test_reuse_current_password(self, app):
        from app.auth import validate_password_strength
        from werkzeug.security import generate_password_hash
        with app.app_context():
            h = generate_password_hash("Abcdef1!")
            assert validate_password_strength("Abcdef1!", current_hash=h) is not None

    def test_different_from_current_password(self, app):
        from app.auth import validate_password_strength
        from werkzeug.security import generate_password_hash
        with app.app_context():
            h = generate_password_hash("OldPass9@")
            assert validate_password_strength("NewPass9@", current_hash=h) is None


# ── v1.14: Forgot / reset password (#22) ─────────────────────────────


class TestForgotResetPassword:
    def test_forgot_password_page_loads(self, client):
        resp = client.get("/forgot-password")
        assert resp.status_code == 200
        assert b"Reset" in resp.data or b"forgot" in resp.data.lower()

    def test_forgot_password_unknown_email_still_shows_confirmation(self, client):
        resp = client.post(
            "/forgot-password",
            data={"email": "nobody@example.com"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"reset link" in resp.data.lower() or b"inbox" in resp.data.lower()

    def test_forgot_password_known_email_generates_token(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user.reset_token is None
        client.post("/forgot-password", data={"email": "admin"})
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user.reset_token is not None
            assert user.reset_token_expiry is not None

    def test_reset_password_invalid_token_shows_error(self, client):
        resp = client.get("/reset-password/not-a-real-token")
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_reset_password_valid_token_sets_new_password(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            raw = user.generate_reset_token()
            db.session.commit()
        resp = client.post(
            f"/reset-password/{raw}",
            data={"new_password": "NewPass1!", "confirm_password": "NewPass1!"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user.check_password("NewPass1!")
            assert user.reset_token is None

    def test_reset_password_weak_password_rejected(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            raw = user.generate_reset_token()
            db.session.commit()
        resp = client.post(
            f"/reset-password/{raw}",
            data={"new_password": "weak", "confirm_password": "weak"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Password must" in resp.data

    def test_reset_password_mismatch_rejected(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            raw = user.generate_reset_token()
            db.session.commit()
        resp = client.post(
            f"/reset-password/{raw}",
            data={"new_password": "NewPass1!", "confirm_password": "Different1!"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"do not match" in resp.data.lower()

    def test_expired_token_rejected(self, app, client):
        from datetime import datetime, timedelta, timezone
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            raw = user.generate_reset_token()
            user.reset_token_expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
            db.session.commit()
        resp = client.get(f"/reset-password/{raw}")
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_second_reset_request_within_cooldown_does_not_overwrite_token(self, app, client):
        """A second forgot-password request within 2 minutes must not overwrite the first token."""
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            raw = user.generate_reset_token()
            db.session.commit()
            first_token_hash = user.reset_token

        # Immediately request again — within the 2-minute cooldown window
        client.post("/forgot-password", data={"email": "admin"})

        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user.reset_token == first_token_hash, (
                "Token was overwritten within the cooldown window"
            )


# ── v1.14: Session ping endpoint (#73) ───────────────────────────────


class TestSessionPing:
    def test_ping_unauthenticated_redirects(self, client):
        resp = client.get("/api/session/ping")
        assert resp.status_code in (302, 401)

    def test_ping_authenticated_returns_ok(self, auth_client):
        resp = auth_client.get("/api/session/ping")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_session_timeout_in_settings(self, auth_client):
        resp = auth_client.get("/admin/settings")
        assert resp.status_code == 200
        assert b"session_timeout_minutes" in resp.data or b"idle timeout" in resp.data.lower()


# ── v1.16: MFA (TOTP) flows (#25) ────────────────────────────────────


class TestMFA:
    def test_mfa_verify_page_requires_pending_session(self, client):
        resp = client.get("/mfa-verify", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_login_with_mfa_enabled_redirects_to_verify(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            user.generate_mfa_secret()
            user.mfa_enabled = True
            db.session.commit()

        resp = client.post(
            "/login",
            data={"email": "admin", "password": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/mfa-verify" in resp.headers["Location"]

    def test_mfa_verify_valid_totp_completes_login(self, app, client):
        import pyotp
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            secret = user.generate_mfa_secret()
            user.mfa_enabled = True
            user.force_password_change = False
            db.session.commit()

        client.post("/login", data={"email": "admin", "password": "admin"})
        code = pyotp.TOTP(secret).now()
        resp = client.post("/mfa-verify", data={"code": code, "use_recovery": "0"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Theaters" in resp.data or b"Alerts" in resp.data

    def test_mfa_verify_invalid_code_shows_error(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            user.generate_mfa_secret()
            user.mfa_enabled = True
            db.session.commit()

        client.post("/login", data={"email": "admin", "password": "admin"})
        resp = client.post("/mfa-verify", data={"code": "000000", "use_recovery": "0"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert b"Invalid" in resp.data

    def test_mfa_verify_valid_recovery_code_completes_login(self, app, client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            user.generate_mfa_secret()
            user.mfa_enabled = True
            user.force_password_change = False
            raw_codes = user.generate_recovery_codes()
            db.session.commit()
            first_code = raw_codes[0]

        client.post("/login", data={"email": "admin", "password": "admin"})
        resp = client.post(
            "/mfa-verify",
            data={"code": first_code, "use_recovery": "1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Theaters" in resp.data or b"Alerts" in resp.data

        # Recovery code must be consumed — using same code again must fail
        client.post("/logout")
        client.post("/login", data={"email": "admin", "password": "admin"})
        resp2 = client.post(
            "/mfa-verify",
            data={"code": first_code, "use_recovery": "1"},
            follow_redirects=True,
        )
        assert b"Invalid" in resp2.data

    def test_mfa_setup_page_loads(self, auth_client):
        resp = auth_client.get("/profile/mfa-setup")
        assert resp.status_code == 200
        assert b"authenticator" in resp.data.lower() or b"MFA" in resp.data

    def test_mfa_disable_requires_correct_password(self, app, auth_client):
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            user.generate_mfa_secret()
            user.mfa_enabled = True
            db.session.commit()

        resp = auth_client.post(
            "/profile/mfa-disable",
            data={"password": "wrongpassword"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert user.mfa_enabled is True

    def test_mfa_disable_with_correct_password(self, app, auth_client):
        # Setup in the outer session (same session the route will use) to avoid
        # stale identity-map state caused by Flask-SQLAlchemy's per-AppContext scoping.
        user = User.query.filter_by(email="admin").first()
        user.generate_mfa_secret()
        user.mfa_enabled = True
        db.session.commit()

        resp = auth_client.post(
            "/profile/mfa-disable",
            data={"password": "admin"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email="admin").first()
            assert not user.mfa_enabled
            assert user.mfa_secret is None


# ── v1.16: User invite flows (#23) ───────────────────────────────────


class TestUserInvite:
    def test_accept_invite_invalid_token_shows_expired(self, client):
        resp = client.get("/accept-invite/notarealtoken")
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_accept_invite_valid_token_shows_form(self, app, client):
        with app.app_context():
            role = Role.query.filter_by(name="user").first()
            invite, raw_token = UserInvite.create(
                email="newuser@example.com", role_id=role.id, created_by_id=None
            )
            db.session.add(invite)
            db.session.commit()

        resp = client.get(f"/accept-invite/{raw_token}")
        assert resp.status_code == 200
        assert b"newuser@example.com" in resp.data

    def test_accept_invite_creates_user(self, app, client):
        with app.app_context():
            role = Role.query.filter_by(name="user").first()
            invite, raw_token = UserInvite.create(
                email="invited@example.com", role_id=role.id, created_by_id=None
            )
            db.session.add(invite)
            db.session.commit()

        resp = client.post(
            f"/accept-invite/{raw_token}",
            data={
                "name": "New User",
                "password": "Invite1!Pass",
                "confirm_password": "Invite1!Pass",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            user = User.query.filter_by(email="invited@example.com").first()
            assert user is not None
            assert user.name == "New User"
            invite_db = UserInvite.query.filter_by(email="invited@example.com").first()
            assert invite_db.accepted_at is not None

    def test_accept_invite_expired_token_rejected(self, app, client):
        from datetime import datetime, timedelta, timezone
        with app.app_context():
            role = Role.query.filter_by(name="user").first()
            invite, raw_token = UserInvite.create(
                email="expired@example.com", role_id=role.id, created_by_id=None
            )
            invite.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
            db.session.add(invite)
            db.session.commit()

        resp = client.get(f"/accept-invite/{raw_token}")
        assert resp.status_code == 200
        assert b"invalid" in resp.data.lower() or b"expired" in resp.data.lower()

    def test_admin_invite_revoke(self, app, auth_client):
        with app.app_context():
            role = Role.query.filter_by(name="user").first()
            invite, _ = UserInvite.create(
                email="torevoke@example.com", role_id=role.id, created_by_id=None
            )
            db.session.add(invite)
            db.session.commit()
            invite_id = invite.id

        resp = auth_client.post(f"/admin/users/invites/{invite_id}/revoke",
                                follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            assert UserInvite.query.get(invite_id) is None


# ── Movies tab (#29) ──────────────────────────────────────────────────


class TestMoviesTab:
    def _make_alert_with_movie(self, app, user_id, theater_id, movie_id):
        """Helper: create an active AlertPreference with one AlertMovie."""
        pref = AlertPreference(user_id=user_id, theater_id=theater_id, is_active=True)
        db.session.add(pref)
        db.session.flush()
        am = AlertMovie(alert_id=pref.id, movie_id=movie_id)
        db.session.add(am)
        db.session.commit()
        return pref.id

    def test_movies_page_loads_empty(self, auth_client):
        resp = auth_client.get("/movies")
        assert resp.status_code == 200
        assert b"No tracked movies" in resp.data or b"My Movies" in resp.data

    def test_movies_page_shows_tracked_movie(self, app, auth_client, sample_movie, sample_theater, sample_user):
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)

        resp = auth_client.get("/movies")
        assert resp.status_code == 200
        assert b"Interstellar IMAX" in resp.data

    def test_movies_page_unauthenticated_redirects(self, client):
        resp = client.get("/movies")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_movies_page_persists_after_alert_fires(self, app, auth_client, sample_movie, sample_theater, sample_user):
        """Movies must remain visible even after the alert fires (is_active=False)."""
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater, is_active=False)
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()

        resp = auth_client.get("/movies")
        assert resp.status_code == 200
        assert b"Interstellar IMAX" in resp.data

    def test_movies_page_excludes_any_movie_alerts(self, app, auth_client, sample_theater, sample_user):
        """An alert with no AlertMovies (any-movie) should not appear on the movies tab."""
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater, is_active=True)
            db.session.add(pref)
            db.session.commit()

        resp = auth_client.get("/movies")
        assert resp.status_code == 200
        # Page loads fine; no movie rows since no AlertMovie rows exist
        assert b"No tracked movies" in resp.data or b"My Movies" in resp.data

    def test_movies_page_shows_next_showtime(self, app, auth_client, sample_movie, sample_theater, sample_user):
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)
            future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5)
            st = Showtime(
                theater_id=sample_theater,
                movie_id=sample_movie,
                show_datetime=future,
                tickets_available=True,
                format_type="IMAX",
            )
            db.session.add(st)
            db.session.commit()

        resp = auth_client.get("/movies")
        assert resp.status_code == 200
        assert b"Next:" in resp.data


# ── Movie detail page (#30) ───────────────────────────────────────────


class TestMovieDetail:
    def _make_alert_with_movie(self, app, user_id, theater_id, movie_id):
        pref = AlertPreference(user_id=user_id, theater_id=theater_id, is_active=True)
        db.session.add(pref)
        db.session.flush()
        am = AlertMovie(alert_id=pref.id, movie_id=movie_id)
        db.session.add(am)
        db.session.commit()
        return pref.id

    def test_movie_detail_loads(self, app, auth_client, sample_movie, sample_theater, sample_user):
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"Interstellar IMAX" in resp.data

    def test_movie_detail_404_not_tracked(self, auth_client, sample_movie):
        # Movie exists but user has no alert for it
        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 404

    def test_movie_detail_404_unknown_movie(self, auth_client):
        resp = auth_client.get("/movies/9999")
        assert resp.status_code == 404

    def test_movie_detail_unauthenticated_redirects(self, client, sample_movie):
        resp = client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_movie_detail_shows_showtimes(self, app, auth_client, sample_movie, sample_theater, sample_user):
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)
            future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)
            st = Showtime(
                theater_id=sample_theater,
                movie_id=sample_movie,
                show_datetime=future,
                tickets_available=True,
                format_type="IMAX",
            )
            db.session.add(st)
            db.session.commit()

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"Upcoming Showtimes" in resp.data
        assert b"Test IMAX Theater" in resp.data

    def test_movie_detail_shows_showtimes_from_any_theater(self, app, auth_client, sample_movie, sample_theater, sample_user):
        """Showtimes at theaters OTHER than the alerted theater must still appear."""
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)
            other = Theater(name="Other IMAX", chain="Regal", city="Othertown", state="ON")
            db.session.add(other)
            db.session.flush()
            future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2)
            db.session.add(Showtime(theater_id=other.id, movie_id=sample_movie,
                                   show_datetime=future, format_type="IMAX"))
            db.session.commit()

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"Other IMAX" in resp.data

    def test_movie_detail_hides_past_showtimes(self, app, auth_client, sample_movie, sample_theater, sample_user):
        from datetime import datetime, timezone, timedelta
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)
            past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            st = Showtime(
                theater_id=sample_theater,
                movie_id=sample_movie,
                show_datetime=past,
                tickets_available=True,
                format_type="IMAX",
            )
            db.session.add(st)
            db.session.commit()

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"No upcoming showtimes" in resp.data

    def test_movie_detail_shows_alerts_section(self, app, auth_client, sample_movie, sample_theater, sample_user):
        with app.app_context():
            self._make_alert_with_movie(app, sample_user, sample_theater, sample_movie)

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"Your Alerts" in resp.data
        assert b"Test IMAX Theater" in resp.data

    def test_movie_detail_accessible_after_alert_fires(self, app, auth_client, sample_movie, sample_theater, sample_user):
        """Detail page must remain accessible after an alert fires (is_active=False)."""
        with app.app_context():
            pref = AlertPreference(user_id=sample_user, theater_id=sample_theater, is_active=False)
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()

        resp = auth_client.get(f"/movies/{sample_movie}")
        assert resp.status_code == 200
        assert b"Interstellar IMAX" in resp.data


class TestRadiusAlert:
    """Tests for radius-based alert creation and target resolution."""

    def _set_user_location(self, user_id, lat, lng):
        user = User.query.get(user_id)
        user.location_lat = lat
        user.location_lon = lng
        db.session.commit()

    def test_create_radius_alert_requires_location(self, app, auth_client, sample_user, sample_movie):
        """API rejects radius alert when user has no saved location."""
        resp = auth_client.post("/api/alerts", json={
            "user_id": sample_user,
            "radius_km": 100.0,
            "movie_ids": [],
        })
        assert resp.status_code == 400
        assert b"location" in resp.data.lower()

    def test_create_radius_alert_succeeds_with_location(self, app, auth_client, sample_user, sample_movie):
        """Radius alert is created when user has a saved location."""
        # Set location directly in the active fixture context (no nested context push)
        self._set_user_location(sample_user, 34.05, -118.24)
        resp = auth_client.post("/api/alerts", json={
            "user_id": sample_user,
            "radius_km": 200.0,
            "tmdb_ids": [],
        })
        assert resp.status_code == 201, resp.get_json()
        data = resp.get_json()
        assert data["radius_km"] == 200.0
        assert data["theater_id"] is None

    def test_create_radius_alert_invalid_radius(self, app, auth_client, sample_user):
        """API rejects non-positive radius."""
        self._set_user_location(sample_user, 34.05, -118.24)
        resp = auth_client.post("/api/alerts", json={
            "user_id": sample_user,
            "radius_km": -5.0,
        })
        assert resp.status_code == 400

    def test_get_active_targets_includes_nearby_theater(self, app, sample_user, sample_theater, sample_movie):
        """_get_active_targets expands a radius alert into specific theater IDs."""
        from app.scrapers.base import _get_active_targets
        # Theater is at (34.05, -118.24); put user at same location → 0 km away
        with app.app_context():
            self._set_user_location(sample_user, 34.05, -118.24)
            pref = AlertPreference(user_id=sample_user, radius_km=50.0, is_active=True, alert_sent=False)
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()
            targets = _get_active_targets()

        assert sample_theater in targets
        assert sample_movie in targets[sample_theater]

    def test_get_active_targets_excludes_far_theater(self, app, sample_user, sample_theater, sample_movie):
        """_get_active_targets does not include theaters outside the radius."""
        from app.scrapers.base import _get_active_targets
        # Put user far away (New York area) so theater in LA is outside 50 km
        with app.app_context():
            self._set_user_location(sample_user, 40.71, -74.01)
            pref = AlertPreference(user_id=sample_user, radius_km=50.0, is_active=True, alert_sent=False)
            db.session.add(pref)
            db.session.flush()
            db.session.add(AlertMovie(alert_id=pref.id, movie_id=sample_movie))
            db.session.commit()
            targets = _get_active_targets()

        assert sample_theater not in targets
