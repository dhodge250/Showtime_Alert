# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the application
python run.py

# Run tests
pytest

# Run a single test
pytest tests/test_app.py::test_function_name -v

# Install dependencies
pip install -r requirements.txt
```

`use_reloader=False` is set in `run.py` intentionally — the APScheduler would double-start if the reloader spawned a second process.

## Architecture

### App Factory Pattern

`app/__init__.py` exports `create_app(config_name)` which wires everything together. `run.py` calls it with `FLASK_ENV` (defaults to `"development"`). Tests use `create_app("testing")` which targets an in-memory SQLite DB.

`create_app` does more than Flask wiring: on every startup it runs `_run_migrations()` (inline ALTER TABLE statements, idempotent), seeds roles/admin/lookup tables/settings/theaters, and loads notification credentials from the DB into `app.config`.

### Blueprints

Three blueprints registered in `create_app`:
- `auth_bp` (`app/auth.py`) — `/login`, `/logout`
- `main_bp` (`app/routes.py`) — all UI routes, protected by `@login_required`
- `api_bp` (`app/routes.py`, prefix `/api`) — REST endpoints, returns JSON

`require_role(*roles)` in `app/auth.py` is a decorator combining `@login_required` with a role check. Roles are `admin`, `editor`, `user`.

### Database & Migrations

SQLAlchemy with SQLite (default) or PostgreSQL. Schema changes are applied as raw `ALTER TABLE` statements inside `_run_migrations()` in `app/__init__.py` — there is no Alembic. Add new column migrations there, not as model-only changes.

`Theater` has both legacy string columns (`chain`, `country`, `state`, `city`, `screen_size`, `projector_type`, `audio_system`) and newer FK columns pointing to lookup tables. Property helpers on `Theater` resolve the FK value and fall back to the string column, so both can coexist during migration. `app/lookup_helpers.py` has `get_or_create_*` helpers for these FK tables.

### Alert Data Model

`AlertPreference` → `AlertMovie` (many-to-many via join table) replaces the old single `movie_id` column. An alert with zero `AlertMovie` rows means "any movie." `_migrate_legacy_alert_movies()` in `__init__.py` back-fills existing rows on startup.

### Scheduler (`app/scheduler.py`)

APScheduler `BackgroundScheduler` runs three jobs:
1. **Showtime scraper** — every N minutes (default 30), calls `run_all_scrapers()` then `process_new_showtimes()`
2. **Venue crawler** — every N days (default 7), calls `run_venue_crawl()`
3. **Cleanup** — every N hours (default 24), deletes past showtimes

Jobs are demand-driven: the scraper only fetches pages for theaters/movies that have active, unsent `AlertPreference` rows.

### Theater Seeding

On first boot (empty `theaters` table), `_seed_theaters_from_csv()` loads `seeds/imax_theaters.csv`. The deprecated `_maybe_seed_venues()` / venue crawler path still exists in the code but is no longer the primary seeding mechanism.

### Notifications (`app/notifications.py`)

Sends email via raw SMTP and SMS via Twilio. Credentials are pulled from `app.config`, which is populated from the `Settings` DB table at startup (and after admin saves settings). The `Settings` table is the source of truth for notification config at runtime; `.env` / environment variables seed `app.config` only on first load.

### TMDB Integration (`app/tmdb.py`)

Optional movie metadata enrichment. API key stored in the `Settings` table under key `tmdb_api_key`. All TMDB functions gracefully no-op when the key is absent.

### Vendor Assets

`app/static/vendor/` contains bundled frontend libraries (Leaflet.js, etc.). The theater map uses Leaflet + OpenStreetMap — no API key required.

## Default Credentials

On first run the app seeds a default admin account: **email `admin`, password `admin`**. The `TestingConfig` uses the same seeding path, so `auth_client` fixture in tests logs in with these credentials.

## Settings Priority

`config.py` reads `.env` → environment variables for initial values. After startup, `app/models.py:Settings` rows are the live source for mail/Twilio credentials. Admin UI (`/admin/settings`) writes to `Settings` and calls `_load_settings_into_config()` to sync back to `app.config`.
