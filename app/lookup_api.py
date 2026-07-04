"""
Generic CRUD factory for lookup-table API endpoints.

Chain, Country, Region, City, AspectRatio, ProjectorType, AudioSystem, and
Continent all share the same GET/POST/DELETE/PATCH shape: list ordered by
name, create with a case-insensitive duplicate check, delete guarded by an
in-use check against Theater, and rename with a scoped duplicate check plus
an optional denormalized Theater.<legacy_col> sync. This module expresses
each table as a LookupSpec and registers one generic view per verb for all
of them, instead of ~150-line hand-written quartets per table.

Region and City scope uniqueness to a parent (country, and country+region
respectively) via scope_cols/scope_models/required_scope_cols: the first
required_scope_cols entry must be present on POST (validated with
get_or_404), any remaining scope_cols are optional (validated with a plain
.query.get, silently resolving to None if invalid — this replicates the
original city endpoint's lenient region_id handling).
"""
from dataclasses import dataclass

from flask import jsonify, request
from flask_login import login_required

from app import db
from app.auth import require_role
from app.models import (
    AspectRatio,
    AudioSystem,
    Chain,
    City,
    Continent,
    Country,
    ProjectorType,
    Region,
    Theater,
)


@dataclass
class LookupSpec:
    url: str
    model: type
    name_attr: str = "name"
    fk_col: str = ""
    legacy_col: str | None = None
    extra_fields: tuple = ()
    scope_cols: tuple = ()
    scope_models: tuple = ()
    required_scope_cols: tuple = ()
    patch_dup_message: str = "already exists"


_LOOKUPS: dict[str, LookupSpec] = {
    "chains": LookupSpec(
        url="chains", model=Chain, fk_col="chain_id", legacy_col="chain",
        extra_fields=("website",),
    ),
    "countries": LookupSpec(
        url="countries", model=Country, fk_col="country_id", legacy_col="country",
    ),
    "regions": LookupSpec(
        url="regions", model=Region, fk_col="region_id", legacy_col="state",
        scope_cols=("country_id",), scope_models=(Country,),
        required_scope_cols=("country_id",),
        patch_dup_message="already exists in this country",
    ),
    "cities": LookupSpec(
        url="cities", model=City, fk_col="city_id", legacy_col="city",
        scope_cols=("country_id", "region_id"), scope_models=(Country, Region),
        required_scope_cols=("country_id",),
        patch_dup_message="already exists in this region",
    ),
    "aspect-ratios": LookupSpec(
        url="aspect-ratios", model=AspectRatio, name_attr="label",
        fk_col="aspect_ratio_id", extra_fields=("description",),
    ),
    "projector-types": LookupSpec(
        url="projector-types", model=ProjectorType, fk_col="projector_type_id",
    ),
    "audio-systems": LookupSpec(
        url="audio-systems", model=AudioSystem, fk_col="audio_system_id",
    ),
    "continents": LookupSpec(
        url="continents", model=Continent, fk_col="continent_id",
    ),
}


def _make_get(spec: LookupSpec):
    def view():
        q = spec.model.query.order_by(getattr(spec.model, spec.name_attr))
        for col in spec.scope_cols:
            val = request.args.get(col, type=int)
            if val:
                q = q.filter_by(**{col: val})
        return jsonify([o.to_dict() for o in q.all()])
    return view


def _make_post(spec: LookupSpec):
    def view():
        data = request.get_json(silent=True) or {}
        name = (data.get(spec.name_attr) or "").strip()
        missing_required = [c for c in spec.required_scope_cols if not data.get(c)]
        if not name or missing_required:
            if spec.required_scope_cols:
                required_desc = " and ".join(spec.required_scope_cols)
                msg = f"{spec.name_attr} and {required_desc} are required"
            else:
                msg = f"{spec.name_attr} is required"
            return jsonify({"error": msg}), 400

        scope_values = {}
        for col, model_cls in zip(spec.scope_cols, spec.scope_models):
            raw = data.get(col)
            if col in spec.required_scope_cols:
                obj = model_cls.query.get_or_404(raw)
                scope_values[col] = obj.id
            else:
                obj = model_cls.query.get(raw) if raw else None
                scope_values[col] = obj.id if obj else None

        filters = [db.func.lower(getattr(spec.model, spec.name_attr)) == name.lower()]
        for col in spec.scope_cols:
            filters.append(getattr(spec.model, col) == scope_values[col])
        if spec.model.query.filter(*filters).first():
            return jsonify({"error": "already exists"}), 409

        obj = spec.model(**{spec.name_attr: name, **scope_values})
        for f in spec.extra_fields:
            setattr(obj, f, data.get(f, ""))
        db.session.add(obj)
        db.session.commit()
        return jsonify(obj.to_dict()), 201
    return view


def _make_delete(spec: LookupSpec):
    def view(obj_id):
        obj = spec.model.query.get_or_404(obj_id)
        if Theater.query.filter_by(**{spec.fk_col: obj_id}).first():
            return jsonify({"error": "In use by one or more theaters"}), 409
        db.session.delete(obj)
        db.session.commit()
        return jsonify({"deleted": True})
    return view


def _make_patch(spec: LookupSpec):
    def view(obj_id):
        obj = spec.model.query.get_or_404(obj_id)
        data = request.get_json(silent=True) or {}
        if spec.name_attr in data:
            name = (data[spec.name_attr] or "").strip()
            if not name:
                return jsonify({"error": f"{spec.name_attr} cannot be blank"}), 400
            filters = [
                db.func.lower(getattr(spec.model, spec.name_attr)) == name.lower(),
                spec.model.id != obj_id,
            ]
            for col in spec.scope_cols:
                filters.append(getattr(spec.model, col) == getattr(obj, col))
            if spec.model.query.filter(*filters).first():
                return jsonify({"error": spec.patch_dup_message}), 409
            setattr(obj, spec.name_attr, name)
            if spec.legacy_col:
                Theater.query.filter_by(**{spec.fk_col: obj_id}).update({spec.legacy_col: name})
        for f in spec.extra_fields:
            if f in data:
                setattr(obj, f, (data[f] or "").strip())
        db.session.commit()
        return jsonify(obj.to_dict())
    return view


def register_lookup_routes(bp):
    """Register GET/POST/DELETE/PATCH views for every table in _LOOKUPS on *bp*."""
    for key, spec in _LOOKUPS.items():
        base = f"/lookup/{spec.url}"
        item = f"{base}/<int:obj_id>"
        slug = key.replace("-", "_")

        bp.add_url_rule(
            base, endpoint=f"lookup_{slug}_get", methods=["GET"],
            view_func=login_required(_make_get(spec)),
        )
        bp.add_url_rule(
            base, endpoint=f"lookup_{slug}_post", methods=["POST"],
            view_func=require_role("admin", "editor")(_make_post(spec)),
        )
        bp.add_url_rule(
            item, endpoint=f"lookup_{slug}_delete", methods=["DELETE"],
            view_func=require_role("admin")(_make_delete(spec)),
        )
        bp.add_url_rule(
            item, endpoint=f"lookup_{slug}_patch", methods=["PATCH"],
            view_func=require_role("admin", "editor")(_make_patch(spec)),
        )
