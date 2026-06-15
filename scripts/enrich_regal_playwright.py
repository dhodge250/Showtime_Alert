#!/usr/bin/env python3
"""
One-time script: fill missing Website values for Regal theaters in
seeds/imax_theaters.csv by scraping regmovies.com directly via Playwright.

Strategy (tries each in order until one works):
  1. Load regmovies.com/sitemap.xml through the browser — bypasses the 403
     that urllib gets, parses all /theatres/ slugs in one request.
  2. Load regmovies.com/theatres and wait for the theater cards to render,
     then extract anchor hrefs from the rendered DOM.

Matched URLs are fuzzy-matched to CSV rows by theater name using the same
normaliser as enrich_csv_websites.py.

Usage:
    python scripts/enrich_regal_playwright.py             # live run
    python scripts/enrich_regal_playwright.py --dry-run   # preview only
"""

import argparse
import csv
import difflib
import re
import sys
import urllib.parse
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "seeds" / "imax_theaters.csv"
BASE_URL  = "https://www.regmovies.com"

_NAME_NOISE = re.compile(
    r"\b(imax|imax[\s\-]?laser|laser|dine[\s\-]?in|& imax|"
    r"\d{1,3}|theatres?|theaters?|cinemas?|multiplex|"
    r"amc dine-in|amc dine in)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]")


def _normalize(name: str) -> str:
    name = name.lower()
    name = _NAME_NOISE.sub(" ", name)
    name = _PUNCT.sub(" ", name)
    return " ".join(name.split())


def _name_score(a: str, b: str) -> float:
    n1, n2 = _normalize(a), _normalize(b)
    if not n1 or not n2:
        return 0.0
    if n1 in n2 or n2 in n1:
        shorter = min(len(n1.split()), len(n2.split()))
        longer  = max(len(n1.split()), len(n2.split()))
        return 0.5 + 0.4 * (shorter / longer)
    return difflib.SequenceMatcher(None, n1, n2).ratio()


def _fetch_via_browser(url: str, wait_selector: str | None = None,
                       wait_ms: int = 5000) -> str:
    """Load a URL in headless Chromium and return the page content."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:
                pass
        else:
            page.wait_for_timeout(wait_ms)
        content = page.content()
        browser.close()
    return content


def fetch_from_sitemap() -> dict[str, str]:
    """
    Load regmovies.com/sitemap.xml through Playwright and extract
    all /theatres/ URLs.  Returns {slug: full_url}.
    """
    print("Trying sitemap.xml via browser...")
    try:
        html = _fetch_via_browser(f"{BASE_URL}/sitemap.xml", wait_ms=3000)
    except Exception as e:
        print(f"  sitemap fetch failed: {e}")
        return {}

    urls = re.findall(
        r"https://www\.regmovies\.com/theatres/([a-z0-9\-]+)",
        html,
    )
    result = {}
    for slug in urls:
        full = f"{BASE_URL}/theatres/{slug}"
        result[slug] = full

    print(f"  Found {len(result)} theater URLs in sitemap")
    return result


def fetch_from_theaters_page() -> dict[str, str]:
    """
    Load regmovies.com/theatres, wait for theater anchor tags to render,
    and extract all /theatres/ href values.  Returns {slug: full_url}.
    """
    print("Trying /theatres page via browser...")
    try:
        html = _fetch_via_browser(
            f"{BASE_URL}/theatres",
            wait_selector="a[href*='/theatres/']",
            wait_ms=8000,
        )
    except Exception as e:
        print(f"  /theatres page fetch failed: {e}")
        return {}

    slugs = re.findall(
        r"/theatres/([a-z0-9\-]+)",
        html,
    )
    result = {}
    for slug in slugs:
        # Filter out non-theater slugs (like /theatres/search, /theatres/all)
        if re.search(r"\d{3,4}$", slug):  # real theater slugs end in a numeric ID
            full = f"{BASE_URL}/theatres/{slug}"
            result[slug] = full

    print(f"  Found {len(result)} theater URLs on /theatres page")
    return result


def slug_to_name(slug: str) -> str:
    """Convert a regmovies slug like 'regal-edwards-fresno-1033' to a plain name."""
    # Strip trailing numeric ID
    name = re.sub(r"-\d{3,4}$", "", slug)
    # Strip leading 'regal-'
    name = re.sub(r"^regal-", "", name)
    return name.replace("-", " ").title()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print matches without writing the CSV")
    parser.add_argument("--threshold", type=float, default=0.45,
                        help="Minimum fuzzy-match score (default 0.45)")
    args = parser.parse_args()

    # --- Load CSV ---
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader     = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows       = list(reader)

    missing_regal = [
        r for r in rows
        if r.get("Chain", "").strip() == "Regal"
        and not r.get("Website", "").strip()
    ]
    print(f"Missing Regal websites: {len(missing_regal)}")
    print()

    # --- Fetch theater catalog from regmovies.com ---
    catalog = fetch_from_sitemap()
    if not catalog:
        catalog = fetch_from_theaters_page()
    if not catalog:
        print("Could not retrieve theater catalog from regmovies.com — aborting.")
        sys.exit(1)

    print()

    # --- Fuzzy-match each missing row against the catalog ---
    updated = 0
    no_match = 0

    for row in missing_regal:
        csv_name = row.get("Location Name", "").strip()
        csv_city = row.get("City", "").strip()

        best_score = 0.0
        best_url   = ""
        best_slug  = ""

        for slug, url in catalog.items():
            site_name = slug_to_name(slug)
            score = _name_score(csv_name, site_name)

            # Small city bonus: if city appears in the slug
            city_slug = re.sub(r"[^a-z0-9]+", "-", csv_city.lower()).strip("-")
            if city_slug[:8] in slug:
                score += 0.05

            if score > best_score:
                best_score = score
                best_url   = url
                best_slug  = slug

        if best_score >= args.threshold and best_url:
            print(f"  MATCH ({best_score:.2f})  {csv_name}")
            print(f"         -> {best_url}  [{best_slug}]")
            if not args.dry_run:
                row["Website"] = best_url
            updated += 1
        else:
            top = f"{best_slug} ({best_score:.2f})" if best_slug else "none"
            print(f"  no match  {csv_name}  (best: {top})")
            no_match += 1

    print()
    print(f"{'DRY RUN ' if args.dry_run else ''}Results:")
    print(f"  Matched  : {updated}")
    print(f"  No match : {no_match}")

    if args.dry_run:
        print("\nDry run — CSV not modified.")
        return

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV written: {CSV_PATH}")


if __name__ == "__main__":
    main()
