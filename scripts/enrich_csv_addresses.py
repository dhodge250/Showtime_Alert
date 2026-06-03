#!/usr/bin/env python3
"""
One-time script: enrich seeds/imax_theaters.csv with street addresses and
postal codes.

Uses four sources in priority order:
  1. OSM Overpass API — fetches all cinemas in the theater's city, then
     fuzzy-matches the CSV name against OSM names.  Much more reliable than
     a direct Nominatim name search because it tolerates slight name
     differences (e.g. "AMC Deer Valley 30 & IMAX" matching "AMC Deer
     Valley 17") and doesn't require an exact OSM name match.
  2. Mapdoor.com — US chain theaters only; constructs a direct URL from the
     theater's Website column and parses the embedded address JSON.
  3. DuckDuckGo local search (requires playwright + chromium) — opens a
     real Chromium browser, intercepts the local.js JSONP response (Apple
     Maps data), and fuzzy-matches the returned places against the CSV name.
     Enable with --use-browser.  Falls back gracefully if playwright is not
     installed.
  4. Nominatim direct search — original OSM fallback.

A "Postal Code" column is added after "Address" if not already present.

Usage:
    python scripts/enrich_csv_addresses.py                        # live run
    python scripts/enrich_csv_addresses.py --dry-run              # preview
    python scripts/enrich_csv_addresses.py --dry-run --max 20
    python scripts/enrich_csv_addresses.py --dry-run --sample 40  # random
    python scripts/enrich_csv_addresses.py --dry-run --use-browser
    python scripts/enrich_csv_addresses.py --dry-run --country "United States"
"""

import argparse
import csv
import difflib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "seeds" / "imax_theaters.csv"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

NOMINATIM_DELAY = 1.1   # Nominatim ToS: max 1 req/sec
OVERPASS_DELAY = 2.0    # polite delay between Overpass requests
MAPDOOR_DELAY = 1.5     # polite delay between mapdoor.com requests

USER_AGENT = "IMAX_Alert_CSV_Enrichment/1.0 (github.com/dhodge250/IMAX_Alert)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# US state abbreviation → 2-letter code (used to build mapdoor.com URLs)
_US_STATE_CODES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}

_COARSE_OSM_TYPES = frozenset({
    "city", "town", "village", "suburb", "county", "state", "country",
    "administrative", "municipality", "district", "region", "province",
    "borough", "quarter", "neighbourhood",
})

# Tokens stripped before fuzzy name comparison (Overpass / Nominatim)
_NAME_NOISE = re.compile(
    r"\b(imax|imax[\s\-]?laser|laser|dine[\s\-]?in|& imax|"
    r"\d{1,3}|theatres?|theaters?|cinemas?|multiplex|"
    r"amc dine-in|amc dine in)\b",
    re.IGNORECASE,
)
# Same, but keeps "imax" — used when matching DDG/Apple Maps results where the
# place name often includes "IMAX" and we want that to contribute to the score.
_NAME_NOISE_NO_IMAX = re.compile(
    r"\b(imax[\s\-]?laser|laser|dine[\s\-]?in|"
    r"\d{1,3}|theatres?|theaters?|cinemas?|multiplex|"
    r"amc dine-in|amc dine in)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]")


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

def _normalize_name(name: str, keep_imax: bool = False) -> str:
    name = name.lower()
    noise = _NAME_NOISE_NO_IMAX if keep_imax else _NAME_NOISE
    name = noise.sub(" ", name)
    name = _PUNCT.sub(" ", name)
    return " ".join(name.split())


def _name_score(csv_name: str, osm_name: str, keep_imax: bool = False) -> float:
    n1 = _normalize_name(csv_name, keep_imax=keep_imax)
    n2 = _normalize_name(osm_name, keep_imax=keep_imax)
    if not n1 or not n2:
        return 0.0
    tokens1 = n1.split()
    tokens2 = n2.split()
    # Containment check scaled by coverage so that a short OSM name like "AMC"
    # matching a long CSV name like "AMC Deer Valley" scores low (~0.63), while
    # a near-full match like "AMC Deer Valley" in "AMC Deer Valley 17" scores
    # high (~0.90).
    if n1 in n2 or n2 in n1:
        shorter = min(len(tokens1), len(tokens2))
        longer = max(len(tokens1), len(tokens2))
        return 0.5 + 0.4 * (shorter / longer)
    return difflib.SequenceMatcher(None, n1, n2).ratio()


