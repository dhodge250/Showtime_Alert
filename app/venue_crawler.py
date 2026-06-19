"""
IMAX Venue Crawler
==================
Parses the local ``imax_venues.md`` file (a snapshot of the IMAX fandom wiki
source) to extract ALL commercial IMAX venues worldwide, then enriches each
entry with coordinates via the Nominatim geocoding API (OpenStreetMap).

Runs on a separate, infrequent schedule (default: every 7 days) because the
venue list changes rarely compared to showtimes.  To pick up new theaters,
update ``imax_venues.md`` from the wiki and trigger a crawl.

Crawl pipeline
--------------
1. Read ``imax_venues.md`` from the project root (or ``/app/imax_venues.md``
   when running inside Docker).
2. Parse every regional MediaWiki table (Europe, Asia, Oceania, Africa,
   Americas).
3. For each venue, call Nominatim to geocode → lat/lon + address.
4. Derive the chain website URL from the chain name.
5. Upsert the Theater row in the DB (insert if new, update if changed).

Table column layouts
--------------------
- Europe / Oceania / Africa  (9 cols):
    Country | City | Name | ScreenAR | DigProj | MaxAR | FilmProj | Dims | Commercial
- Asia  (10 cols):
    Country | Province | City | Name | ScreenAR | DigProj | MaxAR | FilmProj | Dims | Commercial
- Americas  (10 cols):
    Country | State | City | Name | ScreenAR | DigProj | MaxAR | FilmProj | Dims | Commercial

Geocoding rate limit
--------------------
Nominatim's usage policy requires ≤1 request/second and a descriptive
User-Agent.  The crawler enforces a 1.1-second delay between geocode calls.
"""
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from app import db
from app.models import Theater
from app.lookup_helpers import (
    get_or_create_aspect_ratio,
    get_or_create_audio_system,
    get_or_create_chain,
    get_or_create_city,
    get_or_create_continent,
    get_or_create_country,
    get_or_create_projector_type,
    get_or_create_region,
    parse_screen_dims,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
VENUES_MD_PATH = _HERE.parent / "imax_venues.md"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOCODE_DELAY_SECONDS = 1.1
MAX_GEOCODE_FAILURES = 10
REQUEST_TIMEOUT = 20

# OSM class/type pairs that indicate the geocoder matched a city or
# administrative boundary rather than an actual venue.  Results matching
# these are rejected and the next query strategy is tried.
_COARSE_OSM_TYPES = frozenset({
    "city", "town", "village", "suburb", "county", "state", "country",
    "administrative", "municipality", "district", "region", "province",
    "borough", "quarter", "neighbourhood",
})

# Strips chain numbers and "& IMAX" suffix to get a shorter venue name that
# OSM is more likely to have indexed (e.g. "AMC River Park Square 20 & IMAX"
# → "AMC River Park Square").
_GEOCODE_NAME_SIMPLIFY_RE = re.compile(
    r"\s*(?:&\s*)?IMAX\b.*$|\s+\d+\s*$|\bDINE[\s\-]?IN\b", re.IGNORECASE
)

# CSV seed file path
_CSV_SEED_PATH = Path(__file__).parent.parent / "seeds" / "imax_theaters.csv"

# Mapping of Theater DB field → CSV column header for re-seed operations
CSV_RESEED_COLUMNS: dict[str, str] = {
    "address":             "Address",
    "zip_code":            "Postal Code",
    "phone":               "Phone",
    "website":             "Website",
    "chain":               "Chain",
    "venue_key":           "Venue Key",
    "audio_system":        "Audio System",
    "screen_dims":         "Screen Dimensions",
    "screen_size":         "Screen AR",
    "projector_type":      "Digital Projector",
    "film_projector_type": "Film Projector",
    "commercial_films":    "Commercial Films Shown",
}

NOMINATIM_HEADERS = {
    "User-Agent": "IMAXAlert/1.0 (IMAX theater notification app; contact via GitHub)"
}

# ---------------------------------------------------------------------------
# Section definitions — each section maps to a column layout
# ---------------------------------------------------------------------------

# Sections found in imax_venues.md and their column schemas.
# col_layout values:
#   "no_state"  — 9 cols: Country, City, Name, …
#   "with_state"— 10 cols: Country, State/Province, City, Name, …
SECTIONS = [
    {"header": "Europe",   "col_layout": "no_state"},
    {"header": "Asia",     "col_layout": "with_state"},
    {"header": "Oceania",  "col_layout": "no_state"},
    {"header": "Africa",   "col_layout": "no_state"},
    {"header": "Americas", "col_layout": "with_state"},
]

# ---------------------------------------------------------------------------
# Chain → website URL mapping
# ---------------------------------------------------------------------------

CHAIN_WEBSITE_MAP: dict[str, str] = {
    "amc": "https://www.amctheatres.com",
    "regal": "https://www.regmovies.com",
    "cinemark": "https://www.cinemark.com",
    "tcl": "https://www.tclchinesetheatres.com",
    "alamo drafthouse": "https://drafthouse.com",
    "harkins": "https://www.harkins.com",
    "marcus": "https://www.marcustheatres.com",
    "showcase": "https://www.showcasecinemas.com",
    "studio movie grill": "https://www.studiomoviegrill.com",
    "smg": "https://www.studiomoviegrill.com",
    "ipic": "https://www.ipic.com",
    "bow tie": "https://www.bowtiecinemas.com",
    "look": "https://www.lookcinemas.com",
    "violet crown": "https://violetcrown.com",
    "reading": "https://www.readingcinemasus.com",
    "b&b": "https://www.bbtheatres.com",
    "santikos": "https://santikos.com",
    "emagine": "https://www.emagine-entertainment.com",
    "cinepolis": "https://cinepolisusa.com",
    "flix": "https://www.flixbrewhouse.com",
    "landmark": "https://www.landmarktheatres.com",
    "pacific": "https://www.pacifictheatres.com",
    "cineplex": "https://www.cineplex.com",
    "cineplexx": "https://www.cineplexx.at",
    "odeon": "https://www.odeon.co.uk",
    "vue": "https://www.myvue.com",
    "cineworld": "https://www.cineworld.co.uk",
    "kinepolis": "https://kinepolis.com",
    "pathé": "https://www.pathe.nl",
    "pathe": "https://www.pathe.nl",
    "cgv": "https://www.cgv.com",
    "wanda": "https://www.wandacinemas.com",
    "cinemex": "https://www.cinemex.com",
    "cinépolis": "https://www.cinepolis.com",
}

# ---------------------------------------------------------------------------
# Chain name normalizer
# ---------------------------------------------------------------------------

CHAIN_CANONICAL: dict[str, str] = {
    "amc theatres": "AMC", "amc": "AMC", "amc classic": "AMC", "amc dine-in": "AMC",
    "regal cinemas": "Regal", "regal": "Regal",
    "cinemark": "Cinemark", "cinemark theatres": "Cinemark",
    "cineplex": "Cineplex", "cineplex cinemas": "Cineplex",
    "cineplex entertainment": "Cineplex", "cineplex odeon": "Cineplex",
    "scotiabank theatre": "Cineplex", "silvercity": "Cineplex",
    "tcl chinese theatre": "TCL", "tcl": "TCL",
    "alamo drafthouse": "Alamo Drafthouse", "alamo": "Alamo Drafthouse",
    "harkins theatres": "Harkins", "harkins": "Harkins",
    "marcus theatres": "Marcus", "marcus": "Marcus",
    "showcase cinemas": "Showcase", "showcase": "Showcase",
    "studio movie grill": "Studio Movie Grill", "smg": "Studio Movie Grill",
    "ipic theaters": "iPic", "ipic": "iPic",
    "bow tie cinemas": "Bow Tie",
    "look dine-in cinemas": "LOOK", "look cinemas": "LOOK",
    "violet crown cinemas": "Violet Crown",
    "reading cinemas": "Reading",
    "b&b theatres": "B&B",
    "santikos entertainment": "Santikos", "santikos": "Santikos",
    "emagine entertainment": "Emagine",
    "cinepolis usa": "Cinepolis", "cinepolis": "Cinepolis",
    "cinemex": "Cinemex",
    "flix brewhouse": "Flix Brewhouse",
    "landmark theatres": "Landmark",
    "pacific theatres": "Pacific",
    "cineplexx": "Cineplexx",
    "odeon": "Odeon",
    "vue": "Vue",
    "cineworld": "Cineworld",
    "kinepolis": "Kinepolis",
    "pathé": "Pathé", "pathe": "Pathé",
    "cgv": "CGV",
    "wanda": "Wanda",
}

# Keywords that indicate a non-commercial/institutional venue to skip
_SKIP_KEYWORDS = [
    "science center", "science centre", "museum", "aquarium", "zoo",
    "planetarium", "educational", "omnimax", "omni theater", "omni theatre",
    "visitors center", "visitors centre", "visitor center", "visitor centre",
    "state history museum", "state museum", "natural history",
    "imax dome, mc", "dome, mc",
]


# ---------------------------------------------------------------------------
# Wiki markup helpers
# ---------------------------------------------------------------------------

def _clean_wiki_cell(text: str) -> str:
    """Strip MediaWiki link markup from a cell value."""
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    return text.strip()


def _parse_cell_line(raw_line: str) -> tuple[str, int]:
    """
    Parse a single ``|…`` cell line.
    Returns (cell_value, rowspan_count).
    """
    content = raw_line.lstrip("|").strip()
    rowspan = 1
    m = re.match(r'rowspan="(\d+)"\s*\|(.*)', content)
    if m:
        rowspan = int(m.group(1))
        content = m.group(2).strip()
    content = re.sub(r'colspan="\d+"\s*\|', "", content)
    content = _clean_wiki_cell(content)
    return content, rowspan


# ---------------------------------------------------------------------------
# Per-section parser
# ---------------------------------------------------------------------------

def _parse_section_table(
    table_lines: list[str],
    col_layout: str,
    section_name: str,
) -> list[dict]:
    """
    Parse a single regional table from the wiki markup.

    col_layout:
      "no_state"   — Country, City, Name, ScreenAR, DigProj, MaxAR, FilmProj, Dims, Commercial
      "with_state" — Country, State/Province, City, Name, ScreenAR, DigProj, MaxAR, FilmProj, Dims, Commercial
    """
    venues: list[dict] = []

    country_val = ""
    country_rows_left = 0
    state_val = ""
    state_rows_left = 0

    row_raw: list[tuple[str, int]] = []
    in_table = False

    def process_row(cells: list[tuple[str, int]]) -> None:
        nonlocal country_val, country_rows_left, state_val, state_rows_left

        cell_idx = 0

        # Country column (always present with possible rowspan)
        if country_rows_left > 0:
            country_rows_left -= 1
        else:
            if cell_idx < len(cells):
                country_val = cells[cell_idx][0].strip()
                country_rows_left = cells[cell_idx][1] - 1
                cell_idx += 1

        if not country_val:
            return

        # State/Province column (only for "with_state" layouts)
        if col_layout == "with_state":
            if state_rows_left > 0:
                state_rows_left -= 1
            else:
                if cell_idx < len(cells):
                    state_val = cells[cell_idx][0].strip()
                    state_rows_left = cells[cell_idx][1] - 1
                    cell_idx += 1
        else:
            state_val = ""

        def gc(offset: int) -> str:
            idx = cell_idx + offset
            return cells[idx][0] if idx < len(cells) else ""

        city        = gc(0)
        name        = gc(1)
        screen_size = gc(2)
        projector   = gc(3)
        # gc(4) = MaxAR, gc(5) = FilmProjector
        screen_dims = gc(6)   # physical screen dimensions, e.g. "26.0 m × 19.6 m"
        commercial  = gc(7)

        if not name:
            return

        lower_name = name.lower()

        # Skip institutional / non-commercial venues
        if any(kw in lower_name for kw in _SKIP_KEYWORDS):
            logger.debug("Skipping institutional venue: %s", name)
            return

        # For US entries in Americas, apply the stricter commercial check
        # (same logic as before). For all other countries, include everything
        # since most international venues are legitimate commercial cinemas.
        if country_val == "United States":
            is_commercial_chain = any(kw in lower_name for kw in [
                "amc", "regal", "cinemark", "harkins", "marcus", "showcase",
                "alamo", "cinepolis", "emagine", "flix", "landmark", "santikos",
                "ipic", "bow tie", "look cinemas", "violet crown", "reading cinemas",
                "b&b theatres", "pacific theatres", "tcl chinese",
            ])
            if commercial.strip().lower() not in ("yes", "y") and not is_commercial_chain:
                logger.debug("Skipping non-commercial US venue: %s", name)
                return

        venues.append({
            "name": name,
            "chain_raw": _chain_from_name(name),
            "city": city,
            "state": state_val,
            "country": country_val,
            "screen_size": screen_size,
            "screen_dims": screen_dims,
            "projector_type": projector,
            "section": section_name,
        })

    for line in table_lines:
        stripped = line.strip()

        if stripped.startswith("{|"):
            in_table = True
            continue
        if stripped == "|}":
            if row_raw:
                process_row(row_raw)
            break
        if not in_table:
            continue
        if stripped.startswith("!"):
            continue
        if stripped == "|-" or stripped.startswith("|-"):
            if row_raw:
                process_row(row_raw)
                row_raw = []
            continue
        if stripped.startswith("|"):
            val, rs = _parse_cell_line(stripped)
            row_raw.append((val, rs))

    return venues


# ---------------------------------------------------------------------------
# Top-level parser: finds and parses all sections
# ---------------------------------------------------------------------------

def fetch_venue_list() -> list[dict]:
    """
    Parse ``imax_venues.md`` and extract ALL commercial IMAX venues worldwide.

    Returns a list of dicts with keys:
        name, chain_raw, city, state, country, screen_size, projector_type, section
    """
    md_path = VENUES_MD_PATH
    if not md_path.exists():
        logger.error("imax_venues.md not found at %s", md_path)
        return []

    logger.info("Parsing IMAX venue list from %s", md_path)
    lines = md_path.read_text(encoding="utf-8").splitlines()

    # Build a map: section_name → (table_start, table_end) line indices
    section_bounds: dict[str, tuple[int, int]] = {}

    current_section = None
    current_table_start = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Match section header like "== Europe ==" or "==Asia=="
        m = re.match(r"^==\s*([^=]+?)\s*==$", stripped)
        if m:
            current_section = m.group(1).strip()
            current_table_start = None
            continue
        if current_section and stripped.startswith("{|") and current_table_start is None:
            current_table_start = i
        if current_table_start is not None and stripped == "|}":
            section_bounds[current_section] = (current_table_start, i)
            current_table_start = None
            current_section = None

    all_venues: list[dict] = []

    for section_def in SECTIONS:
        header = section_def["header"]
        col_layout = section_def["col_layout"]

        if header not in section_bounds:
            logger.warning("Section '%s' not found in imax_venues.md", header)
            continue

        start, end = section_bounds[header]
        table_lines = lines[start: end + 1]
        venues = _parse_section_table(table_lines, col_layout, header)
        logger.info("  %s: parsed %d venues", header, len(venues))
        all_venues.extend(venues)

    logger.info("Total venues parsed: %d", len(all_venues))
    return all_venues


# ---------------------------------------------------------------------------
# Chain helpers
# ---------------------------------------------------------------------------

def _chain_from_name(name: str) -> str:
    lower = name.lower()
    if "amc" in lower:
        return "AMC"
    if "regal" in lower:
        return "Regal"
    if "cinemark" in lower:
        return "Cinemark"
    if "tcl chinese" in lower:
        return "TCL"
    if "alamo drafthouse" in lower or "alamo" in lower:
        return "Alamo Drafthouse"
    if "harkins" in lower:
        return "Harkins"
    if "marcus" in lower:
        return "Marcus"
    if "showcase" in lower:
        return "Showcase"
    if "studio movie grill" in lower or "smg" in lower:
        return "Studio Movie Grill"
    if "ipic" in lower:
        return "iPic"
    if "bow tie" in lower:
        return "Bow Tie"
    if "look cinemas" in lower or "look dine" in lower:
        return "LOOK"
    if "violet crown" in lower:
        return "Violet Crown"
    if "reading cinemas" in lower:
        return "Reading"
    if "b&b theatres" in lower or "b&b" in lower:
        return "B&B"
    if "santikos" in lower:
        return "Santikos"
    if "emagine" in lower:
        return "Emagine"
    if "cinepolis" in lower or "cinépolis" in lower:
        return "Cinepolis"
    if "cinemex" in lower:
        return "Cinemex"
    if "flix brewhouse" in lower:
        return "Flix Brewhouse"
    if "landmark" in lower:
        return "Landmark"
    if "pacific theatres" in lower or "pacific theaters" in lower:
        return "Pacific"
    if "cineplexx" in lower:
        return "Cineplexx"
    if "odeon" in lower:
        return "Odeon"
    if "vue" in lower:
        return "Vue"
    if "cineworld" in lower:
        return "Cineworld"
    if "kinepolis" in lower:
        return "Kinepolis"
    if "pathé" in lower or "pathe" in lower:
        return "Pathé"
    if "cgv" in lower:
        return "CGV"
    if "wanda" in lower:
        return "Wanda"
    return ""


def _canonicalize_chain(raw: str) -> str:
    key = raw.lower().strip()
    if key in CHAIN_CANONICAL:
        return CHAIN_CANONICAL[key]
    for k, v in CHAIN_CANONICAL.items():
        if k in key or key in k:
            return v
    return raw.strip().title() if raw.strip() else "Independent"


def _chain_website(canonical_chain: str) -> str:
    key = canonical_chain.lower()
    if key in CHAIN_WEBSITE_MAP:
        return CHAIN_WEBSITE_MAP[key]
    for k, v in CHAIN_WEBSITE_MAP.items():
        if k in key:
            return v
    return "https://www.imax.com/theatres"


# ---------------------------------------------------------------------------
# Geocoding via Nominatim (OpenStreetMap)
# ---------------------------------------------------------------------------

def geocode_venue(name: str, city: str, state: str, country: str,
                  address: str = "", zip_code: str = "") -> dict:
    """
    Look up coordinates for a theater using Nominatim.

    Query priority:
      1. Free-text address: "123 Main St, City, State, ZIP"
      2. Structured address: Nominatim street/city/state/country params
      3. Full name free-text: "Theater Name, City, State, Country"
      4. Simplified name: strip chain number and IMAX suffix for a better OSM hit

    City-only queries are intentionally omitted — they always return the city
    center node, not a venue location.  Results whose OSM type indicates a
    city or administrative boundary are rejected so city-center coordinates
    are never returned as a theater location.

    Returns a dict with keys: address, zip_code, latitude, longitude,
    city_name, state_name, country_name.
    """
    result = {
        "address": "", "zip_code": "", "latitude": None, "longitude": None,
        "city_name": "", "state_name": "", "country_name": "",
    }

    is_us = country in ("United States", "US", "USA")
    country_q = "USA" if is_us else country
    params_base: dict = {
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
    }
    if is_us:
        params_base["countrycodes"] = "us"

    # Each entry is merged into params_base for a Nominatim request.
    # Use {"q": "..."} for free-text search, or structured keys
    # (street/city/state/country/postalcode) for Nominatim's structured search.
    queries: list[dict] = []

    if address and address.strip():
        # 1. Free-text address query
        parts = [address.strip(), city, state] if is_us else [address.strip(), city, country_q]
        if zip_code and zip_code.strip():
            parts.append(zip_code.strip())
        queries.append({"q": ", ".join(p for p in parts if p)})

        # 2. Structured query — often more reliable when free-text address fails
        struct: dict = {"street": address.strip(), "city": city}
        if is_us:
            if state:
                struct["state"] = state
            struct["country"] = "US"
            if zip_code and zip_code.strip():
                struct["postalcode"] = zip_code.strip()
        else:
            struct["country"] = country_q
        queries.append(struct)

    # 3. Full name free-text
    queries.append({"q": f"{name}, {city}, {state}, USA" if is_us else f"{name}, {city}, {country_q}"})

    # 4. Simplified name: drop trailing number and "& IMAX" so the base venue
    #    name (e.g. "AMC River Park Square") has a better chance of an OSM hit
    short_name = _GEOCODE_NAME_SIMPLIFY_RE.sub("", name).strip(" ,&")
    if short_name and short_name.lower() != name.lower():
        q4 = f"{short_name}, {city}, {state}, USA" if is_us else f"{short_name}, {city}, {country_q}"
        queries.append({"q": q4})

    hit = None
    for i, q_override in enumerate(queries):
        if i > 0:
            time.sleep(GEOCODE_DELAY_SECONDS)
        request_params = {**params_base, **q_override}
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params=request_params,
                headers=NOMINATIM_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Geocode request failed for %r: %s", q_override, exc)
            return result
        except ValueError as exc:
            logger.warning("Geocode JSON parse error for %r: %s", q_override, exc)
            return result

        if not data:
            continue

        candidate = data[0]
        if candidate.get("class") == "place" and candidate.get("type") in _COARSE_OSM_TYPES:
            logger.debug(
                "Rejecting coarse OSM result (class=%s type=%s) for query %r",
                candidate.get("class"), candidate.get("type"), q_override,
            )
            continue

        hit = candidate
        break

    if not hit:
        logger.warning("No geocode results for '%s, %s %s %s'", name, city, state, country)
        return result

    addr = hit.get("address", {})
    number = addr.get("house_number", "")
    road = addr.get("road", "")
    street = f"{number} {road}".strip() if number else road
    result["address"]      = street
    result["zip_code"]     = addr.get("postcode", "")
    result["latitude"]     = float(hit.get("lat", 0)) or None
    result["longitude"]    = float(hit.get("lon", 0)) or None
    result["city_name"]    = (addr.get("city") or addr.get("town") or
                              addr.get("village") or addr.get("municipality") or "")
    result["state_name"]   = addr.get("state", "")
    result["country_name"] = addr.get("country", "")
    return result


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def upsert_theater(venue: dict, geo: dict) -> tuple[Theater, bool]:
    """
    Insert a new Theater row or update an existing one.
    Match key: name (case-insensitive) + country + city.
    """
    name = venue["name"].strip()
    country = venue.get("country", "United States").strip()
    city = venue.get("city", "").strip()
    state = venue.get("state", "").strip()

    existing = Theater.query.filter(
        db.func.lower(Theater.name) == name.lower(),
        db.func.lower(Theater.country) == country.lower(),
    ).first()

    chain = _canonicalize_chain(venue.get("chain_raw", ""))
    website = _chain_website(chain)
    now = datetime.now(timezone.utc)

    # Resolve FK lookup objects
    chain_obj    = get_or_create_chain(chain) if chain else None
    country_obj  = get_or_create_country(country) if country else None
    region_obj   = get_or_create_region(state, country_obj) if state and country_obj else None
    city_obj     = get_or_create_city(city, country_obj, region_obj) if city and country_obj else None
    pt_obj       = get_or_create_projector_type(venue["projector_type"]) if venue.get("projector_type") else None

    # Parse screen dims to metres
    w_m, h_m = parse_screen_dims(venue.get("screen_dims", ""))

    if existing:
        if chain_obj:
            existing.chain    = chain_obj.name
            existing.chain_id = chain_obj.id
        if website:
            existing.website = existing.website or website
        if geo.get("address"):
            existing.address = geo["address"]
        if geo.get("zip_code"):
            existing.zip_code = geo["zip_code"]
        if geo.get("latitude"):
            existing.latitude = geo["latitude"]
        if geo.get("longitude"):
            existing.longitude = geo["longitude"]
        if venue.get("screen_size"):
            existing.screen_size = venue["screen_size"]
        if venue.get("screen_dims"):
            existing.screen_dims = venue["screen_dims"]
        if venue.get("projector_type"):
            existing.projector_type = venue["projector_type"]
        if pt_obj:
            existing.projector_type_id = pt_obj.id
        if w_m is not None:
            existing.screen_width_m  = w_m
        if h_m is not None:
            existing.screen_height_m = h_m
        if country_obj:
            existing.country    = country_obj.name
            existing.country_id = country_obj.id
        if region_obj:
            existing.state     = region_obj.name
            existing.region_id = region_obj.id
        elif state:
            existing.state = state
        if city_obj:
            existing.city    = city_obj.name
            existing.city_id = city_obj.id
        elif city:
            existing.city = city
        existing.is_active       = True
        existing.crawl_source    = "imax_venues_md"
        existing.last_crawled_at = now
        return existing, False

    theater = Theater(
        name=name,
        chain=chain_obj.name if chain_obj else chain,
        chain_id=chain_obj.id if chain_obj else None,
        city=city_obj.name if city_obj else city,
        city_id=city_obj.id if city_obj else None,
        state=region_obj.name if region_obj else state,
        region_id=region_obj.id if region_obj else None,
        country=country_obj.name if country_obj else country,
        country_id=country_obj.id if country_obj else None,
        address=geo.get("address", ""),
        zip_code=geo.get("zip_code", ""),
        latitude=geo.get("latitude"),
        longitude=geo.get("longitude"),
        screen_size=venue.get("screen_size", ""),
        screen_dims=venue.get("screen_dims", ""),
        screen_width_m=w_m,
        screen_height_m=h_m,
        projector_type=venue.get("projector_type", ""),
        projector_type_id=pt_obj.id if pt_obj else None,
        audio_system="",
        website=website,
        phone="",
        image_url="",
        is_active=True,
        crawl_source="imax_venues_md",
        last_crawled_at=now,
    )
    db.session.add(theater)
    return theater, True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_venue_crawl() -> dict:
    """
    Run the full venue crawl pipeline for all countries.

    Returns a summary dict:
        venues_found, geocoded, geocode_failed, inserted, updated, errors
    """
    summary = {
        "venues_found": 0,
        "geocoded": 0,
        "geocode_failed": 0,
        "inserted": 0,
        "updated": 0,
        "errors": [],
    }

    venues = fetch_venue_list()
    summary["venues_found"] = len(venues)

    if not venues:
        msg = "Venue crawl returned 0 venues — imax_venues.md may be missing or unparseable"
        logger.warning(msg)
        summary["errors"].append(msg)
        return summary

    consecutive_geocode_failures = 0
    geocode_gave_up = False

    for venue in venues:
        name    = venue.get("name", "")
        city    = venue.get("city", "")
        state   = venue.get("state", "")
        country = venue.get("country", "United States")

        if not geocode_gave_up:
            time.sleep(GEOCODE_DELAY_SECONDS)
            geo = geocode_venue(name, city, state, country)
            if geo.get("latitude"):
                summary["geocoded"] += 1
                consecutive_geocode_failures = 0
            else:
                summary["geocode_failed"] += 1
                consecutive_geocode_failures += 1
                logger.debug("Geocode failed for '%s, %s %s %s'", name, city, state, country)
                if consecutive_geocode_failures >= MAX_GEOCODE_FAILURES:
                    geocode_gave_up = True
                    logger.warning(
                        "Too many consecutive geocode failures (%d); skipping geocoding for remaining venues",
                        consecutive_geocode_failures,
                    )
        else:
            geo = {}

        try:
            theater, is_new = upsert_theater(venue, geo)
            if is_new:
                summary["inserted"] += 1
            else:
                summary["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"Failed to upsert theater '{name}': {exc}"
            logger.error(msg)
            summary["errors"].append(msg)

    try:
        db.session.commit()
        logger.info(
            "Venue crawl complete: %d found, %d inserted, %d updated, %d geocoded, %d geocode failures",
            summary["venues_found"],
            summary["inserted"],
            summary["updated"],
            summary["geocoded"],
            summary["geocode_failed"],
        )
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        msg = f"DB commit failed after venue crawl: {exc}"
        logger.error(msg)
        summary["errors"].append(msg)

    return summary


# ---------------------------------------------------------------------------
# Bulk geocoder — geocode all theaters missing lat/lng
# ---------------------------------------------------------------------------

_geocode_status: dict = {
    "running":      False,
    "started_at":   None,
    "finished_at":  None,
    "total":        0,
    "processed":    0,
    "geocoded":     0,
    "failed":       0,
    "errors":       [],
}


def get_geocode_status() -> dict:
    """Return a snapshot of the current bulk-geocode status."""
    return dict(_geocode_status)


def run_bulk_geocode(app, mode: str = "missing") -> None:
    """
    Geocode Theater rows in a background daemon thread.

    mode="missing"  — only rows with latitude=NULL or longitude=NULL (default)
    mode="all"      — every row, overwriting existing coordinates

    Updates ``_geocode_status`` in real time so the UI can poll for progress.
    Only latitude and longitude are written back; address and zip_code are
    inputs to the geocoder, not outputs from it.
    """
    global _geocode_status  # noqa: PLW0603

    with app.app_context():
        q = db.session.query(
            Theater.id,
            Theater.name,
            Theater.city,
            Theater.state,
            Theater.country,
            Theater.address,
            Theater.zip_code,
        )
        if mode == "missing":
            q = q.filter(db.or_(Theater.latitude.is_(None), Theater.longitude.is_(None)))
        rows = q.order_by(Theater.id).all()

    theaters = [
        {
            "id":       r.id,
            "name":     r.name     or "",
            "city":     r.city     or "",
            "state":    r.state    or "",
            "country":  r.country  or "United States",
            "address":  r.address  or "",
            "zip_code": r.zip_code or "",
        }
        for r in rows
    ]

    total = len(theaters)
    _geocode_status = {
        "running":      True,
        "mode":         mode,
        "started_at":   datetime.now(timezone.utc).isoformat(),
        "finished_at":  None,
        "total":        total,
        "processed":    0,
        "geocoded":     0,
        "failed":       0,
        "errors":       [],
    }
    logger.info("Bulk geocode (%s) starting: %d theaters to process.", mode, total)
    with app.app_context():
        from app.log_utils import write_log
        write_log("geocode", f"Bulk geocode ({mode}) started: {total} theaters to process",
                  details={"total": total, "mode": mode})

    try:
        for theater in theaters:
            name     = theater["name"]
            city     = theater["city"]
            state    = theater["state"]
            country  = theater["country"]
            address  = theater["address"]
            zip_code = theater["zip_code"]

            time.sleep(GEOCODE_DELAY_SECONDS)

            try:
                geo = geocode_venue(name, city, state, country,
                                    address=address, zip_code=zip_code)
            except Exception as exc:  # noqa: BLE001
                msg = f"geocode_venue raised for '{name}': {exc}"
                logger.warning(msg)
                _geocode_status["errors"].append(msg)
                _geocode_status["processed"] += 1
                _geocode_status["failed"]    += 1
                with app.app_context():
                    from app.log_utils import write_log
                    write_log("geocode", msg, level="ERROR",
                              details={"theater_id": theater["id"], "name": name,
                                       "city": city, "country": country})
                continue

            with app.app_context():
                from app.log_utils import write_log
                t = Theater.query.get(theater["id"])
                if t is None:
                    _geocode_status["processed"] += 1
                    _geocode_status["failed"]    += 1
                    continue

                if geo.get("latitude"):
                    t.latitude  = geo["latitude"]
                    t.longitude = geo["longitude"]
                    # address and zip_code are source data — never overwritten by geocoding
                    try:
                        db.session.commit()
                        _geocode_status["geocoded"] += 1
                    except Exception as exc:  # noqa: BLE001
                        db.session.rollback()
                        msg = f"DB commit failed for '{name}': {exc}"
                        logger.error(msg)
                        _geocode_status["errors"].append(msg)
                        _geocode_status["failed"] += 1
                        write_log("geocode", msg, level="ERROR",
                                  details={"theater_id": theater["id"], "name": name})
                else:
                    logger.warning("No geocode result for '%s, %s %s %s'",
                                   name, city, state, country)
                    write_log("geocode",
                              f"Geocode failed: {name}, {city} {state} {country}",
                              level="WARNING",
                              details={"theater_id": theater["id"], "name": name,
                                       "city": city, "state": state, "country": country})
                    _geocode_status["failed"] += 1

            _geocode_status["processed"] += 1

    except Exception as exc:  # noqa: BLE001
        msg = f"Bulk geocode aborted unexpectedly: {exc}"
        logger.error(msg)
        _geocode_status["errors"].append(msg)
        with app.app_context():
            from app.log_utils import write_log
            write_log("geocode", msg, level="ERROR")
    finally:
        _geocode_status["running"]     = False
        _geocode_status["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary_msg = (
            f"Bulk geocode ({mode}) complete: {_geocode_status['geocoded']}/{total} geocoded, "
            f"{_geocode_status['failed']} failed, {len(_geocode_status['errors'])} errors"
        )
        logger.info(summary_msg)
        level = "WARNING" if _geocode_status["failed"] > 0 else "INFO"
        with app.app_context():
            from app.log_utils import write_log
            write_log("geocode", summary_msg, level=level,
                      details={
                          "total": total,
                          "mode":  mode,
                          "geocoded": _geocode_status["geocoded"],
                          "failed":   _geocode_status["failed"],
                          "errors":   len(_geocode_status["errors"]),
                      })


# ---------------------------------------------------------------------------
# Geocoding reset
# ---------------------------------------------------------------------------

def reset_geocoding() -> int:
    """
    Null out latitude and longitude for every Theater row.
    Returns the number of rows affected.
    Only touches lat/lng — address and all other fields are left unchanged.
    """
    result = db.session.execute(
        db.text("UPDATE theaters SET latitude = NULL, longitude = NULL")
    )
    db.session.commit()
    return result.rowcount


# ---------------------------------------------------------------------------
# Re-seed from CSV
# ---------------------------------------------------------------------------

def reseed_from_csv(columns: list[str], dry_run: bool = False) -> dict:
    """
    Restore selected Theater columns from seeds/imax_theaters.csv.

    Match key: Location Name (case-insensitive) + Country.
    Only columns present in CSV_RESEED_COLUMNS are accepted.
    Returns {"updated": N, "skipped": N, "unmatched": N, "errors": []}.
    """
    import csv as _csv

    valid = [c for c in columns if c in CSV_RESEED_COLUMNS]
    if not valid:
        return {"updated": 0, "skipped": 0, "unmatched": 0, "errors": ["No valid columns specified"]}

    if not _CSV_SEED_PATH.exists():
        return {"updated": 0, "skipped": 0, "unmatched": 0,
                "errors": [f"Seed file not found: {_CSV_SEED_PATH}"]}

    updated = skipped = unmatched = 0
    errors: list[str] = []

    with open(_CSV_SEED_PATH, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            location_name = (row.get("Location Name") or "").strip()
            country_name  = (row.get("Country") or "").strip()
            if not location_name:
                skipped += 1
                continue

            t = Theater.query.filter(
                db.func.lower(Theater.name) == location_name.lower(),
                db.func.lower(Theater.country) == country_name.lower(),
            ).first()

            if t is None:
                unmatched += 1
                continue

            if dry_run:
                updated += 1
                continue

            try:
                for col in valid:
                    csv_header = CSV_RESEED_COLUMNS[col]
                    raw = (row.get(csv_header) or "").strip() or None
                    setattr(t, col, raw)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Error updating '{location_name}': {exc}")

    if not dry_run and updated:
        try:
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            errors.append(f"DB commit failed: {exc}")
            updated = 0

    return {"updated": updated, "skipped": skipped, "unmatched": unmatched, "errors": errors}


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

_EXPORT_FIELDS = [
    "Region", "Country", "State/Province", "City", "Location Name",
    "Screen AR", "Digital Projector", " max AR (Digital)", "Film Projector",
    "Screen Dimensions", "Commercial Films Shown", "Venue Key", "Chain",
    "Website", "Audio System", "Address", "Postal Code", "Phone", "Active",
]


def export_theaters_csv() -> str:
    """Return all theaters as a CSV string compatible with import_theaters_from_csv_str."""
    import csv as _csv
    import io

    theaters = Theater.query.order_by(Theater.name).all()
    out = io.StringIO()
    writer = _csv.DictWriter(out, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for t in theaters:
        writer.writerow({
            "Region":                  t.continent_name,
            "Country":                 t.country_name,
            "State/Province":          t.region_name,
            "City":                    t.city_name,
            "Location Name":           t.name,
            "Screen AR":               t.aspect_ratio_label,
            "Digital Projector":       t.projector_type_name,
            " max AR (Digital)":       t.digital_projector_ar_label,
            "Film Projector":          t.film_projector_type_name,
            "Screen Dimensions":       t.screen_dims or "",
            "Commercial Films Shown":  t.commercial_films or "",
            "Venue Key":               t.venue_key or "",
            "Chain":                   t.chain_name,
            "Website":                 t.website or "",
            "Audio System":            t.audio_system_name,
            "Address":                 t.address or "",
            "Postal Code":             t.zip_code or "",
            "Phone":                   t.phone or "",
            "Active":                  "Yes" if t.is_active else "No",
        })
    return out.getvalue()


_VALID_COMMERCIAL = frozenset({"yes", "no", "limited"})
_VALID_ACTIVE     = frozenset({"yes", "no", "true", "false", "1", "0", "inactive"})


def import_theaters_from_csv_str(csv_text: str) -> dict:
    """
    Upsert theaters from a CSV string.

    Uses the same column layout as export_theaters_csv() / imax_theaters.csv.
    Match priority: venue_key > exact name > case-insensitive name > insert new.
    Non-empty CSV fields overwrite DB values; empty fields are left unchanged.
    Returns {"inserted": N, "updated": N, "skipped": N, "errors": [], "warnings": []}.
    """
    import csv as _csv
    import io
    import urllib.parse

    def _normalise_ar(raw: str) -> str:
        if not raw:
            return raw
        return re.sub(r":0+(\d)$", r":\1", raw.strip())

    inserted = updated = skipped = 0
    errors: list[str] = []
    warnings: list[str] = []
    processed = 0

    reader = _csv.DictReader(io.StringIO(csv_text))

    # ── Header validation ──────────────────────────────────────────────
    if not reader.fieldnames:
        return {"inserted": 0, "updated": 0, "skipped": 0,
                "errors": ["File is empty or has no header row"], "warnings": []}
    headers = set(reader.fieldnames)
    if "Location Name" not in headers:
        return {
            "inserted": 0, "updated": 0, "skipped": 0,
            "errors": ["Missing required column 'Location Name'. "
                       "Is this a valid IMAX Alert theater CSV?"],
            "warnings": [],
        }
    known = set(_EXPORT_FIELDS) | {"max AR (Digital)"}
    unknown_cols = headers - known - {"Location Name"}
    if unknown_cols:
        warnings.append(f"Unrecognized column(s) ignored: {', '.join(sorted(unknown_cols))}")

    for row in reader:
        try:
            location_name = (row.get("Location Name") or "").strip()
            if not location_name:
                skipped += 1
                continue

            continent_name  = (row.get("Region") or "").strip()
            country_name    = (row.get("Country") or "").strip()
            state_name      = (row.get("State/Province") or "").strip()
            city_name       = (row.get("City") or "").strip()
            screen_ar_raw   = _normalise_ar(row.get("Screen AR") or "")
            digital_proj    = (row.get("Digital Projector") or "").strip()
            digital_ar_raw  = _normalise_ar(
                row.get(" max AR (Digital)") or row.get("max AR (Digital)") or ""
            )
            film_proj_raw   = (row.get("Film Projector") or "").strip()
            screen_dims_str = (row.get("Screen Dimensions") or "").strip()
            commercial      = (row.get("Commercial Films Shown") or "").strip() or None
            venue_key       = (row.get("Venue Key") or "").strip() or None
            chain_name      = (row.get("Chain") or "").strip() or None
            website_url     = (row.get("Website") or "").strip() or None
            audio_sys_name  = (row.get("Audio System") or "").strip() or None
            address         = (row.get("Address") or "").strip() or None
            postal_code     = (row.get("Postal Code") or "").strip() or None
            phone           = (row.get("Phone") or "").strip() or None
            active_str      = (row.get("Active") or "Yes").strip().lower()
            is_active       = active_str not in ("no", "false", "0", "inactive")

            # ── Per-row data validation ────────────────────────────────
            if website_url and not (
                website_url.startswith("http://") or website_url.startswith("https://")
            ):
                errors.append(
                    f"Row '{location_name}': Website '{website_url}' rejected — "
                    "must start with http:// or https://"
                )
                website_url = None

            if commercial and commercial.lower() not in _VALID_COMMERCIAL:
                errors.append(
                    f"Row '{location_name}': Commercial Films Shown '{commercial}' rejected — "
                    "must be Yes, No, or Limited"
                )
                commercial = None

            if active_str not in _VALID_ACTIVE:
                errors.append(
                    f"Row '{location_name}': Active '{active_str}' unrecognized — defaulting to Yes"
                )
                is_active = True

            continent_obj = get_or_create_continent(continent_name) if continent_name else None
            country_obj   = get_or_create_country(country_name) if country_name else None
            region_obj    = (
                get_or_create_region(state_name, country_obj)
                if state_name and country_obj else None
            )
            city_obj      = (
                get_or_create_city(city_name, country_obj, region_obj)
                if city_name and country_obj else None
            )
            ar_obj        = get_or_create_aspect_ratio(screen_ar_raw) if screen_ar_raw else None
            dig_proj_obj  = get_or_create_projector_type(digital_proj) if digital_proj else None
            dig_ar_obj    = get_or_create_aspect_ratio(digital_ar_raw) if digital_ar_raw else None
            film_pt_obj   = get_or_create_projector_type(film_proj_raw) if film_proj_raw else None
            chain_root    = None
            if website_url:
                _p = urllib.parse.urlparse(website_url)
                chain_root = f"{_p.scheme}://{_p.netloc}" if _p.netloc else None
            chain_obj     = get_or_create_chain(chain_name, website=chain_root or "") if chain_name else None
            audio_sys_obj = get_or_create_audio_system(audio_sys_name) if audio_sys_name else None
            w_m, h_m      = parse_screen_dims(screen_dims_str) if screen_dims_str else (None, None)

            t = None
            if venue_key:
                t = Theater.query.filter_by(venue_key=venue_key).first()
            if t is None:
                t = Theater.query.filter_by(name=location_name).first()
            if t is None:
                t = Theater.query.filter(
                    db.func.lower(Theater.name) == location_name.lower()
                ).first()

            if t is None:
                t = Theater(name=location_name, is_active=is_active, crawl_source="import")
                db.session.add(t)
                inserted += 1
            else:
                t.is_active = is_active
                updated += 1

            t.name = location_name
            if venue_key:
                t.venue_key = venue_key
            if country_name:
                t.country    = country_name
                t.country_id = country_obj.id if country_obj else t.country_id
            if state_name:
                t.state     = state_name
                t.region_id = region_obj.id if region_obj else t.region_id
            if city_name:
                t.city    = city_name
                t.city_id = city_obj.id if city_obj else t.city_id
            if screen_ar_raw:
                t.screen_size         = screen_ar_raw
                t.aspect_ratio_id     = ar_obj.id if ar_obj else t.aspect_ratio_id
            if digital_proj:
                t.projector_type       = digital_proj
                t.projector_type_id    = dig_proj_obj.id if dig_proj_obj else t.projector_type_id
            if digital_ar_raw:
                t.digital_projector_ar_id = dig_ar_obj.id if dig_ar_obj else t.digital_projector_ar_id
            if film_proj_raw:
                t.film_projector_type    = film_proj_raw
                t.film_projector_type_id = film_pt_obj.id if film_pt_obj else t.film_projector_type_id
            if screen_dims_str:
                t.screen_dims = screen_dims_str
            if commercial is not None:
                t.commercial_films = commercial
            if chain_name:
                t.chain    = chain_name
                t.chain_id = chain_obj.id if chain_obj else t.chain_id
            if website_url:
                t.website = website_url
            if audio_sys_name:
                t.audio_system    = audio_sys_name
                t.audio_system_id = audio_sys_obj.id if audio_sys_obj else t.audio_system_id
            if address:
                t.address = address
            if postal_code:
                t.zip_code = postal_code
            if phone:
                t.phone = phone
            if continent_obj:
                t.continent_id = continent_obj.id
            if w_m is not None:
                t.screen_width_m  = w_m
            if h_m is not None:
                t.screen_height_m = h_m

            processed += 1
            if processed % 50 == 0:
                db.session.flush()

        except Exception as exc:  # noqa: BLE001
            errors.append(f"Row '{row.get('Location Name', '?')}': {exc}")
            logger.warning("CSV import row error: %s", exc)

    try:
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        errors.append(f"DB commit failed: {exc}")
        return {"inserted": 0, "updated": 0, "skipped": skipped, "errors": errors, "warnings": warnings}

    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors, "warnings": warnings}
