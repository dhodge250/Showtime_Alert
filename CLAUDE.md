# IMAX Alert — Project Guide

## Commands

```bash
python run.py          # run the app (use_reloader=False intentional — prevents APScheduler double-start)
pytest                 # full test suite
pytest tests/test_app.py -k "TestClassName" -v  # targeted (preferred)
pip install -r requirements.txt
```

## Branching & PR Workflow (Gitflow)

1. Cut branch from `develop`: `git checkout -b fix/issue-NNN-description origin/develop`
2. Commit, push, PR → `develop` with `--reviewer Copilot`
3. Never merge PRs — create them and let the user approve and merge
4. Never delete branches after merging
5. Run only tests for changed code; full suite only when explicitly asked

**Release cycle:**
1. `git checkout -b release/X.Y.Z origin/develop && git push -u origin release/X.Y.Z`
2. PR `release/X.Y.Z` → `main` with Copilot reviewer
3. After merge: `git checkout main && git pull origin main && git tag vX.Y.Z && git push origin vX.Y.Z`
4. Tag push triggers Docker Hub CI/CD

## Local Testing

```bash
docker compose down && docker compose build --no-cache && docker compose up -d
docker logs imax-alert -f
```

## Architecture

**App factory:** `create_app(config_name)` in `app/__init__.py`. On startup: runs `_run_migrations()` (idempotent ALTER TABLEs — no Alembic), seeds lookup tables/admin/theaters, loads notification credentials from DB into `app.config`.

**Blueprints:** `auth_bp` (`/login`, `/logout`), `main_bp` (UI, `@login_required`), `api_bp` (`/api`, JSON). `require_role(*roles)` combines login + role check.

**Database:** SQLAlchemy + SQLite/PostgreSQL. Schema changes go in `_run_migrations()` in `__init__.py`, not model-only. `Theater` has legacy string columns and newer FK columns to lookup tables — property helpers fall back to strings during migration.

**Alert model:** `AlertPreference` → `AlertMovie` (join table). Zero `AlertMovie` rows = "any movie." `_migrate_legacy_alert_movies()` back-fills on startup.

**Scheduler:** APScheduler `BackgroundScheduler` — scraper every 30 min, venue crawl every 7 days, cleanup every 24 hrs. Demand-driven: only scrapes theaters/movies with active unsent alerts.

**Seeding:** `_seed_theaters_from_csv()` loads `seeds/imax_theaters.csv` on first boot.

**Notifications:** `app/notifications.py` — SMTP email + Twilio SMS. Credentials from `Settings` DB table (not `.env` after startup).

**TMDB:** Optional enrichment via `app/tmdb.py`. Key stored in `Settings`. No-ops when absent.

**Default credentials:** email `admin`, password `admin`.

## Scraper Patterns

**Cloudflare-protected (AMC, Regal):** Playwright headless Chromium for initial page load only. After CF challenge clears, extract cookies via `context.cookies()`, seed a `requests.Session` with them, and make all API calls via `requests` — never `page.evaluate(fetch(...))`, which CF blocks inside Docker.

**Plain HTTP (Cinemark, Cineplex):** `requests` + `BeautifulSoup` directly. No Playwright.

**Rules:** No `_MAX_DAYS` cap. `upsert_showtime()` deduplicates. `_movie_wanted()` scopes movies per alert.

**Tests:** HTML/JSON fixtures, test module-level helpers directly, cover IMAX extraction + non-IMAX exclusion + past showtime exclusion + deduplication.

## Release Roadmap

| Version | Issue | Scraper |
|---------|-------|---------|
| v1.13.1 | #130 | AMC — Playwright + GraphQL API |
| v1.13.2 | #131 | Regal — Playwright CF handshake + requests.Session |
| v1.13.3 | #132 | Cinemark — requests + BeautifulSoup + GetByTheaterId API |
| v1.13.4 | #133 | TCL Chinese Theatre — next |
| v1.14   | #22 #24 #73 #80 | Account Security |