# ---------------------------------------------------------------------------
# Overpass helpers
# ---------------------------------------------------------------------------

def _nominatim_city_center(city: str, state: str, country: str) -> tuple[float, float] | None:
    """Return (lat, lon) for the city centre via Nominatim."""
    country_lower = country.strip().lower()
    is_us = country_lower in {"united states", "usa", "us"}
    if is_us:
        q = f"{city}, {state}, USA" if state else f"{city}, USA"
    else:
        q = f"{city}, {state}, {country}" if state else f"{city}, {country}"

    params = urllib.parse.urlencode({
        "q": q,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def _overpass_cinemas(lat: float, lon: float, radius_m: int = 35_000) -> list[dict]:
    """Return all OSM cinema nodes/ways within radius_m metres of (lat, lon)."""
    query = (
        f"[out:json][timeout:30];\n"
        f"(\n"
        f'  node["amenity"="cinema"](around:{radius_m},{lat},{lon});\n'
        f'  way["amenity"="cinema"](around:{radius_m},{lat},{lon});\n'
        f");\n"
        f"out body;\n"
    )
    req = urllib.request.Request(
        OVERPASS_URL,
        data=query.encode(),
        headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode())
            return data.get("elements", [])
    except Exception:
        return []


def _best_overpass_match(
    csv_name: str, elements: list[dict], threshold: float = 0.55
) -> tuple[str, str] | None:
    """Find the best-matching cinema element and return (street, postcode)."""
    best_score = 0.0
    best_tags = None

    for el in elements:
        tags = el.get("tags", {})
        osm_name = tags.get("name", "")
        if not osm_name:
            continue
        score = _name_score(csv_name, osm_name)
        if score > best_score:
            best_score = score
            best_tags = tags

    if best_score < threshold or best_tags is None:
        return None

    house_no = best_tags.get("addr:housenumber", "").strip()
    road = best_tags.get("addr:street", "").strip()
    postcode = best_tags.get("addr:postcode", "").strip()

    if house_no and road:
        street = f"{house_no} {road}"
    elif road:
        street = road
    else:
        return None  # OSM entry exists but has no street address

    return street, postcode


# ---------------------------------------------------------------------------
# Nominatim direct search (original fallback)
# ---------------------------------------------------------------------------

def nominatim_search(query: str) -> dict | None:
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
    })
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data[0] if data else None
    except Exception as exc:
        print(f"  request error: {exc}", file=sys.stderr)
        return None


def is_coarse(result: dict) -> bool:
    return (
        result.get("class") == "place"
        and result.get("type") in _COARSE_OSM_TYPES
    )


def extract_street_and_postal(result: dict) -> tuple[str, str]:
    addr = result.get("address", {})
    house_no = addr.get("house_number", "").strip()
    road = (
        addr.get("road")
        or addr.get("pedestrian")
        or addr.get("footway")
        or addr.get("path")
        or ""
    ).strip()
    postal = addr.get("postcode", "").strip()
    if house_no and road:
        street = f"{house_no} {road}"
    elif road:
        street = road
    else:
        street = ""
    return street, postal


_IMAX_PREFIX_RE = re.compile(r"^IMAX\s*(?:Dome|Theatre|Theater|Screen)?\s*,?\s*", re.IGNORECASE)


