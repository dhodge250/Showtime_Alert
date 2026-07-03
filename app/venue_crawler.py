"""
Theater geocoding and CSV import/export utilities.

Geocodes Theater rows missing coordinates via the Nominatim API (OpenStreetMap),
and provides CSV export/import/re-seed helpers used by the admin theater
management endpoints. The CSV in seeds/imax_theaters.csv is the source of
truth for theater data; this module no longer includes a venue-list crawler.

Geocoding rate limit
--------------------
Nominatim's usage policy requires <=1 request/second and a descriptive
User-Agent. A 1.1-second delay is enforced between geocode calls.
"""
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

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
