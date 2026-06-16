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
2. Commit, push, PR → `develop`
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

**Plain HTTP (Cinemark, Cineplex, Royal BC Museum, TCL):** `requests` + `BeautifulSoup` directly. No Playwright.

**Rules:** No `_MAX_DAYS` cap. `upsert_showtime()` deduplicates. `_movie_wanted()` scopes movies per alert.

**Tests:** HTML/JSON fixtures, test module-level helpers directly, cover IMAX extraction + non-IMAX exclusion + past showtime exclusion + deduplication.

## Release History & Roadmap

> **Maintainer note:** After every release is tagged and deployed, move the version row from "In Progress & Upcoming" into the "Shipped" table and fill in its summary. Keep the "next" label on whichever issue is actively being worked.

### Shipped

| Version | Summary |
|---------|---------|
| v1.0.0 | Initial release |
| v1.0.1 | Hotfix: Docker multi-stage build missing transitive dependencies |
| v1.1.0 | Bug fixes: login card centering, dashboard card heights, profile location (#3 #4 #6) |
| v1.2.0 | Dashboard: multi-select filters, pagination, alert filters (#7 #8 #9 #10) |
| v1.3.0 | Notification limit, alert detail improvements, cache-busting, Cineplex website fix (#11) |
| v1.4.0 | Admin settings layout, alert management sort/pagination, theater map/filter fixes (#14 #15 #16 #17 #53) |
| v1.5.0 | Alert detail pagination, login screen polish, timezone display fix (#18 #19 #20 #31) |
| v1.6.0 | Structured app-wide logging + log viewer, Trivy CVE scanning in CI (#26 #27 #28 #34) |
| v1.6.1 | Add scheduled job entries to in-app log viewer |
| v1.7.0 | Security: fix perl/pip/wheel CVEs, mobile CSRF expiry |
| v1.8.0 | Mobile responsive layout: hamburger nav, padding, touch targets (#74) |
| v1.8.1 | Mobile layout follow-up: nav spacing, subnav scroll, table cells |
| v1.9.0 | Scraper package refactor (#82), Chain field populated for all 1,927 theaters (#100), 113 website URLs added (#83 partial) |
| v1.10.0 | Geocoding accuracy fix, geocode task logging (#107 #108) |
| v1.11.0 | Multi-notification deduplication fix, ~25x test speedup via parallel execution (#115) |
| v1.11.1 | Admin UI: actions dropdown divider, re-seed modal checkbox layout |
| v1.12.0 | Bug Fixes & Mobile Responsiveness milestone (#117 #118 #120 #126 #127 #128 #129) |
| v1.12.1 | Activity Log timestamps display in user's local timezone (#161) |
| v1.12.2 | Alert target date filter (#164), Cineplex scraper fix, log timezone corrections |
| v1.13.0 | CSV theater data: second website pass, chain normalization, chain root URL population |
| v1.13.1 | AMC scraper rebuild: Playwright + GraphQL API (#130) |
| v1.13.2 | Regal scraper rebuild: Playwright CF handshake + requests.Session (#131) |
| v1.13.3 | Cinemark scraper rebuild: requests + BeautifulSoup + GetByTheaterId API (#132) |

### In Progress & Upcoming

| Version | Milestone | Status | Issues |
|---------|-----------|--------|--------|
| v1.13.4 | Scraper Reliability | 🔄 next | #133 TCL Chinese Theatre |
| v1.14 | Account Security | ⬜ | #22 #24 #73 #80 |
| v1.15 | Admin & User Management | ⬜ | #23 #25 #106 #119 |
| v1.16 | Movies Feature | ⬜ | #29 #30 |
| v1.17 | Theater Data Infrastructure | ⬜ | #46 #83 |
| v2.0 | Full North American Scraper Coverage | ⬜ | #84–#92 #134–#150 |
| v2.x | Global Expansion | ⬜ | #151 |
