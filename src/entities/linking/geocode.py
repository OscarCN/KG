"""Geocoder wrapper for structured Location dicts.

Thin client for deepriver's geocoder microservice: builds the mention list
from an already-structured Location dict (no NLP step needed) and POSTs it
to `GEOCODING_URL`.

Location fields → geocoder level keys:

    country      → PAIS  (level 1)
    state        → EST   (level 2)
    city         → MUN   (level 3)
    neighborhood → COL   (level 5)
    zone         → (not geocoded — a generic directional/functional area; see _build_mentions)
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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# Cache directory mirrors the extraction cache pattern.
_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache" / "geocode"


# Geocoder level key → precision level. Iteration order (most → least
# precise) also fixes mention_id assignment in the request payload.
_LEVELS = {"LUG": 7, "CALLE": 6, "COL": 5, "MUN": 3, "EST": 2, "PAIS": 1}
_LEVEL_KEYS = tuple(_LEVELS)

_GEOCODE_MAX_RETRIES = 3
_GEOCODE_RETRY_SLEEP = 5  # seconds between retries
_GEOCODE_TIMEOUT = 10  # seconds


def _mentions_payload(
    mentions: Dict[str, List[Tuple[str, int]]],
) -> List[Dict[str, Any]]:
    """Flatten level-keyed mentions into the geocoder's mention-dict list."""
    payload: List[Dict[str, Any]] = []
    for level_key in _LEVELS:
        for text, position in mentions.get(level_key, []):
            payload.append({
                "confidence": 0.6,
                "level": _LEVELS[level_key],
                "text": text,
                "position_in_text": position,
                "mention_id": len(payload),
                "context_group": 1,
            })
    return payload


