"""
Shared get-or-create helpers for lookup tables.

Used by both the startup seeder (_seed_lookup_tables in __init__.py)
and the CSV import/re-seed helpers (venue_crawler.py) so both write FK
columns consistently without duplicating logic.
"""
import re

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    AspectRatio,
    AudioSystem,
    Chain,
    City,
    Continent,
    Country,
    ProjectorType,
    Region,
)


def _get_or_create(model, filters, defaults):
    """
    Generic get-or-create that handles IntegrityError from concurrent inserts.

    *filters* is a list of SQLAlchemy filter expressions.
    *defaults* is a dict of kwargs passed to model() on creation.
    Returns the existing or newly-created row.
    """
    obj = model.query.filter(*filters).first()
    if obj:
        return obj
    try:
        obj = model(**defaults)
        db.session.add(obj)
        db.session.flush()
        return obj
    except IntegrityError:
        db.session.rollback()
        # Another insert raced us — fetch the now-existing row.
        return model.query.filter(*filters).first()


def get_or_create_continent(name: str) -> Continent:
    """Return the Continent row for *name*, creating it if absent."""
    name = (name or "").strip()
    if not name:
        return None
    return _get_or_create(
        Continent,
        [db.func.lower(Continent.name) == name.lower()],
        {"name": name},
    )


def get_or_create_chain(name: str, website: str = "") -> Chain:
    """Return the Chain row for *name*, creating it if absent."""
    name = (name or "").strip()
    if not name:
        return None
    obj = _get_or_create(
        Chain,
        [db.func.lower(Chain.name) == name.lower()],
        {"name": name, "website": website or ""},
    )
    if obj and website and not obj.website:
        obj.website = website
    return obj


def get_or_create_country(name: str) -> Country:
    """Return the Country row for *name*, creating it if absent."""
    name = (name or "").strip()
    if not name:
        return None
    return _get_or_create(
        Country,
        [db.func.lower(Country.name) == name.lower()],
        {"name": name},
    )


def get_or_create_region(name: str, country: Country) -> Region:
    """Return the Region row for (*name*, *country*), creating it if absent."""
    name = (name or "").strip()
    if not name or not country:
        return None
    return _get_or_create(
        Region,
        [
            db.func.lower(Region.name) == name.lower(),
            Region.country_id == country.id,
        ],
        {"name": name, "country_id": country.id},
    )


def get_or_create_city(
    name: str, country: Country, region: Region = None
) -> City:
    """Return the City row for (*name*, *country*, *region*), creating if absent."""
    name = (name or "").strip()
    if not name or not country:
        return None
    region_id = region.id if region else None
    return _get_or_create(
        City,
        [
            db.func.lower(City.name) == name.lower(),
            City.country_id == country.id,
            City.region_id == region_id,
        ],
        {"name": name, "country_id": country.id, "region_id": region_id},
    )


def get_or_create_aspect_ratio(label: str) -> AspectRatio:
    """Return the AspectRatio row for *label*, creating it if absent."""
    label = (label or "").strip()
    if not label:
        return None
    return _get_or_create(
        AspectRatio,
        [db.func.lower(AspectRatio.label) == label.lower()],
        {"label": label},
    )


def get_or_create_projector_type(name: str) -> ProjectorType:
    """Return the ProjectorType row for *name*, creating it if absent."""
    name = (name or "").strip()
    if not name:
        return None
    return _get_or_create(
        ProjectorType,
        [db.func.lower(ProjectorType.name) == name.lower()],
        {"name": name},
    )


def get_or_create_audio_system(name: str) -> AudioSystem:
    """Return the AudioSystem row for *name*, creating it if absent."""
    name = (name or "").strip()
    if not name:
        return None
    return _get_or_create(
        AudioSystem,
        [db.func.lower(AudioSystem.name) == name.lower()],
        {"name": name},
    )


def parse_screen_dims(dims_str: str):
    """
    Parse a screen dimensions string into (width_m, height_m).

    Accepts formats like::

        "26.0m×18.0m"
        "26.0 m × 18.0 m"
        "85.3ft×59.1ft"
        "85.3 ft × 59.1 ft"
        "26.0×18.0"   (assumes metres)

    Returns (width_m, height_m) as floats, or (None, None) if unparseable.
    Converts feet to metres when the unit is ft/feet/foot.
    """
    if not dims_str:
        return None, None
    dims_str = dims_str.strip()
    # Normalise separator (× U+00D7, x, X, by, *)
    normalised = re.sub(r"\s*[×xX\*]|by\s*", "×", dims_str)
    parts = normalised.split("×")
    if len(parts) != 2:
        return None, None

    def _parse_part(s):
        """Parse a single dimension token into (value, unit)."""
        s = s.strip()
        match = re.match(r"([\d.]+)\s*(m|ft|feet|foot)?", s, re.IGNORECASE)
        if not match:
            return None, None
        val = float(match.group(1))
        unit = (match.group(2) or "m").lower()
        return val, unit

    w_val, w_unit = _parse_part(parts[0])
    h_val, h_unit = _parse_part(parts[1])
    if w_val is None or h_val is None:
        return None, None

    ft_units = {"ft", "feet", "foot"}
    if w_unit in ft_units:
        w_val = round(w_val / 3.28084, 4)
    if h_unit in ft_units:
        h_val = round(h_val / 3.28084, 4)

    return w_val, h_val