def build_queries(name: str, city: str, state: str, country: str) -> list[str]:
    """Return ordered list of Nominatim query strings to try for this theater."""
    country_lower = country.strip().lower()
    is_us = country_lower in {"united states", "usa", "us"}
    country_q = "USA" if is_us else country.strip()
    if is_us:
        loc_full = f"{city}, {state}, USA" if state else f"{city}, USA"
        loc_city = f"{city}, USA"
    else:
        loc_full = f"{city}, {state}, {country_q}" if state else f"{city}, {country_q}"
        loc_city = f"{city}, {country_q}"

    queries = [f"{name}, {loc_full}", f"{name}, {loc_city}"]

    # For names like "IMAX Dome, McWane Center, Birmingham" also try searching
    # just the venue part after stripping the leading IMAX token so Nominatim
    # can match the building's canonical OSM name (e.g. "McWane Science Center").
    stripped = _IMAX_PREFIX_RE.sub("", name).strip().strip(",").strip()
    if stripped and stripped.lower() != name.lower():
        queries += [f"{stripped}, {loc_full}", f"{stripped}, {loc_city}"]

    return queries


# ---------------------------------------------------------------------------
# Mapdoor.com — business directory with embedded address JSON for US theaters
# ---------------------------------------------------------------------------

_MAPDOOR_RESULT_RE = re.compile(r'result\s*=\s*(\{[^;]+\})\s*;')
_MAPDOOR_SPAN_RE = re.compile(r'<span>(\d+[^<,]{3,60}(?:Dr|Drive|St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Way|Pkwy|Ln|Lane|Ct|Court|Pl|Place|Cir|Circle|Hwy|Highway|Mall|Plaza|Blvd)\.?[^<]{0,20})</span>', re.IGNORECASE)

# Map Website domain → mapdoor chain slug
_CHAIN_SLUGS = {
    "www.amctheatres.com": "amc-theatres",
    "www.regmovies.com": "regal-cinemas",
    "www.cinemark.com": "cinemark",
    "www.landmarkcinemas.com": "landmark-cinemas",
    "www.cineplex.com": "cineplex",
}


def _mapdoor_url(website_url: str, city: str, state: str) -> str | None:
    """Attempt to construct a mapdoor.com URL from a theater's Website value."""
    if not website_url:
        return None
    parsed = urllib.parse.urlparse(website_url)
    domain = parsed.netloc.lower()
    chain_slug = _CHAIN_SLUGS.get(domain)
    if not chain_slug:
        return None
    state_code = _US_STATE_CODES.get(state.strip().lower())
    if not state_code:
        return None
    # Theater slug = last non-empty path segment of the Website URL
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not path_parts:
        return None
    theater_slug = path_parts[-1]
    city_slug = re.sub(r"[^a-z0-9]+", "-", city.strip().lower()).strip("-")
    return f"https://mapdoor.com/us/{state_code}/{city_slug}/{chain_slug}/{theater_slug}"


def fetch_mapdoor_address(website_url: str, city: str, state: str) -> tuple[str, str]:
    """Fetch the mapdoor.com page for a US theater and extract its address."""
    url = _mapdoor_url(website_url, city, state)
    if not url:
        return "", ""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status not in (200, 301, 302):
                return "", ""
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return "", ""

    # Primary: embedded JS result object  {"address_min": "123 Main St, City CA 90000"}
    m = _MAPDOOR_RESULT_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            addr_min = data.get("address_min", "").strip()
            if addr_min:
                # addr_min format: "123 Main St, Los Angeles CA 90036"
                # Extract street (everything before the first comma)
                parts = addr_min.split(",")
                street = parts[0].strip()
                # Try to extract postcode from the rest
                postal_m = re.search(r"\b(\d{5}(?:-\d{4})?)\b", addr_min)
                postal = postal_m.group(1) if postal_m else ""
                if street:
                    return street, postal
        except Exception:
            pass

    # Fallback: <span> containing a street address
    for m in _MAPDOOR_SPAN_RE.finditer(html):
        street = m.group(1).strip()
        if street:
            postal_m = re.search(r"\b(\d{5}(?:-\d{4})?)\b", street)
            postal = postal_m.group(1) if postal_m else ""
            return street, postal

    return "", ""


# ---------------------------------------------------------------------------
# DuckDuckGo local search via real Chromium (playwright)
# ---------------------------------------------------------------------------

