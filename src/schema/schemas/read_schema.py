from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union


from src.schema.types.registry import resolve_type_string, extract_list_object_type
from src.schema.types.composite_types import COMPOSITE_TYPES, COMPOSITE_META


def load_schema(
    source: Union[str, Path, dict],
    callables: Optional[Dict[str, Callable]] = None,
) -> Dict[str, Any]:
    """Load a JSON schema definition and convert it to the Python dict format
    that Parser consumes.

    Composite type dependencies (e.g. LocationCoords, DateRangeFromUnstructured)
    are auto-resolved from types/composite_types.json.

    Args:
        source: JSON file path (str/Path) or already-parsed dict.
        callables: Maps function names (used in "default_fn" / "required_fn")
                   to Python callables.

    Returns:
        {"schemas": {type_name: field_specs_dict, ...},
         "meta":    {type_name: meta_dict, ...}}
    """
    callables = callables or {}

    if isinstance(source, (str, Path)):
        with open(source, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = source

    schemas: Dict[str, Dict[str, Dict[str, Any]]] = {}
    meta: Dict[str, Dict[str, Any]] = {}

    for type_name, type_def in raw.items():
        meta[type_name] = type_def.get("meta", {})
        schemas[type_name] = _resolve_fields(type_def["schema"], callables)

    _resolve_composite_dependencies(schemas, meta)

    return {"schemas": schemas, "meta": meta}


def _resolve_fields(
    fields: Dict[str, Dict[str, Any]],
    callables: Dict[str, Callable],
) -> Dict[str, Dict[str, Any]]:
    """Convert a JSON fields dict into the field-spec format expected by Parser."""
    resolved: Dict[str, Dict[str, Any]] = {}

    for field_name, spec in fields.items():
        resolved[field_name] = _resolve_field_spec(spec, callables)

    return resolved


def _resolve_field_spec(
    spec: Dict[str, Any],
    callables: Dict[str, Callable],
) -> Dict[str, Any]:
    """Resolve a single field spec: convert type string, resolve callable refs."""
    out: Dict[str, Any] = {}

    for key, value in spec.items():
        if key == "type":
            out["type"] = resolve_type_string(value)

        elif key == "default_fn":
            fn = callables.get(value)
            if fn is None:
                raise KeyError(
                    f"default_fn '{value}' not found in callables registry"
                )
            out["default"] = fn

        elif key == "required_fn":
            fn = callables.get(value)
            if fn is None:
                raise KeyError(
                    f"required_fn '{value}' not found in callables registry"
                )
            out["required"] = fn

        else:
            out[key] = value

    return out


def _resolve_composite_dependencies(
    schemas: Dict[str, Dict[str, Dict[str, Any]]],
    meta: Dict[str, Dict[str, Any]],
) -> None:
    """Pull in composite types referenced by fields but not defined locally.

    Walks all schemas, finds string type references not yet in the schemas dict,
    and includes them from COMPOSITE_TYPES. Recurses to handle transitive
    dependencies (e.g. DateRangeFromUnstructured → PeriodDates).
    """
    to_check = set(schemas.keys())
    resolved = set(schemas.keys())

    while to_check:
        current = to_check.pop()
        for spec in schemas[current].values():
            field_type = spec.get("type")
            if not isinstance(field_type, str):
                continue

            # Direct object reference
            type_name = field_type
            # Or element type inside List[ObjectType]
            element = extract_list_object_type(field_type)
            if element:
                type_name = element

            if type_name not in resolved and type_name in COMPOSITE_TYPES:
                schemas[type_name] = COMPOSITE_TYPES[type_name]
                meta[type_name] = COMPOSITE_META.get(type_name, {})
                resolved.add(type_name)
                to_check.add(type_name)
