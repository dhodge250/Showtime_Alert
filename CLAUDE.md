# IMAX Alert — Project Guide

## Commands

```bash
python run.py          # run the app (use_reloader=False intentional — prevents APScheduler double-start)
pytest                 # full test suite
pytest tests/test_app.py -k "TestClassName" -v  # targeted (preferred)
pip install -r requirements.txt
```

## Branching & PR Workflow (Gitflow)

This project follows the Gitflow branching model: https://nvie.com/posts/a-successful-git-branching-model/

**Repo:** `dhodge250/IMAX_Alert` — container: `imax-alert`

### Permanent Branches

| Branch | Role |
|--------|------|
| `main` | Production-ready code only. Every commit represents a deployed release. Tag pushes trigger Docker Hub CI/CD. |
| `develop` | Integration branch. All feature and fix work targets here first. Represents the latest state of the next release. |

### Supporting Branch Types

#### Feature / Fix Branches
- **Naming:** `feature/short-description` or `fix/issue-NNN-short-description`
- **Branch from:** `develop`
- **PR target:** `develop`
- **Purpose:** New features and bug fixes. Never branch from or PR directly to `main`.
- **Examples:** `feature/v1.21-theater-detail`, `fix/issue-227-profile-map-width`

#### Release Branches
- **Naming:** `release/X.Y.Z`
- **Branch from:** `develop`
- **PR target:** `main` (then tag; `develop` picks up the tag via the merge history)
- **Purpose:** Stabilise a release — only minor last-minute fixes, no new features.
- **Examples:** `release/1.21.0`, `release/1.20.0`

#### Hotfix Branches
- **Naming:** `hotfix/X.Y.Z`
- **Branch from:** `main`
- **PR target:** `main` **and** a separate PR to `develop` (to keep branches in sync)
- **Purpose:** Urgent production fixes that cannot wait for the next release cycle.
- **Examples:** `hotfix/1.20.1`, `hotfix/1.19.1`

### General Rules

1. Never merge PRs — create them and let the user approve and merge
2. Never delete branches after merging
3. Never commit or push directly to `main` or `develop` — all changes go through PRs
4. **Feature and fix branches MUST target `develop`, never `main`.** Only `release/*` and `hotfix/*` branches may target `main`. If a PR is ever created with the wrong base, correct it before the user merges.
5. After creating the PR, rebuild and restart the local container so the user can test immediately: `docker compose down && docker compose build --no-cache && docker compose up -d`
6. Run only tests for changed code; full suite only when explicitly asked

### Release Cycle

```
git checkout -b release/X.Y.Z origin/develop
git push -u origin release/X.Y.Z
# PR release/X.Y.Z → main; user merges
git checkout main && git pull origin main
git tag vX.Y.Z && git push origin vX.Y.Z   # triggers Docker Hub CI/CD
```

### Hotfix Cycle

```
git checkout -b hotfix/X.Y.Z origin/main
git push -u origin hotfix/X.Y.Z
# PR hotfix/X.Y.Z → main; user merges
# PR hotfix/X.Y.Z → develop; user merges
git checkout main && git pull origin main
git tag vX.Y.Z && git push origin vX.Y.Z   # triggers Docker Hub CI/CD
```

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

**`_get_active_targets()`** returns `{theater_id: set[movie_id]}`. `theater_id=None` = "any theater" alert (applies to all theaters of that chain). `movie_id=None` = "any movie" sentinel. Scrapers merge both sets: `movie_ids = targets.get(None, set()) | targets.get(theater.id, set())`.

**Test fixtures** (session-scoped for performance): `app`, `client`, `auth_client` (logged-in admin), `sample_user`, `sample_theater` (returns ID), `sample_movie` (returns ID). Use `Theater.query.get(sample_theater)` inside `app.app_context()` to get the ORM object.

**Scheduler:** APScheduler `BackgroundScheduler` — scraper every 30 min, venue crawl every 7 days, cleanup every 24 hrs. Demand-driven: only scrapes theaters/movies with active unsent alerts.

**Seeding:** `_seed_theaters_from_csv()` loads `seeds/imax_theaters.csv` on first boot.

**Notifications:** `app/notifications.py` — SMTP email + Twilio SMS. Credentials from `Settings` DB table (not `.env` after startup).

