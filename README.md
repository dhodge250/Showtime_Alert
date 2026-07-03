# IMAX Alert

A web application that monitors IMAX theater websites for new showtimes and ticket availability, then notifies users via email and/or SMS when tickets go on sale for movies they want to see.

## Features

- **Automated Scraping** — Monitors AMC Theatres, Regal Cinemas, Cinemark, and TCL Chinese Theatre on a configurable schedule (default: every 30 minutes)
- **Email & SMS Notifications** — Sends alerts via SMTP email (Gmail-compatible) and Twilio SMS
- **Alert Preferences** — Watch for a specific movie, a specific theater, or any combination; supports multiple movies per alert and an optional notification cap
- **Web Dashboard** — View showtimes, manage alerts, and trigger manual scrapes
- **Interactive Theater Map** — Browse monitored theaters on an OpenStreetMap-powered map
- **Admin UI** — Manage users, theaters, notification settings, and all lookup tables through a browser interface
- **TMDB Integration** — Optional movie metadata enrichment (poster art, genre, runtime) via The Movie Database API
- **REST API** — Full JSON API for all data and scheduler controls
- **CSV-Seeded Theaters** — IMAX theaters are loaded from `seeds/imax_theaters.csv` on first launch

## Tech Stack

| Category | Technology |
|---|---|
| Language | Python 3.10+ |
| Web Framework | Flask 3.1.1 |
| WSGI Server | Gunicorn (production) |
| Authentication | Flask-Login + Werkzeug password hashing |
| Database | SQLite (default) via SQLAlchemy |
| Scheduling | APScheduler |
| Web Scraping | requests + BeautifulSoup4 + lxml |
| SMS | Twilio |
| Frontend Maps | Leaflet.js + OpenStreetMap |
| Rate Limiting | Flask-Limiter |
| Testing | pytest + pytest-flask |

## Default Credentials

On first launch the app seeds a default admin account:

| Field | Value |
|---|---|
| Email | `admin` |
| Password | `admin` |

**Change the password immediately after first login.** The app will prompt you to do so.

## Setup

### Prerequisites

- Python 3.10 or higher
- pip

### Installation

```bash
# 1. Clone the repository
git clone <repo-url>
cd IMAX_Alert

# 2. Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Create a .env file for notification credentials — see Configuration

# 5. Run the application
python run.py
```

The app starts at `http://localhost:5000`. On first launch the database is created, lookup tables are seeded, and theaters are loaded from `seeds/imax_theaters.csv` automatically.

### Running Tests

```bash
# Full suite
pytest

# Single test
pytest tests/test_app.py::TestClassName::test_method_name -v
```

Tests use an in-memory SQLite database and require no `.env` file or external services.

## Docker

### Local Docker

```bash
docker compose up --build
```

App available at `http://localhost:5000`. The `./data` directory is bind-mounted for SQLite persistence.

### Production / TrueNAS Scale

The production image runs under Gunicorn (1 worker, 4 threads) to keep APScheduler to a single instance.

**Option A — build locally and run:**
```bash
docker build -t imax-alert:latest .
docker compose up -d
```

**Option B — Docker Hub via GitHub Actions:**

Every push to `main` triggers the CI/CD pipeline: tests run first, then the image is built and pushed as `yourdockerhubuser/imax-alert:latest`. Set two repository secrets in GitHub:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | A Docker Hub access token (Account Settings → Security) |

To update a running TrueNAS deployment after a new image is pushed:
```bash
docker compose pull && docker compose up -d
```

**Required environment variables for production:**

| Variable | Description |
|---|---|
| `SECRET_KEY` | Random 32-byte hex string — required; the container exits if this is unset |
| `FLASK_ENV` | Set to `production` |
| `DATABASE_URL` | `sqlite:////app/data/imax_alert.db` (four slashes = absolute path) |

Generate a secret key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Notification credentials (SMTP, Twilio) are configured through the Admin → Settings page and stored in the database — no environment variables needed after initial deployment.

## Configuration

Create a `.env` file in the project root. All values are optional — the app runs without them, but notifications will be skipped until credentials are set via Admin → Settings.

```env
# Flask
SECRET_KEY=your-secret-key-here
FLASK_ENV=development  # or production

# Database (defaults to SQLite in project root)
# DATABASE_URL=sqlite:////app/data/imax_alert.db

# Email notifications (Gmail example)
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_FROM=noreply@imaxalert.com

# SMS notifications (Twilio)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_FROM_NUMBER=+15551234567

# Scheduler intervals
SCRAPER_INTERVAL_MINUTES=30
ALERT_INTERVAL_MINUTES=15
```

