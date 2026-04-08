from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from src.schema.types.string_helpers import _is_valid_url
from .read_schema import load_schema


news_types = ["news", "X", "Facebook", "impreso", "Instagram", "Radio", "TV"]


def date_now(*args, **kwargs) -> datetime:
    return datetime.now(tz=ZoneInfo("America/Mexico_City"))


def default_timestamp_added(obj: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> str:
    """Default timestamp_added to current execution time"""
    return datetime.now(tz=ZoneInfo("America/Mexico_City")).isoformat()


def default_source_extra_found_source(obj: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> bool:
    """Default __FOUND_SOURCE__ flag"""
    return obj.get("__FOUND_SOURCE__") is not None


def require_url(obj: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> bool:
    if obj['message'].get("type", "").lower() != "impreso":
        return _is_valid_url(obj['message'].get("url"))
    return True


_CALLABLES = {
    "date_now": date_now,
    "default_source_extra_found_source": default_source_extra_found_source,
    "require_url": require_url,
}

_loaded = load_schema(
    Path(__file__).parent / "news.json",
    callables=_CALLABLES,
)

NEWS_SCHEMA = _loaded["schemas"]["News"]
SOURCE_EXTRA_SCHEMA = _loaded["schemas"]["SourceExtra"]
SOURCE_EXTRA_STATS_SCHEMA = _loaded["schemas"]["SourceExtraStats"]
SUPPLIER_SCHEMA = _loaded["schemas"]["Supplier"]
MESSAGE_WRAPPER_SCHEMA = _loaded["schemas"]["MessageWrapper"]

__all__ = [
    "NEWS_SCHEMA",
    "SOURCE_EXTRA_SCHEMA",
    "SOURCE_EXTRA_STATS_SCHEMA",
    "SUPPLIER_SCHEMA",
    "MESSAGE_WRAPPER_SCHEMA",
    "default_timestamp_added",
    "default_source_extra_found_source",
]
