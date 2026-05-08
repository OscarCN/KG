"""Schema loading and normalization helpers for linking_gpt."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Optional

from src.schema.parse_object import Parser
from src.schema.schemas.read_schema import load_schema

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "extraction" / "schemas"
_schema_cache: dict[str, dict[str, Any]] = {}


def snake_to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def get_schema(supertype: str) -> Optional[dict[str, Any]]:
    if supertype not in _schema_cache:
        path = _SCHEMAS_DIR / f"{supertype}.json"
        if not path.exists():
            return None
        _schema_cache[supertype] = load_schema(path)
    return _schema_cache[supertype]


def category_for(supertype: str) -> str:
    loaded = get_schema(supertype)
    if not loaded:
        return "event"
    schema_key = snake_to_pascal(supertype)
    return loaded.get("meta", {}).get(schema_key, {}).get("category", "event")


def normalize_by_schema(raw: dict[str, Any], supertype: str) -> dict[str, Any]:
    loaded = get_schema(supertype)
    if not loaded:
        return copy.deepcopy(raw)
    schema_key = snake_to_pascal(supertype)
    parser = Parser(loaded["schemas"])
    try:
        return parser.normalize_record(raw, schema_key, raise_validation_error=False)
    except Exception as ex:
        logger.warning("Schema parse failed for %s: %s", supertype, ex)
        return copy.deepcopy(raw)