def _geocode_request(
    mentions: Dict[str, List[Tuple[str, int]]],
    extra_mentions: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """POST the mentions to the geocoder microservice, with retries.

    Returns the parsed response dict (keyed by context group), or None when
    `GEOCODING_URL` is unset or the service stays unreachable.
    """
    url = os.environ.get("GEOCODING_URL")
    if not url:
        logger.warning("GEOCODING_URL not set; geocoding disabled.")
        return None

    payload = _mentions_payload(mentions)
    if extra_mentions:
        payload = payload + list(extra_mentions)
    arguments = {"mentions": payload}
    for attempt in range(1, _GEOCODE_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json=arguments,
                headers={"Content-Type": "application/json"},
                timeout=_GEOCODE_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except Exception as ex:
            logger.warning(
                "Geocoding attempt %d/%d failed: %s",
                attempt, _GEOCODE_MAX_RETRIES, ex,
            )
            if attempt < _GEOCODE_MAX_RETRIES:
                time.sleep(_GEOCODE_RETRY_SLEEP)
    logger.warning(
        "Geocoding service unavailable after %d attempts.", _GEOCODE_MAX_RETRIES
    )
    return None


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

    # Neighborhood (a named colonia/fraccionamiento) → COL (level 5). `zone` is
    # deliberately NOT geocoded: per the Location schema it's a generic directional
    # / functional area with no residential proper name ("zona norte", "corredor
    # industrial"), so the geocoder mis-matches it to a literal colonia of that name
    # (e.g. "sur" → colonia SUR, Sonora; "corredor industrial" → a colonia in
    # Tamaulipas), producing cross-state precision mismatches. It stays on the
    # extracted record, just isn't used for geocoding.
    add("COL", loc.get("neighborhood") or "")

    # The house number is deliberately NOT part of the CALLE mention: the
    # geocoder retrieves streets by normalized-name LSH, and "Calz. Tlalpan 136"
    # misses the buckets for "Calz. Tlalpan" entirely — a systematic L6 recall
    # killer (2026-07 kgdb repair: 25 events upgraded 5->6 just by dropping the
    # number). `number` stays on the record for display / address building.
    add("CALLE", (loc.get("street") or "").strip())

    add("LUG", loc.get("place_name") or "")
    return mentions


def _location_cache_key(loc: Dict[str, Any]) -> str:
    payload = json.dumps(loc, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _author_context_mentions(author_geo: Optional[Dict[str, Any]],
                             start_id: int) -> List[Dict[str, Any]]:
    """Author-location context mentions (SEPARATE context group, low confidence).

    The document author's declared location (ES `location_author`) gives the
    collective matcher an anchor when the extracted location has none — a local
    account posting "se inundó la colonia Del Valle" usually means its own city.
    Shape decided empirically (2026-08-08, geocoding repo
    `docs/todos/kg_social_cdmx_lluvias_geo_review.md` §3.4): mentions ride in
    `context_group` 2 — the geocoder's native `context` shape — NOT the event's
    group 1 (same-group perturbed correct matches in testing). State (level 2)
    and municipality (level 3) names only; author precision must be state or
    finer (a country-only author location anchors nothing).
    """
    if not isinstance(author_geo, dict):
        return []
    try:
        prec = int(author_geo.get("precision_level") or 0)
    except (TypeError, ValueError):
        prec = 0
    if prec < 2:
        return []
    out: List[Dict[str, Any]] = []
    for field, level in (("level_2", 2), ("level_3", 3)):
        text = (author_geo.get(field) or "").strip()
        if text:
            out.append({
                "confidence": 0.3,
                "level": level,
                "text": text,
                "position_in_text": len(out),
                "mention_id": start_id + len(out),
                "context_group": 2,
            })
    return out


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
    out: Dict[str, Any] = {
        "geoid": match.get("geoid") or "",
        "precision_level": precision,
        "formatted_name": match.get("formatted_name") or "",
        "matched_lat": coords.get("lat"),
        "matched_lon": coords.get("lon"),
    }
    # Retain the full admin hierarchy — both names and the hierarchical
    # `level_N_id`s (each a strict prefix of the next, e.g. `_484` ⊂ `_48422`
    # ⊂ `_48422016`). The ids are what the linker partitions on and mirror
    # kgdb `entity_locations.level_N_id`; dropping them (the prior behaviour)
    # forced the geo partition down to state-name only.
    for n in range(1, 8):
        out[f"level_{n}"] = match.get(f"level_{n}") or ""
        out[f"level_{n}_id"] = match.get(f"level_{n}_id") or ""
    return out


def geocode_location(
    location: Optional[Dict[str, Any]],
    use_cache: bool = True,
    author_geo: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Geocode a structured Location dict and return a normalized result.

    Returns None when the location is empty, the geocoder is unavailable,
    or no match is returned.

    `author_geo` (the document author's declared location, ES
    `location_author`) is used as CONTEXT — a separate low-confidence mention
    group the collective matcher can lean on. Evidence (2026-08-08 behavioral
    test, geocoding repo spec §3.4): the separate group changed nothing on 19/22
    already-anchored locations, rescued an anchored-but-unresolved street
    (prec 2 → 6), and its one failure mode (losing a match the bare location
    would find) is covered by the bare-call fallback below — so context is sent
    whenever the author location is state-or-finer, not only for anchor-less
    records.
    """
    loc = _normalize_location(location)
    if not loc:
        return None

    ctx_mentions: List[Dict[str, Any]] = (
        _author_context_mentions(author_geo, start_id=100) if author_geo else []
    )

    cache_key = _location_cache_key(
        {**loc, "_author_ctx": [m["text"] for m in ctx_mentions]}
        if ctx_mentions else loc
    )
    if use_cache:
        cached = _cache_read(cache_key)
        if cached is not None:
            # Cached `null` (no match) is stored as a JSON null → loaded as None.
            return cached or None

    mentions = _build_mentions(loc)
    if not any(mentions.values()):
        if use_cache:
            _cache_write(cache_key, None)
        return None

    response = _geocode_request(mentions, extra_mentions=ctx_mentions or None)
    if response is None:
        # Transient service failure — don't cache as a no-match.
        return None

    # The event's match is context group "1"; the author group ("2") is context
    # only and never read as a result.
    matches = response.get("1", []) or []
    best = _pick_best_match(matches)
    if not best and ctx_mentions:
        # Fallback: the context attempt found nothing — retry bare (its own
        # cache key), and memoize the resolved outcome under the context key.
        logger.info("author-context geocode found no match; falling back bare")
        result = geocode_location(location, use_cache=use_cache)
        if use_cache:
            _cache_write(cache_key, result)
        return result
    if not best:
        if use_cache:
            _cache_write(cache_key, None)
        return None

    result = _normalize_response(best)
    if ctx_mentions:
        result["_author_context_used"] = True
    if use_cache:
        _cache_write(cache_key, result)
    return result
