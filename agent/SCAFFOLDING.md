# IMAX Alert — Agent Scaffolding Reference

This file is the authoritative reference for any AI agent making changes to this codebase.
Read it fully before touching any file. Follow the Agent Rules in every section.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [File Map](#3-file-map)
4. [Data Models](#4-data-models)
5. [Routes & API Reference](#5-routes--api-reference)
6. [Key Patterns & Conventions](#6-key-patterns--conventions)
7. [Environment & Setup](#7-environment--setup)
8. [Agent Rules](#8-agent-rules)
9. [Docker & TrueNAS Scale Deployment](#9-docker--truenas-scale-deployment)

---

## 1. Project Overview

IMAX Alert is a Flask web application that monitors IMAX theater chain websites for new
showtimes and ticket availability. When a new showtime is detected, it automatically notifies
users via email (SMTP) and/or SMS (Twilio) based on their alert preferences.

The list of monitored theaters is seeded from `seeds/imax_theaters.csv` on first boot.
A separate **venue crawler** can also fetch and geocode theaters from the IMAX fandom wiki.

- **Runtime:** Python 3.10+, Flask 3.1.1, SQLite (default), APScheduler
- **Production server:** Gunicorn via `wsgi.py` (1 worker + 4 threads to prevent duplicate scheduler instances)
- **Deployment target:** Docker container on TrueNAS Scale (Custom App via YAML) or any Docker host
- **Authentication:** Flask-Login with role-based access (admin, editor, user)
- **Default credentials:** email `admin`, password `admin` — user is forced to change on first login

---

## 2. Architecture

```
wsgi.py (production)          run.py (local dev)
  └── create_app()                └── create_app()
        │  app/__init__.py — factory, DB init, migrations, seeding
        ├── Flask app
        ├── Flask-Login (auth)    auth_bp  app/auth.py
        ├── SQLAlchemy (db)       app/models.py
        ├── Flask-Limiter
        ├── main_bp (UI routes)   app/routes.py
        ├── api_bp  (REST routes) app/routes.py  prefix /api
        └── start_scheduler(app)  app/scheduler.py
              └── BackgroundScheduler (APScheduler)
                    ├── every N min:  run_all_scrapers()     app/scraper.py
                    │                 └── process_new_showtimes()  app/notifications.py
                    │                       ├── send_email()   (smtplib)
                    │                       └── send_sms()     (Twilio)
                    ├── every N min:  standalone alert check  (ALERT_INTERVAL_MINUTES)
                    ├── every N days: run_venue_crawl()       app/venue_crawler.py
                    └── every N hrs:  cleanup past showtimes
```

**Request flow (UI):** Browser → `auth_bp` login OR `main_bp` route (requires login) → SQLAlchemy query → Jinja2 template

**Request flow (API):** Client → `api_bp` route (admin endpoints require role check) → SQLAlchemy query → `jsonify()`

**Scrape flow:** APScheduler tick → `run_all_scrapers()` → each chain scraper fetches theater
website → `upsert_showtime()` writes to DB → `process_new_showtimes()` checks
`AlertPreference` / `AlertMovie` table → sends email/SMS → records `Notification` row

**Settings priority:** `config.py` reads `.env` / environment variables for initial values.
After startup, `Settings` DB rows are the live source for mail/Twilio credentials.
Admin UI (`/admin/settings`) writes to `Settings` and calls `_load_settings_into_config()`
to sync back into `app.config`.

---

## 3. File Map

```
IMAX_Alert/
├── run.py                   Entry point for local dev. Creates app, starts scheduler.
│                            use_reloader=False is REQUIRED — do not remove.
├── wsgi.py                  Gunicorn entry point for production. Checks SECRET_KEY,
│                            creates app, starts scheduler.
├── config.py                Dev / Prod / Testing config classes. All env vars mapped here.
├── requirements.txt         Pinned Python dependencies.
├── pytest.ini               Pytest configuration.
├── Dockerfile               Multi-stage image build. CMD runs Gunicorn via wsgi:app.
├── docker-compose.yml       Local Docker + TrueNAS YAML deployment.
├── .dockerignore            Excludes .env, venv, *.db, __pycache__ from image.
├── .env.example             Template .env file for local development.
├── .github/
│   └── workflows/
│       └── docker-publish.yml  CI/CD: run pytest → build → push to Docker Hub on main merge.
│
├── seeds/
│   └── imax_theaters.csv    Theater seed data. Loaded by _seed_theaters_from_csv() on first boot.
│
├── data/                    SQLite DB lives here in Docker (bind-mounted from host).
│                            Excluded from image via .dockerignore.
│
├── app/
│   ├── __init__.py          App factory (create_app). Registers blueprints, Flask-Login,
│   │                        Flask-Limiter. Runs _run_migrations(), seeds roles/admin/lookup
│   │                        tables/settings/theaters. Loads Settings into app.config.
│   ├── auth.py              auth_bp: /login, /logout, /change-password.
│   │                        require_role(*roles) decorator for admin/editor access.
│   ├── models.py            All SQLAlchemy ORM models. See Section 4.
│   ├── routes.py            main_bp (UI) + api_bp (REST). All application routes.
│   │                        See Section 5 for full route list.
│   ├── scraper.py           BaseScraper + AMCScraper, RegalScraper, CinemarkScraper, TCLScraper.
│   │                        run_all_scrapers(), ALL_SCRAPERS list, _parse_time_text().
│   ├── scheduler.py         APScheduler wrapper. Four jobs: showtime scraper, alert processor,
│   │                        venue crawler, cleanup. start_scheduler(), stop_scheduler(),
│   │                        get_scheduler_status(), reschedule_jobs().
│   ├── venue_crawler.py     Fetches IMAX fandom wiki → parses US venues → geocodes via Nominatim
│   │                        → upserts Theater rows. run_venue_crawl() is the main entry point.
│   ├── notifications.py     send_email(), send_sms(), process_new_showtimes(), _notify_for_showtime().
│   │                        Also: _build_email_body_multi(), _build_sms_body(), _record_notification().
│   ├── tmdb.py              Optional TMDB API integration. search_movie(), get_movie_details(),
│   │                        _format_result(). All functions no-op when API key is absent.
│   ├── lookup_helpers.py    get_or_create_chain/country/region/city helpers used during seeding
│   │                        and CSV import.
│   │
│   ├── templates/
│   │   ├── base.html            Base Jinja2 layout. Navbar, footer. Leaflet + vendor JS via /static/vendor.
│   │   ├── index.html           Dashboard: stats, showtime table, active alerts, scheduler status.
│   │   ├── login.html           Login form.
│   │   ├── change_password.html Password change form (shown on first login).
│   │   ├── theaters.html        Theater listing + Leaflet map.
│   │   ├── theater_detail.html  Single theater page with upcoming showtimes.
│   │   ├── alerts.html          Create / view / delete alert preferences.
│   │   ├── alert_detail.html    Single alert detail page with AlertMovie rows.
│   │   ├── profile.html         User settings (name, email, phone, notification prefs).
│   │   ├── admin_theaters.html  Admin: theater list with edit/delete/reactivate actions.
│   │   ├── admin_theater_edit.html  Admin: theater create/edit form.
│   │   ├── admin_users.html     Admin: user list.
│   │   ├── admin_user_edit.html Admin: user create/edit form with role assignment.
│   │   ├── admin_settings.html  Admin: SMTP, Twilio, TMDB, scheduler interval settings.
│   │   └── admin_lookup_*.html  Admin: CRUD pages for each lookup table (8 files).
│   │
│   └── static/
│       ├── css/style.css        Application stylesheet.
│       └── vendor/              Bundled frontend libraries (Leaflet.js, etc.).
│
├── tests/
│   └── test_app.py          pytest suite. 152+ tests covering models, all API endpoints,
│                            UI routes, scraper logic, notification builders, scheduler.
│
└── agent/
    ├── SCAFFOLDING.md       This file. Agent reference only.
    └── CLAUDE.md            Claude Code guidance (commands, architecture summary).
```

---

## 4. Data Models

All models live in `app/models.py`. The database is SQLite by default (`imax_alert.db`).
Schema changes are applied as idempotent `ALTER TABLE` statements in `_run_migrations()`
inside `app/__init__.py` — there is no Alembic.

### Lookup / Reference tables

| Model | Table | Purpose |
|---|---|---|
| `Role` | `roles` | User roles: `admin`, `editor`, `user` |
| `Chain` | `chains` | Theater chains (AMC, Regal, Cinemark, …) |
| `Country` | `countries` | Country names |
| `Region` | `regions` | State/Province (unique per country) |
| `City` | `cities` | City names (unique per region+country) |
| `Continent` | `continents` | Continent names |
| `AspectRatio` | `aspect_ratios` | Screen aspect ratios (1.43:1, 1.90:1, …) |
| `ProjectorType` | `projector_types` | Projector type names (IMAX with Laser, …) |
| `AudioSystem` | `audio_systems` | Audio system names (IMAX 12-channel, …) |
| `Settings` | `settings` | Key/value app settings (editable via admin UI) |

---

### Theater

Legacy string columns (`chain`, `country`, `state`, `city`, `screen_size`, `projector_type`, `audio_system`) co-exist with FK columns pointing to the lookup tables above. Property helpers on `Theater` resolve FK values and fall back to string columns.

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| name | String(200) | Not null |
| chain | String(100) | Legacy string — use `chain_ref` / `chain_name` property |
| country | String(100) | Legacy string |
| state | String(50) | Legacy string |
| city | String(100) | Legacy string |
| chain_id | FK chains.id | Preferred over `chain` string |
| country_id | FK countries.id | |
| region_id | FK regions.id | |
| city_id | FK cities.id | |
| aspect_ratio_id | FK aspect_ratios.id | |
| projector_type_id | FK projector_types.id | |
| audio_system_id | FK audio_systems.id | |
| continent_id | FK continents.id | |
| digital_projector_ar_id | FK aspect_ratios.id | Second AR for dual-laser theaters |
| film_projector_type_id | FK projector_types.id | |
| film_projector_type | String(100) | Raw string from CSV |
| commercial_films | String(20) | 'Yes', 'Limited', 'No' |
| screen_width_m | Float | Stored in metres; property helpers convert to ft |
| screen_height_m | Float | Stored in metres |
| address | String(300) | |
| zip_code | String(20) | |
| latitude | Float | Used for Leaflet map |
| longitude | Float | Used for Leaflet map |
| website | String(500) | |
| phone | String(30) | |
| image_url | String(500) | |
| is_active | Boolean | Default True |
| crawl_source | String(100) | "csv", "imax_fandom", "manual" |
| last_crawled_at | DateTime | UTC |
| created_at | DateTime | UTC |
| updated_at | DateTime | UTC, auto-updated |

Property helpers: `chain_name`, `country_name`, `region_name`, `city_name`, `aspect_ratio_label`, `projector_type_name`, `audio_system_name`, `continent_name`, `digital_projector_ar_label`, `film_projector_type_name`, `screen_width_ft`, `screen_height_ft`

---

### Movie

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| title | String(300) | Not null. Deduplicated via case-insensitive ilike. |
| description | Text | |
| image_url | String(500) | |
| release_date | Date | |
| genre | String(100) | |
| runtime_minutes | Integer | |
| rating | String(10) | |
| tmdb_id | Integer | Optional TMDB movie ID |
| created_at | DateTime | UTC |

---

### Showtime

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| theater_id | FK theaters.id | Not null |
| movie_id | FK movies.id | Not null |
| show_datetime | DateTime | Not null, UTC |
| tickets_available | Boolean | Default True |
| tickets_url | String(500) | |
| format_type | String(100) | "IMAX", "IMAX 3D", "IMAX with Laser" |
| first_seen | DateTime | UTC, set on insert |
| last_checked | DateTime | UTC, updated every scrape pass |

Unique constraint: `(theater_id, movie_id, show_datetime)` — named `uq_showtime`

---

### User

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| name | String(200) | Not null |
| email | String(300) | Unique |
| phone | String(30) | E.164 format for Twilio |
| password_hash | String(256) | Werkzeug hash |
| role_id | FK roles.id | Nullable — defaults to "user" behavior |
| is_active | Boolean | Default True |
| measurement_unit | String(10) | 'metric' or 'imperial' |
| location_lat | Float | |
| location_lon | Float | |
| location_name | String(300) | Human-readable location label |
| location_address | String(500) | Full address string for geocoding |
| notify_email | Boolean | Default True |
| notify_sms | Boolean | Default False |
| force_password_change | Boolean | Default False; set True on admin-created accounts |
| created_at | DateTime | UTC |

---

### AlertPreference

An alert can watch zero or more specific movies (`AlertMovie` rows). Zero movies = "any movie"
mode — all films at the theater trigger notifications and the alert never auto-closes.

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| user_id | FK users.id | Not null |
| theater_id | FK theaters.id | Nullable — None = any theater |
| movie_id | FK movies.id | Legacy single-movie column; always None on new rows |
| alert_sent | Boolean | Default False; True = all AlertMovie rows have fired |
| alert_sent_at | DateTime | UTC, set when alert_sent flips True |
| is_active | Boolean | Default True; soft-delete flag |
| max_notifications | Integer | Nullable = unlimited |
| notifications_fired | Integer | Default 0; incremented on each send |
| created_at | DateTime | UTC |

---

### AlertMovie

Tracks per-movie sent-state within an `AlertPreference`.

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| alert_preference_id | FK alert_preferences.id | Not null |
| movie_id | FK movies.id | Not null |
| sent | Boolean | Default False; True after first notification for this movie |
| sent_at | DateTime | UTC |

---

### Notification

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| user_id | FK users.id | Not null |
| alert_preference_id | FK alert_preferences.id | Nullable |
| showtime_id | FK showtimes.id | Nullable |
| method | String(20) | "email" or "sms" |
| message | Text | Full message body sent |
| sent_at | DateTime | UTC |
| success | Boolean | Default True |
| error_message | Text | Populated if success=False |

---

### Settings

Key/value store for runtime-configurable settings. Admin UI at `/admin/settings` writes here.
`_load_settings_into_config()` in `__init__.py` syncs these into `app.config` at startup and
after any save.

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | Auto |
| key | String(100) | Unique |
| value | Text | |
| updated_at | DateTime | UTC |

---

## 5. Routes & API Reference

### Auth Routes (`auth_bp` — no prefix)

| Method | URL | Description |
|---|---|---|
| GET, POST | `/login` | Login form |
| POST | `/logout` | Logout |
| GET, POST | `/change-password` | Password change (required before accessing app if `force_password_change=True`) |

### UI Routes (`main_bp` — no prefix, all require `@login_required`)

| Method | URL | Template | Description |
|---|---|---|---|
| GET | `/` | `index.html` | Dashboard |
| GET | `/theaters` | `theaters.html` | Theater list + map |
| GET | `/theaters/<id>` | `theater_detail.html` | Single theater + showtimes |
| GET | `/alerts` | `alerts.html` | Alert list |
| GET | `/alerts/<id>` | `alert_detail.html` | Alert detail (AlertMovie rows) |
| POST | `/alerts/<id>` | — | Delete (deactivate) alert |
| GET, POST | `/profile` | `profile.html` | User settings |
| GET | `/admin/theaters` | `admin_theaters.html` | Admin: theater list |
| GET, POST | `/admin/theaters/new` | `admin_theater_edit.html` | Admin: create theater |
| GET, POST | `/admin/theaters/<id>/edit` | `admin_theater_edit.html` | Admin: edit theater |
| POST | `/admin/theaters/<id>/delete` | — | Admin: soft-delete theater |
| POST | `/admin/theaters/<id>/reactivate` | — | Admin: reactivate theater |
| GET | `/admin/users` | `admin_users.html` | Admin: user list |
| GET, POST | `/admin/users/new` | `admin_user_edit.html` | Admin: create user |
| GET, POST | `/admin/users/<id>/edit` | `admin_user_edit.html` | Admin: edit user |
| POST | `/admin/users/<id>/delete` | — | Admin: delete user |
| GET, POST | `/admin/settings` | `admin_settings.html` | Admin: app settings |
| GET | `/admin/lookup` | — | Admin: lookup table index |
| GET | `/admin/lookup/aspect-ratios` | `admin_lookup_aspect_ratios.html` | |
| GET | `/admin/lookup/projector-types` | `admin_lookup_projector_types.html` | |
| GET | `/admin/lookup/audio-systems` | `admin_lookup_audio_systems.html` | |
| GET | `/admin/lookup/chains` | `admin_lookup_chains.html` | |
| GET | `/admin/lookup/countries` | `admin_lookup_countries.html` | |
| GET | `/admin/lookup/regions` | `admin_lookup_regions.html` | |
| GET | `/admin/lookup/cities` | `admin_lookup_cities.html` | |
| GET | `/admin/lookup/continents` | `admin_lookup_continents.html` | |

### REST API Routes (`api_bp` — prefix `/api`)

All responses are JSON. Admin-only endpoints require the `admin` role.

#### Theaters & Movies

| Method | URL | Description |
|---|---|---|
| GET | `/api/theaters` | All active theaters |
| GET | `/api/theaters/<id>` | Single theater (404 if not found) |
| PATCH | `/api/theaters/<id>` | Update theater fields |
| GET | `/api/movies` | All movies, alpha order |
| GET | `/api/movies/search` | Search via TMDB or local DB |
| GET | `/api/showtimes` | Future showtimes; filter: `theater_id`, `movie_id` |
| GET | `/api/showtimes/count` | Showtime count by filter |
| DELETE | `/api/showtimes` | Bulk delete; filter: `theater_id`, `movie_id`, `before` |

#### Users & Alerts

| Method | URL | Description |
|---|---|---|
| GET | `/api/users` | All users |
| POST | `/api/users` | Create user; requires `name`; 409 on duplicate email |
| PUT | `/api/users/<id>` | Update user (partial) |
| GET | `/api/alerts` | All alert preferences, newest first |
| POST | `/api/alerts` | Create alert; requires `user_id`; 409 on duplicate |
| GET | `/api/alerts/<id>` | Alert detail with AlertMovie rows |
| DELETE | `/api/alerts/<id>` | Soft-delete (sets `is_active=False`) |
| PATCH | `/api/alerts/<id>/reset` | Re-arm alert (clear `alert_sent`, reset AlertMovie rows) |
| PATCH | `/api/alerts/<id>/movies/<movie_id>/reset` | Re-arm single AlertMovie row |
| GET | `/api/notifications` | Last 50 notifications, newest first |

#### Scheduler & Crawler

| Method | URL | Description |
|---|---|---|
| GET | `/api/scheduler/status` | Scheduler state + next run times for all jobs |
| POST | `/api/scheduler/trigger` | Manually trigger showtime scrape |
| GET | `/api/venues/crawl/status` | Crawler status + theater count by source |
| POST | `/api/venues/crawl/trigger` | Manually trigger a full venue crawl |

#### Geocoding

| Method | URL | Description |
|---|---|---|
| POST | `/api/geocode` | Geocode a single address (Nominatim) |
| POST | `/api/geocode/bulk/trigger` | Geocode all un-geocoded theaters |
| GET | `/api/geocode/bulk/status` | Status of a running bulk geocode |

#### Admin — SMTP

| Method | URL | Description |
|---|---|---|
| POST | `/api/admin/smtp/test` | Test SMTP credentials; returns `{success, message}` |

#### Lookup CRUD (all require admin role)

Each lookup entity has GET (list), POST (create), DELETE `/<id>`, and PATCH `/<id>` endpoints
under `/api/lookup/<resource>`. Resources: `chains`, `countries`, `regions`, `cities`,
`aspect-ratios`, `projector-types`, `audio-systems`, `continents`.

---

## 6. Key Patterns & Conventions

### Authentication & Authorization

`auth_bp` in `app/auth.py` handles login/logout via Flask-Login. The `@login_required`
decorator (from Flask-Login) protects all main_bp routes. The `require_role(*roles)` decorator
in `auth.py` wraps `@login_required` and adds a role check — use it on admin routes:

```python
@main_bp.route("/admin/users")
@require_role("admin")
def admin_users():
    ...
```

`force_password_change=True` on a User triggers a redirect to `/change-password` from the
`before_app_request` hook in `auth.py`. Any test that logs in directly must clear this flag
on the seeded admin account, since it is set to `True` by the seeder.

### Migrations (`app/__init__.py`)

Schema changes are applied as `ALTER TABLE` SQL inside `_run_migrations()`. Wrap each
statement in a `try/except OperationalError` so it is idempotent (safe to re-run on every
startup):

```python
try:
    db.session.execute(text("ALTER TABLE theaters ADD COLUMN new_col TEXT"))
    db.session.commit()
except OperationalError:
    db.session.rollback()
```

Never use Alembic. Never drop columns — add nullable columns with a default.

### Settings

`Settings` rows are the runtime source of truth for mail/Twilio/TMDB credentials and
scheduler intervals. At startup and after any admin save, `_load_settings_into_config()`
copies them into `app.config`. Always read credentials from `app.config` or
`current_app.config` in code — never query `Settings` directly at runtime.

### Venue Crawler (`app/venue_crawler.py`)

Fetches the IMAX fandom wiki, filters to US commercial venues, geocodes via Nominatim (1.1s
delay between requests), and upserts `Theater` rows. `CHAIN_CANONICAL` and
`CHAIN_WEBSITE_MAP` dicts at the top of the file map raw wiki text to canonical chain names.

The crawler does NOT overwrite: `website` (if already set), `audio_system`, `phone`,
`image_url`. Theaters missing from the wiki are not deactivated automatically.

### CSV Theater Seeding (`app/__init__.py:_seed_theaters_from_csv`)

Loads `seeds/imax_theaters.csv` on first boot (empty `theaters` table) via
`_seed_theaters_from_csv()`. Uses `lookup_helpers.py` helpers to upsert FK lookup rows
before creating the `Theater` row.

### Adding a New Theater Chain Scraper

1. Add a class in `app/scraper.py` inheriting `BaseScraper`
2. Set `chain_name` to match the `chain` column / `Chain.name` in the DB
3. Implement `scrape_theater(self, theater: Theater) -> list[Showtime]`
4. Use `self.fetch(url)` for HTTP (handles errors, returns BeautifulSoup or None)
5. Use `self.get_or_create_movie(title)` to avoid duplicate Movie rows
6. Use `self.upsert_showtime(...)` — returns `(showtime, is_new)`; only append if `is_new`
7. Register the instance in `ALL_SCRAPERS` at the bottom of `scraper.py`
8. Add matching theaters to `seeds/imax_theaters.csv`

### Alert Matching Logic

`notifications.py:_notify_for_showtime()`:
- Zero `AlertMovie` rows on a preference → "any movie" mode; matches all films at the theater
- One or more `AlertMovie` rows → only the listed movies match; each fires independently
- The whole `AlertPreference.alert_sent` flips True only when all `AlertMovie` rows have fired
- `alert_sent` is set after the first notification *attempt* (success or failure) — intentional

### Upsert Pattern for Scraping

`BaseScraper.upsert_showtime()` checks `(theater_id, movie_id, show_datetime)`. Returns
`(showtime, True)` for new rows, `(showtime, False)` for existing (just updates
`last_checked`). Only pass `True` results to the notification system.

### Database Access Pattern

- In routes: Flask provides context automatically
- In scheduler jobs and notifications: always use `with app.app_context():`
- Never import `db` from `app` in a context where `create_app` hasn't run
- Use `db.session.get(Model, id)` (SQLAlchemy 2.0 style) — not `Model.query.get(id)`

### Config Access Pattern

- All config in `app.config.get("KEY", default)` or `current_app.config`
- Never hardcode credentials or import `config.py` directly at runtime
- All keys defined in `config.py`, sourced from environment via `python-dotenv`

---

## 7. Environment & Setup

### Local Development

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate        # macOS/Linux
pip install -r requirements.txt
python run.py
# → http://localhost:5000  (login: admin / admin)
```

### Environment Variables (`.env` file)

```env
SECRET_KEY=change-me-in-production
FLASK_ENV=development

# Database — defaults to imax_alert.db in project root
# DATABASE_URL=sqlite:////app/data/imax_alert.db

# Email
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_FROM=noreply@imaxalert.com

# SMS (Twilio)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_FROM_NUMBER=+15551234567

# Scheduler
SCRAPER_INTERVAL_MINUTES=30
ALERT_INTERVAL_MINUTES=15
VENUE_CRAWL_INTERVAL_DAYS=7
VENUE_CRAWL_ON_EMPTY=true
```

### Running Tests

```bash
pytest                          # all tests
pytest -v                       # verbose
pytest tests/test_app.py::TestClassName::test_method  # single test
```

Tests use `TestingConfig` (in-memory SQLite). No `.env` or external services needed.

**Important:** Tests that call login directly must clear `force_password_change` on the
seeded admin account:

```python
with app.app_context():
    admin = User.query.filter_by(email="admin").first()
    admin.force_password_change = False
    db.session.commit()
```

The `auth_client` fixture in `test_app.py` already does this — reuse it for tests needing
an authenticated session.

---

## 8. Agent Rules

### General

- **Always read a file before editing it.**
- **Run `pytest tests/test_app.py::TestAffectedClass -v` after changes** and fix failures before finishing. Only run the full suite when explicitly requested.
- **Use the virtual environment** for all Python commands: `venv\Scripts\python` (Windows) or `venv/bin/python` (Linux/macOS).
- **Never commit secrets.** Credentials belong in `.env` only.
- **Prefer editing existing files** over creating new ones.

### Database / Models

- **No Alembic.** Column additions go in `_run_migrations()` inside `app/__init__.py` as idempotent `ALTER TABLE` statements.
- Use `nullable=True` with a `server_default` for any new column so existing rows are not broken.
- A new model must be imported inside `create_app()` in `app/__init__.py` so `db.create_all()` picks it up.
- Never rename `uq_showtime` — it is referenced implicitly by upsert logic.
- Use `db.session.get(Model, id)` not `Model.query.get(id)` (legacy API, triggers warnings).

### Authentication

- All main_bp UI routes must have `@login_required`.
- Admin routes additionally need `@require_role("admin")` from `app/auth.py`.
- The `before_app_request` hook in `auth.py` enforces `force_password_change` by redirecting
  all requests to `/change-password` for flagged users. Tests must clear this flag to avoid
  unexpected 302 redirects to `/change-password`.

### Scraper

- Scrapers target live websites whose HTML changes without notice. If a scraper returns 0 results, check CSS selectors against the live site before assuming a code bug.
- `_parse_time_text()` handles multiple datetime formats. Add new formats there, not inline.
- Do not commit inside `scrape_theater()` — commits happen in `run_all_scrapers()`.

### Scheduler

- `use_reloader=False` in `run.py` is **required**. Flask's reloader forks the process → duplicate APScheduler instance → duplicate notifications. Do not remove it.
- The scheduler is started in `run.py` and `wsgi.py`, not inside `create_app()`. This keeps it out of test contexts.
- Gunicorn must be run with `--workers 1` — multiple workers = multiple schedulers.

### Notifications

- Notifications are fire-and-forget. Errors are logged and recorded in `Notification`, but execution continues.
- `alert_sent` / `AlertMovie.sent` is set after the first notification *attempt*, success or failure. This prevents repeated alerts on transient send errors.
- Never call `process_new_showtimes()` from a route without `app.app_context()`.

### Venue Crawler

- The fandom wiki blocks plain `requests` user-agents. `WIKI_HEADERS` in `venue_crawler.py` mimics a browser. If the crawler returns 0 venues, check if the wiki requires JS or has changed its table structure.
- Nominatim: ≤1 req/s, descriptive `User-Agent`. Never remove `GEOCODE_DELAY_SECONDS`.
- The crawler never deletes Theater rows and never deactivates theaters that disappear from the wiki. Deactivate manually if needed.
- Do not call `run_venue_crawl()` from a route without try/except — geocoding is slow.

### Configuration

- New config values must be added to the `Config` base class with `os.environ.get()` and a safe default.
- `TestingConfig` uses `sqlite:///:memory:` — never change this.
- Never access `config.py` directly at runtime — use `app.config` or `current_app.config`.

### Templates

- All templates must extend `base.html` via `{% extends "base.html" %}`.
- Leaflet.js is loaded from `app/static/vendor/` (not CDN). Do not add a second copy.
- The map on `theaters.html` reads a JSON object passed from the route. Follow that pattern for any new map features.

---

## 9. Docker & TrueNAS Scale Deployment

### Current State

All Docker infrastructure is in place and production-ready:

- **`Dockerfile`** — builds the image from `python:3.11-slim`; installs `gcc`/`libxml2`/`libxslt` for lxml; runs Gunicorn via `wsgi:app`
- **`wsgi.py`** — Gunicorn entry point; checks that `SECRET_KEY` is set in production (exits with code 1 if not); creates app; starts scheduler
- **`docker-compose.yml`** — single-service compose; binds `./data:/app/data` for SQLite persistence; binds `./.env:/app/.env:ro` for credentials; includes healthcheck
- **`.github/workflows/docker-publish.yml`** — CI/CD: runs pytest on all PRs; builds and pushes to Docker Hub on merge to `main`

### Gunicorn Configuration

```
--workers 1          One process only — prevents multiple APScheduler instances
--worker-class gthread
--threads 4          Handles concurrent requests within the single process
--timeout 120        Prevents worker kill during slow venue crawls / geocoding
--access-logfile -   Sends access logs to stdout → captured by docker logs
```

### CI/CD (GitHub Actions)

Workflow file: `.github/workflows/docker-publish.yml`

- **On PR to main:** runs `pytest tests/ -q` only
- **On push/merge to main:** runs tests first, then builds `linux/amd64` image and pushes:
  - `DOCKERHUB_USERNAME/imax-alert:latest`
  - `DOCKERHUB_USERNAME/imax-alert:sha-<short-commit>`

Required GitHub repository secrets:
- `DOCKERHUB_USERNAME` — Docker Hub username
- `DOCKERHUB_TOKEN` — Docker Hub access token (not password)

### TrueNAS Scale Deployment

Replace `build: .` with `image:` in `docker-compose.yml` for TrueNAS, update paths to
absolute dataset paths, and inject `SECRET_KEY` via the TrueNAS Custom App environment
variables UI (do not hardcode it in compose):

```yaml
services:
  imax-alert:
    image: yourdockerhubuser/imax-alert:latest
    container_name: imax-alert
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /mnt/tank/imax-alert/data:/app/data
    environment:
      - FLASK_ENV=production
      - DATABASE_URL=sqlite:////app/data/imax_alert.db
      # SECRET_KEY must be set via TrueNAS Custom App environment variables UI
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:5000/login')"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
```

Create the data directory before first start:
```bash
mkdir -p /mnt/tank/imax-alert/data
```

To update after a new image push:
```bash
docker compose pull && docker compose up -d
```

### Agent Rules Specific to Docker

- **Never store `.env` inside the image.** It is bind-mounted from the host. `.dockerignore` excludes it.
- **`DATABASE_URL` must point to the mounted path** (`sqlite:////app/data/imax_alert.db`). Without it the DB is written inside the container layer and lost on rebuild.
- **`use_reloader=False` in `run.py` must not be removed.**
- **Port mapping:** `5000:5000` is the default. Change only the host-side if there is a conflict (e.g. `8080:5000`). Update the TrueNAS Portal configuration to match.
- **Rebuilding the image** does not affect the database — it lives on the mounted dataset.
- **`SECRET_KEY` is required in production.** `wsgi.py` calls `sys.exit(1)` if it is the default development value.
