from __future__ import annotations

from typing import Any, Dict, Optional
import re
from datetime import datetime
from pathlib import Path

from src.schema.types.string_helpers import _is_null
from .read_schema import load_schema


def default_sitio_from_domain(domain: Optional[str]) -> Optional[str]:
    if not domain or not isinstance(domain, str):
        return None
    return re.sub(r"\..*(\..*)?", "", domain.strip())

def default_tier(website_visits: Optional[int]) -> Optional[str]:
    if _is_null(website_visits):
        return None
    if website_visits < 15000:
        return 3
    elif website_visits < 30000:
        return 2
    else:
        return 1

def default_valuacion(website_visits: Optional[int]) -> Optional[str]:
    if _is_null(website_visits):
        return None
    if website_visits < 1000:
        return 5000
    elif website_visits < 10000:
        return 10000
    elif website_visits < 20000:
        return 15000
    elif website_visits < 50000:
        return 20000
    elif website_visits < 100000:
        return 25000
    elif website_visits < 250000:
        return 30000
    elif website_visits < 1000000:
        return 40000
    else:
        return 50000

def date_now(*args, **kwargs) -> datetime:
    return datetime.now()


_CALLABLES = {
    "date_now": date_now,
    "default_sitio_from_domain": lambda obj, ctx: default_sitio_from_domain(obj.get("domain")),
    "default_tier": lambda obj, ctx: default_tier(obj.get("stats", {}).get("website_visits")),
    "default_valuacion": lambda obj, ctx: default_valuacion(obj.get("stats", {}).get("website_visits")),
}

_loaded = load_schema(
    Path(__file__).parent / "source.json",
    callables=_CALLABLES,
)

SOURCE_SCHEMA = _loaded["schemas"]["Source"]
SOURCE_STATS_SCHEMA = _loaded["schemas"]["SourceStats"]
LOCATION_COORDS_SCHEMA = _loaded["schemas"]["LocationCoords"]  # auto-resolved from composite types

__all__ = ["SOURCE_SCHEMA", "SOURCE_STATS_SCHEMA", "LOCATION_COORDS_SCHEMA", "default_sitio_from_domain"]