_DDG_JSONP_RE = re.compile(r'DDG\.duckbar\.add_local\((.+)\)\s*;?\s*$', re.DOTALL)
_POSTAL_RE = re.compile(r'\b([A-Z0-9]{2,4}\s?\d[A-Z0-9]{0,3}\s?\d[A-Z]{2}|\d{5}(?:-\d{4})?)\b')


def _ddg_browser_search(query: str) -> list[dict]:
    """
    Open a real Chromium browser, navigate to DDG, intercept the local.js
    JSONP response, and return the list of place dicts.  Returns [] on any
    failure or if playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    captured: list[str] = []

    def _on_response(response) -> None:
        if "local.js" in response.url and "duckduckgo.com" in response.url:
            try:
                captured.append(response.text())
            except Exception:
                pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = context.new_page()
            page.on("response", _on_response)
            page.goto(
                "https://duckduckgo.com/?q=" + query.replace(" ", "+") + "&kl=us-en",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(5000)
            browser.close()
    except Exception:
        return []

    places = []
    for body in captured:
        m = _DDG_JSONP_RE.search(body)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        places.extend(data.get("results", []))

    return places


def ddg_browser_address(
    csv_name: str, city: str, country: str, threshold: float = 0.45
) -> tuple[str, str]:
    """
    Search DDG local for the theater, fuzzy-match against returned places,
    and return (street, postal).  Returns ("", "") on no match.
    """
    query = f"{csv_name} {city} {country} address"
    places = _ddg_browser_search(query)
    if not places:
        return "", ""

    best_score = 0.0
    best_place = None
    for pl in places:
        # Keep "imax" during DDG scoring — Apple Maps often includes it in the
        # place name, so preserving it gives better discrimination.
        score = _name_score(csv_name, pl.get("name", ""), keep_imax=True)
        if score > best_score:
            best_score = score
            best_place = pl

    if best_score < threshold or best_place is None:
        return "", ""

    # Reject if the returned place is in a completely different country —
    # catches DDG returning a US business when querying an international theater.
    place_country = (best_place.get("country_code") or "").upper()
    csv_country_lower = country.strip().lower()
    is_us_csv = csv_country_lower in {"united states", "usa", "us"}
    if place_country and not is_us_csv and place_country == "US":
        return "", ""

    lines = best_place.get("address_lines", [])
    if not lines:
        addr = best_place.get("address", "")
        parts = addr.split(",")
        street = parts[0].strip() if parts else ""
        postal_m = _POSTAL_RE.search(addr)
        postal = postal_m.group(1) if postal_m else ""
        return street, postal

    street = lines[0].strip()
    # Postal code is usually in lines[1] (city/state/zip line)
    postal = ""
    for line in lines[1:]:
        pm = _POSTAL_RE.search(line)
        if pm:
            postal = pm.group(1)
            break

    return street, postal


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing to the CSV",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N theaters (useful for testing)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Randomly sample N theaters from the missing-address set",
    )
    parser.add_argument(
        "--country",
        default=None,
        metavar="NAME",
        help='Limit to theaters in this country (e.g. "United States")',
    )
    parser.add_argument(
        "--use-browser",
        action="store_true",
        help="Enable DuckDuckGo local search via a real Chromium browser (requires playwright)",
    )
    args = parser.parse_args()

    # --- Read CSV ---
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    if "Postal Code" not in fieldnames:
        addr_idx = fieldnames.index("Address")
        fieldnames.insert(addr_idx + 1, "Postal Code")
        for row in rows:
            row.setdefault("Postal Code", "")

    missing = [r for r in rows if not r.get("Address", "").strip()]
    if args.country:
        missing = [r for r in missing if r.get("Country", "").strip().lower() == args.country.strip().lower()]

    if args.sample:
        missing = random.sample(missing, min(args.sample, len(missing)))
    elif args.max:
        missing = missing[: args.max]

    total = len(missing)
    print(f"Rows to process : {total}")
    print(f"Mode            : {'DRY RUN' if args.dry_run else 'LIVE'}\n")

    updated_overpass = updated_nominatim = updated_mapdoor = updated_ddg = skipped = 0
    nom_req_count = 0

    # Cache Overpass results per city to avoid redundant API calls
    _overpass_cache: dict[str, list[dict]] = {}

    for i, row in enumerate(missing):
        name = row.get("Location Name", "").strip()
        city = row.get("City", "").strip()
        state = row.get("State/Province", "").strip()
        country = row.get("Country", "").strip()

        prefix = f"[{i + 1}/{total}] {name}, {city} ({country})"
        print(f"{prefix} ... ", end="", flush=True)

        street = postal = ""
        source = None

        # ------------------------------------------------------------------
        # Source 1: Overpass city search + fuzzy name match
        # ------------------------------------------------------------------
        city_key = f"{city}|{state}|{country}".lower()
        if city_key not in _overpass_cache:
            if nom_req_count > 0:
                time.sleep(NOMINATIM_DELAY)
            center = _nominatim_city_center(city, state, country)
            nom_req_count += 1

            if center:
                time.sleep(OVERPASS_DELAY)
                _overpass_cache[city_key] = _overpass_cinemas(*center)
            else:
                _overpass_cache[city_key] = []

        elements = _overpass_cache[city_key]
        if elements:
            match = _best_overpass_match(name, elements)
            if match:
                street, postal = match
                source = "overpass"

        # ------------------------------------------------------------------
        # Source 2: Mapdoor.com (US only, theaters with a Website URL)
        # ------------------------------------------------------------------
        if not street:
            website = row.get("Website", "").strip()
            country_lower = country.strip().lower()
            if website and country_lower in {"united states", "usa", "us"}:
                time.sleep(MAPDOOR_DELAY)
                s, p = fetch_mapdoor_address(website, city, state)
                if s:
                    street, postal = s, p
                    source = "mapdoor"

        # ------------------------------------------------------------------
        # Source 3: DuckDuckGo local search via real browser (optional)
        # ------------------------------------------------------------------
        if not street and args.use_browser:
            s, p = ddg_browser_address(name, city, country)
            if s:
                street, postal = s, p
                source = "ddg"

        # ------------------------------------------------------------------
        # Source 4: Nominatim direct search (original fallback)
        # ------------------------------------------------------------------
        if not street:
            queries = build_queries(name, city, state, country)
            for q in queries:
                if nom_req_count > 0:
                    time.sleep(NOMINATIM_DELAY)
                result = nominatim_search(q)
                nom_req_count += 1

                if not result:
                    continue
                if is_coarse(result):
                    print("coarse → ", end="", flush=True)
                    continue
                s, p = extract_street_and_postal(result)
                if s:
                    street, postal = s, p
                    source = "nominatim"
                    break

        # ------------------------------------------------------------------
        # Record result
        # ------------------------------------------------------------------
        if street:
            tag = f"[{source}]"
            print(f"OK {tag}  {street}  |  {postal or '(no postcode)'}")
            if not args.dry_run:
                row["Address"] = street
                row["Postal Code"] = postal
            if source == "overpass":
                updated_overpass += 1
            elif source == "mapdoor":
                updated_mapdoor += 1
            elif source == "ddg":
                updated_ddg += 1
            else:
                updated_nominatim += 1
        else:
            print("no match")
            skipped += 1

    total_updated = updated_overpass + updated_mapdoor + updated_ddg + updated_nominatim
    print(f"\n{'DRY RUN ' if args.dry_run else ''}Results:")
    print(f"  Found (overpass)  : {updated_overpass}")
    print(f"  Found (mapdoor)   : {updated_mapdoor}")
    print(f"  Found (ddg)       : {updated_ddg}")
    print(f"  Found (nominatim) : {updated_nominatim}")
    print(f"  Not found         : {skipped}")
    print(f"  Total updated     : {total_updated} / {total}")

    if args.dry_run:
        print("\nDry run complete — CSV not modified.")
        return

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nCSV written: {CSV_PATH}")


if __name__ == "__main__":
    main()