**TMDB:** Optional enrichment via `app/tmdb.py`. Key stored in `Settings`. No-ops when absent.

**Default credentials:** email `admin`, password `admin`.

## Scraper Patterns

**Cloudflare-protected (AMC, Regal):** Playwright headless Chromium for initial page load only. After CF challenge clears, extract cookies via `context.cookies()`, seed a `requests.Session` with them, and make all API calls via `requests` — never `page.evaluate(fetch(...))`, which CF blocks inside Docker.

**Plain HTTP (Cinemark, Cineplex, Royal BC Museum):** `requests` + `BeautifulSoup` directly. No Playwright.

**Playwright + plain requests (TCL):** Playwright fetches the homepage to bypass Cloudflare and extract the `gasToken` from `__NEXT_DATA__`; browser is closed immediately after. All subsequent OCAPI calls use plain `requests` with the token as a Bearer header.

**`scrape_all()` override:** Playwright scrapers (AMC, Regal) subclass `PlaywrightBatchScraper` (`app/scrapers/base.py`) to launch one browser and share it across all theaters. Plain HTTP scrapers rely on the base class `scrape_all()`, which calls `scrape_theater()` per theater. When writing a new Playwright scraper, subclass `PlaywrightBatchScraper` and implement `_scrape_with_context`.

**Adding a new scraper:** Create `app/scrapers/chain.py`, then register it in `ALL_SCRAPERS` in `app/scrapers/__init__.py`. Without the registration step the scraper is never called.

**Known date volumes:** Regal ~96 dates, Cinemark ~74 dates. Expect multi-second scrape times per theater even with fast HTTP — this is normal.

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
| v1.13.4 | TCL Chinese Theatre scraper rebuild: Playwright gasToken + Vista OCAPI (#133) |
| v1.14.0 | Account Security: password strength/reuse, forgot-password flow, rate limiting, session timeout (#22 #24 #73 #80) |
| v1.15.0 | Frontend Design & UX: IMAX visual identity, light/dark toggle, button hover reactions, Profile/Settings layout (#182–#191 #194) |
| v1.16.0 | Admin & User Management: MFA/TOTP, user invites, admin user list/edit, dashboard showtime scoping, theater map viewport filter, security fixes (#23 #25 #106 #119) |
| v1.17.0 | Movies Feature: Movies tab, movie detail page, radius-based alerts, UTC showtime storage + browser-local display, notification reliability fixes (#29 #30) |
| v1.18.0 | UX Polish & Mobile Fixes: brand link, theme toggle relocation, segmented theater targeting, target date hint, movie card overflow fix, Clear Showtimes in Actions menu (#205 #206 #207 #208 #209 #210) |
| v1.19.0 | Theater Data Infrastructure: CSV export (download/email/save), bulk import with validation, Actions menu split into Showtimes/Venue Data/CSV & Data with inline descriptions, 10 remaining website URLs populated (#46 #83) |
| v1.20.0 | Settings & Navigation Redesign: unified Settings nav, left-rail sidebar (desktop) + page-picker overlay (mobile), Profile Info/Settings split with accordions, App Settings accordion groups, segmented unit buttons, last_login_at tracking, XSS + datetime fixes (#211) |
| v1.20.1 | Hotfix: restore base.html/CSS/template changes lost in release merge conflict; remove dead profile.html |
| v1.21.0 | Theater detail page, scraper status/on-demand scraping, movie TMDB matching improvements, movies filter/sort/pagination, configurable page sizes (#225 #226 #227) |
| v1.21.1 | Hotfix: apply v1.21 code missing from main due to release branch merge base collision |
| v1.22.0 | Scheduled Proximity Browsing: unified scraper coordinator (cooldown, in-flight, concurrency caps), browse schedule model + settings UI + Run Now, format type normalization across all scrapers, Regal rate-limit + date-range fixes, multi-user scrape isolation (#243 #242 #245) |

### In Progress & Upcoming

| Version | Milestone | Status | Issues |
|---------|-----------|--------|--------|
| v2.0 | Full North American Scraper Coverage | 🔄 next | #84–#92 #134–#150 |
| v2.x | Global Expansion | ⬜ | #151 |
| v3.0 | Expand to Non-IMAX Theaters | ⬜ | #200 #251 |

