from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .registry import resolve_type_string


def _load_composite_types() -> Dict[str, Any]:
    """Load composite_types.json and resolve type strings to Python types."""
    path = Path(__file__).parent / "composite_types.json"
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    schemas: Dict[str, Dict[str, Dict[str, Any]]] = {}
    meta: Dict[str, Dict[str, Any]] = {}

    for type_name, type_def in raw.items():
        meta[type_name] = type_def.get("meta", {})
        schemas[type_name] = {
            field_name: {
                k: resolve_type_string(v) if k == "type" else v
                for k, v in spec.items()
            }
            for field_name, spec in type_def["schema"].items()
        }

    return schemas, meta


COMPOSITE_TYPES, COMPOSITE_META = _load_composite_types()
