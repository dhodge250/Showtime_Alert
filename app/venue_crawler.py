"""
Theater geocoding utilities.

Geocodes Theater rows missing coordinates via the Nominatim API (OpenStreetMap).
CSV export/import/re-seed helpers live in app.theater_csv; the CSV in
seeds/imax_theaters.csv is the source of truth for theater data — this
module no longer includes a venue-list crawler.

Geocoding rate limit
--------------------
Nominatim's usage policy requires <=1 request/second and a descriptive
User-Agent. A 1.1-second delay is enforced between geocode calls.
"""
import logging
import re
import time
from datetime import datetime, timezone

import requests

from app import db
from app.models import Theater

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

