"""
Theater CSV upsert, export, and re-seed utilities.

One shared row-upsert implementation (`_upsert_theater_row`) backs both
callers:
  - startup seeding from seeds/imax_theaters.csv (`app._upsert_theaters_from_csv`,
    preserve_existing=True — never clobbers a website/zip_code/is_active that
    already has a value)
  - admin CSV upload (`import_theaters_from_csv_str`, preserve_existing=False —
    non-empty CSV fields always overwrite, and the Active column is honored)
"""
import csv as _csv
import io
import logging
import re
import urllib.parse
from pathlib import Path

from app import db
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
from app.models import Theater

logger = logging.getLogger(__name__)

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

_EXPORT_FIELDS = [
    "Region", "Country", "State/Province", "City", "Location Name",
    "Screen AR", "Digital Projector", " max AR (Digital)", "Film Projector",
    "Screen Dimensions", "Commercial Films Shown", "Venue Key", "Chain",
    "Website", "Audio System", "Address", "Postal Code", "Phone", "Active",
]

_VALID_COMMERCIAL = frozenset({"yes", "no", "limited"})
_VALID_ACTIVE     = frozenset({"yes", "no", "true", "false", "1", "0", "inactive"})


def _normalise_ar(raw: str) -> str:
    """Fix '2.30:01' → '2.30:1'."""
    if not raw:
        return raw
    return re.sub(r":0+(\d)$", r":\1", raw.strip())


def _upsert_theater_row(row, *, preserve_existing: bool, source: str,
                         errors: list, warnings: list) -> str:
    """
    Upsert a single Theater from one CSV row dict.

    Match priority: venue_key > exact name > case-insensitive name > insert new.

    preserve_existing=True (startup seed semantics): website/zip_code are only
    set when the existing row has none, is_active is never touched on an
    existing row, and crawl_source is always re-stamped with `source`.

    preserve_existing=False (admin import semantics): non-empty CSV fields
    always overwrite, the Active column controls is_active, and crawl_source
    is only stamped on insert (existing rows keep their prior source).

    Row-level validation problems are appended to `errors`/`warnings` rather
    than raised; the row is still upserted with the offending field cleared.

    Returns "inserted", "updated", or "skipped".
    """
    location_name = (row.get("Location Name") or "").strip()
    if not location_name:
        return "skipped"

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

    # ── Per-row data validation ────────────────────────────────────────
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

    # ── Find existing theater ───────────────────────────────────────────
    t = None
    if venue_key:
        t = Theater.query.filter_by(venue_key=venue_key).first()
    if t is None:
        # Exact match first so Unicode names (e.g. Turkish İ) that SQLite
        # lower() can't fold still match correctly after the first upsert
        # stores them verbatim.
        t = Theater.query.filter_by(name=location_name).first()
    if t is None:
        t = Theater.query.filter(
            db.func.lower(Theater.name) == location_name.lower()
        ).first()

    if t is None:
        t = Theater(
            name=location_name,
            is_active=True if preserve_existing else is_active,
            crawl_source=source,
        )
        db.session.add(t)
        status = "inserted"
    else:
        if not preserve_existing:
            t.is_active = is_active
        status = "updated"

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
        t.screen_size     = screen_ar_raw
        t.aspect_ratio_id = ar_obj.id if ar_obj else t.aspect_ratio_id
    if digital_proj:
        t.projector_type    = digital_proj
        t.projector_type_id = dig_proj_obj.id if dig_proj_obj else t.projector_type_id
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
    if website_url and (not preserve_existing or not t.website):
        t.website = website_url
    if audio_sys_name:
        t.audio_system    = audio_sys_name
        t.audio_system_id = audio_sys_obj.id if audio_sys_obj else t.audio_system_id
    if address:
        t.address = address
    if postal_code and (not preserve_existing or not t.zip_code):
        t.zip_code = postal_code
    if phone:
        t.phone = phone
    if continent_obj:
        t.continent_id = continent_obj.id
    if w_m is not None:
        t.screen_width_m  = w_m
    if h_m is not None:
        t.screen_height_m = h_m

    if preserve_existing:
        t.crawl_source = source

    return status


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

def export_theaters_csv() -> str:
    """Return all theaters as a CSV string compatible with import_theaters_from_csv_str."""
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


def import_theaters_from_csv_str(csv_text: str) -> dict:
    """
    Upsert theaters from a CSV string (admin CSV upload).

    Uses the same column layout as export_theaters_csv() / imax_theaters.csv.
    Match priority: venue_key > exact name > case-insensitive name > insert new.
    Non-empty CSV fields overwrite DB values; empty fields are left unchanged.
    Returns {"inserted": N, "updated": N, "skipped": N, "errors": [], "warnings": []}.
    """
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
            status = _upsert_theater_row(
                row, preserve_existing=False, source="import",
                errors=errors, warnings=warnings,
            )
            if status == "inserted":
                inserted += 1
            elif status == "updated":
                updated += 1
            else:
                skipped += 1

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