### Gmail Setup

Enable 2-factor authentication on your Google account and generate an [App Password](https://support.google.com/accounts/answer/185833). Use the app password (not your account password) as `MAIL_PASSWORD`.

### TMDB Setup

To enable movie poster art and metadata, create a free account at [themoviedb.org](https://www.themoviedb.org/), generate an API key, and enter it in **Admin → Settings → TMDB API Key**.

## API Reference

All endpoints return JSON. Endpoints under `/api/admin/*` require admin role.

### Core

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/theaters` | List all active theaters |
| GET | `/api/theaters/<id>` | Get theater details |
| PATCH | `/api/theaters/<id>` | Update theater fields |
| GET | `/api/movies` | List all movies |
| GET | `/api/movies/search` | Search movies (TMDB or local) |
| GET | `/api/showtimes` | Future showtimes (filter: `theater_id`, `movie_id`) |
| GET | `/api/showtimes/count` | Showtime counts by filter |
| DELETE | `/api/showtimes` | Clear showtimes (filter: `theater_id`, `movie_id`, `before`) |

### Users & Alerts

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/users` | List all users |
| POST | `/api/users` | Create a user |
| PUT | `/api/users/<id>` | Update a user |
| GET | `/api/alerts` | List alert preferences |
| POST | `/api/alerts` | Create an alert |
| GET | `/api/alerts/<id>` | Get alert detail |
| DELETE | `/api/alerts/<id>` | Deactivate an alert (soft delete) |
| PATCH | `/api/alerts/<id>/reset` | Re-arm a sent alert |
| GET | `/api/notifications` | Last 50 sent notifications |

### Scheduler

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/scheduler/status` | Scheduler state + next run times |
| POST | `/api/scheduler/trigger` | Manually trigger a showtime scrape |

### Geocoding

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/geocode` | Geocode a single address via Nominatim |
| POST | `/api/geocode/bulk/trigger` | Geocode all un-geocoded theaters |
| GET | `/api/geocode/bulk/status` | Status of a running bulk geocode job |

## Project Structure

```
IMAX_Alert/
├── app/
│   ├── __init__.py          App factory, DB init, migrations, seeding
│   ├── auth.py              Login/logout/change-password routes + role decorator
│   ├── models.py            SQLAlchemy ORM models (Theater, User, Alert, Lookup tables…)
│   ├── routes.py            UI routes (main_bp) + REST API (api_bp)
│   ├── scraper.py           Web scraper classes per theater chain
│   ├── scheduler.py         APScheduler background jobs (scraper, crawler, cleanup)
│   ├── notifications.py     Email (SMTP) and SMS (Twilio) logic
│   ├── venue_crawler.py     IMAX fandom wiki crawler + Nominatim geocoder
│   ├── tmdb.py              TMDB API integration (optional)
│   ├── lookup_helpers.py    get_or_create_* helpers for FK lookup tables
│   ├── templates/           Jinja2 HTML templates (UI + admin pages)
│   └── static/              CSS + vendored frontend libraries (Leaflet.js)
├── seeds/
│   └── imax_theaters.csv    Theater seed data loaded on first boot
├── tests/
│   └── test_app.py          pytest suite (152+ tests)
├── config.py                Dev/Prod/Testing configuration classes
├── run.py                   Local development entry point
├── wsgi.py                  Gunicorn entry point (production)
├── Dockerfile               Container image definition
├── docker-compose.yml       Local Docker + TrueNAS deployment
└── requirements.txt         Pinned Python dependencies
```

## Notes

- **Scraper fragility** — Scrapers target specific HTML structures on theater websites. Updates to those sites may require scraper adjustments.
- **SQLite in production** — SQLite is appropriate for this app's write volume (a scrape every 30 min, occasional alert fires). WAL mode is enabled. The `data/` bind-mount ensures the database survives container rebuilds.
- **Maps** — The theater map uses Leaflet.js with free OpenStreetMap tiles; no API key is required.
- **Single-worker deployment** — Gunicorn is configured for 1 worker + 4 threads. Multiple workers would spawn multiple APScheduler instances, causing duplicate notifications.

## License

See [LICENSE](LICENSE) for details.
