# IMAX Alert — Project Guide

## Branching & PR Workflow (Gitflow)

1. Cut feature/fix branch from `develop`: `git checkout -b fix/issue-NNN-description origin/develop`
2. Do work, commit, push
3. PR → `develop` (never `main`), always add `--reviewer Copilot`
4. Never merge PRs — create them and let the user approve and merge
5. Never delete branches after merging

**Release cycle (after all fixes for a version are merged to develop):**
1. `git checkout -b release/X.Y.Z origin/develop && git push -u origin release/X.Y.Z`
2. PR `release/X.Y.Z` → `main`, add Copilot reviewer
3. After user merges: `git checkout main && git pull origin main && git tag vX.Y.Z && git push origin vX.Y.Z`
4. Tag push triggers Docker Hub CI/CD automatically

## Local Testing

- Rebuild container after code changes: `docker compose down && docker compose build --no-cache && docker compose up -d`
- Watch logs live: `docker logs imax-alert -f`
- Run targeted tests only (not full suite unless explicitly asked): `python -m pytest tests/test_app.py -k "TestClassName" -v`

## Scraper Patterns

**Cloudflare-protected sites (AMC, Regal):** Use Playwright headless Chromium to load the page and clear the CF challenge. Extract `cf_clearance` cookies via `context.cookies()` after the page loads, then use a `requests.Session` seeded with those cookies for all subsequent API calls. Never make API calls from within the browser via `page.evaluate(fetch(...))` — CF blocks these inside Docker.

**Plain HTTP sites (Cinemark, Cineplex):** Use `requests` + `BeautifulSoup` directly. No Playwright needed.

**General rules:**
- No `_MAX_DAYS` cap — scrape all available dates
- IMAX detection is chain-specific (check `PerformanceAttributes` for Regal, `data-print-type-name` for Cinemark, etc.)
- `upsert_showtime()` handles deduplication; `_movie_wanted()` scopes movies per alert

## Scraper Release Versions

- v1.13.1 — AMC (#130): Playwright + AMC GraphQL API
- v1.13.2 — Regal (#131): Playwright page load + requests.Session for CF cookie reuse
- v1.13.3 — Cinemark (#132): requests + BeautifulSoup + GetByTheaterId API
- v1.13.4 — TCL Chinese Theatre (#133): next
- v1.14 — Account Security (#22 forgot password, #24 password complexity, #73 session timeout, #80 CVE tracking)

## Code Conventions

- No comments explaining what code does — only add a comment when the WHY is non-obvious
- No `_MAX_DAYS` or artificial date caps on scrapers
- No error handling for scenarios that can't happen
- Tests: use HTML/JSON fixtures, test module-level helpers directly (not the full scraper class), cover IMAX extraction, non-IMAX exclusion, past showtime exclusion, and deduplication
