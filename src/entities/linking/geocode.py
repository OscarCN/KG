"""Geocoder wrapper for structured Location dicts.

Wraps the apify_client geocoder
(`/Users/oscarcuellar/ocn/media/apify_client/src/helpers/geocode.py`),
feeding it the structured-input path of `format_mentions` (which short-
circuits the NLP step when its `main` argument is already a dict).

Location fields → geocoder level keys (per `levels` in geocode.py):

    country      → PAIS  (level 1)
    state        → EST   (level 2)
    city         → MUN   (level 3)
    neighborhood → COL   (level 5)   (zone is appended here too)
    zone         → COL   (level 5)
    street (+ number) → CALLE (level 6)
    place_name   → LUG   (level 7)

Returns a single best-match dict for context group '1' or None when the
geocoder can't resolve the location.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The apify_client package lives outside this repo. We add its src/ to
# sys.path so that `from helpers.geocode import geocode` resolves.
_APIFY_SRC = os.environ.get(
    "APIFY_CLIENT_SRC",
    "/Users/oscarcuellar/ocn/media/apify_client/src",
)
if _APIFY_SRC not in sys.path:
    sys.path.insert(0, _APIFY_SRC)

try:
    from helpers.geocode import geocode as _apify_geocode  # type: ignore
except Exception as ex:  # pragma: no cover
    logger.warning(
        "Could not import apify_client geocoder from %s: %s. "
        "Geocoding will be disabled.",
        _APIFY_SRC, ex,
    )
    _apify_geocode = None  # type: ignore


# Cache directory mirrors the extraction cache pattern.
_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache" / "geocode"


# Mapping from Location field name → geocoder level key.
_LEVEL_KEYS = ("PAIS", "EST", "MUN", "COL", "CALLE", "LUG")


def _normalize_location(loc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Strip whitespace and drop empty values from a Location dict."""
    if not isinstance(loc, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in loc.items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        out[k] = v
    return out


def _build_mentions(loc: Dict[str, Any]) -> Dict[str, List[Tuple[str, int]]]:
    """Build the structured-mentions dict for the geocoder's format_mentions.

    Each populated Location field becomes a `[(text, position)]` tuple list
    under the corresponding level key. Position is a monotonic counter
    (we have no real character offsets).
    """
    mentions: Dict[str, List[Tuple[str, int]]] = {k: [] for k in _LEVEL_KEYS}
    pos = 0

    def add(level_key: str, text: str) -> None:
        nonlocal pos
        if not text:
            return
        mentions[level_key].append((text, pos))
        pos += 1

    add("PAIS", loc.get("country") or "")
    add("EST", loc.get("state") or "")
    add("MUN", loc.get("city") or "")

    # Neighborhood + zone both fold into COL (level 5) — geocoder has no level-4 slot.
    add("COL", loc.get("neighborhood") or "")
    add("COL", loc.get("zone") or "")

    street = (loc.get("street") or "").strip()
    number = (loc.get("number") or "").strip()
    if street:
        add("CALLE", f"{street} {number}".strip())
    elif number:
        add("CALLE", number)

    add("LUG", loc.get("place_name") or "")
    return mentions


def _location_cache_key(loc: Dict[str, Any]) -> str:
    payload = json.dumps(loc, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, value: Optional[Dict[str, Any]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)


def _pick_best_match(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the most precise match from the geocoder's response list."""
    if not matches:
        return None
    # Highest precision_level wins; ties broken by first occurrence.
    return max(
        matches,
        key=lambda m: int(m.get("precision_level") or 0),
    )


def _normalize_response(match: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the geocoder response into our linker-friendly shape."""
    coords = match.get("coords") or {}
    try:
        precision = int(match.get("precision_level") or 0)
    except (TypeError, ValueError):
        precision = 0
    return {
        "geoid": match.get("geoid") or "",
        "precision_level": precision,
        "formatted_name": match.get("formatted_name") or "",
        "level_1": match.get("level_1") or "",
        "level_2": match.get("level_2") or "",
        "level_3": match.get("level_3") or "",
        "level_5": match.get("level_5") or "",
        "level_7": match.get("level_7") or "",
        "matched_lat": coords.get("lat"),
        "matched_lon": coords.get("lon"),
    }


def geocode_location(
    location: Optional[Dict[str, Any]],
    use_cache: bool = True,
) -> Optional[Dict[str, Any]]:
    """Geocode a structured Location dict and return a normalized result.

    Returns None when the location is empty, the geocoder is unavailable,
    or no match is returned.
    """
    loc = _normalize_location(location)
    if not loc:
        return None

    cache_key = _location_cache_key(loc)
    if use_cache:
        cached = _cache_read(cache_key)
        if cached is not None:
            # Cached `null` (no match) is stored as a JSON null → loaded as None.
            return cached or None

    if _apify_geocode is None:
        return None

    mentions = _build_mentions(loc)
    if not any(mentions.values()):
        if use_cache:
            _cache_write(cache_key, None)
        return None

    try:
        response = _apify_geocode(mentions)
    except Exception as ex:
        logger.warning("Geocoder call failed for %s: %s", loc, ex)
        return None

    if not isinstance(response, dict) or "error" in response:
        if use_cache:
            _cache_write(cache_key, None)
        return None

    matches = response.get("1", []) or []
    best = _pick_best_match(matches)
    if not best:
        if use_cache:
            _cache_write(cache_key, None)
        return None

    result = _normalize_response(best)
    if use_cache:
        _cache_write(cache_key, result)
    return result
