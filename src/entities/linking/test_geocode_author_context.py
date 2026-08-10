"""Author-location context (`context_group` 2) and its degradation guard.

The context group is meant to anchor an otherwise anchor-less location. When the
author's declared location CONFLICTS with the event's (a Tijuana account posting
about CDMX flooding), the collective matcher can return a match that is real but
coarser than the location's own admin anchors — the failure mode measured on the
fb/x corpus on 2026-08-10 (1 degradation, 0 improvements in 18 cases; see
`docs/todos/author_context_geocoding_rollout.md`).

Both calls bypass the on-disk cache, and `_geocode_request` is faked, so these
tests never touch the geocoder service.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import src.entities.linking.geocode as geo

# The Tijuana/Tlalpan case, verbatim from the 2026-08-10 A/B run.
TIJUANA_AUTHOR = {
    "level_2": "Baja California", "level_3": "Tijuana",
    "precision_level": 3, "geoid": "_48402004",
}
CDMX_LOCATION = {
    "city": "Ciudad de México", "state": "Ciudad de México",
    "place_name": "Tlalpan", "country": "México",
}

_P7 = {"geoid": "_48409012001", "precision_level": 7,
       "formatted_name": "tlalpan, DE TLALPAN, Coyoacan, Distrito Federal",
       "coords": {"lat": 19.29, "lon": -99.16}}
_P2 = {"geoid": "_48409", "precision_level": 2,
       "formatted_name": "Distrito Federal, Mexico", "coords": {"lat": 19.4, "lon": -99.1}}


def _fake_request(bare: Dict[str, Any], with_context: Dict[str, Any]):
    """Fake `_geocode_request`: answer differently with and without context."""
    calls: List[bool] = []

    def _request(mentions, extra_mentions: Optional[List[Dict[str, Any]]] = None):
        has_context = bool(extra_mentions)
        calls.append(has_context)
        return {"1": [with_context if has_context else bare]}

    _request.calls = calls  # type: ignore[attr-defined]
    return _request


def test_context_degradation_falls_back_to_bare(monkeypatch):
    """Context returns a real but coarser match → keep the bare one."""
    request = _fake_request(bare=_P7, with_context=_P2)
    monkeypatch.setattr(geo, "_geocode_request", request)

    result = geo.geocode_location(
        CDMX_LOCATION, use_cache=False, author_geo=TIJUANA_AUTHOR
    )

    assert result["precision_level"] == 7
    assert result["geoid"] == _P7["geoid"]
    # The bare result is not the context result, so it must not claim otherwise.
    assert "_author_context_used" not in result
    # Context attempt first, then the bare retry.
    assert request.calls == [True, False]


def test_context_improvement_is_kept(monkeypatch):
    """Context that helps (the reason the feature exists) survives the guard."""
    request = _fake_request(bare=_P2, with_context=_P7)
    monkeypatch.setattr(geo, "_geocode_request", request)

    result = geo.geocode_location(
        CDMX_LOCATION, use_cache=False, author_geo=TIJUANA_AUTHOR
    )

    assert result["precision_level"] == 7
    assert result["_author_context_used"] is True
    assert request.calls == [True]  # no retry needed


def test_no_retry_when_anchors_are_satisfied(monkeypatch):
    """A p3 result with only a municipality anchor is not a degradation."""
    municipality = {"geoid": "_48409012", "precision_level": 3,
                    "formatted_name": "Coyoacan, Distrito Federal",
                    "coords": {"lat": 19.35, "lon": -99.16}}
    request = _fake_request(bare=municipality, with_context=municipality)
    monkeypatch.setattr(geo, "_geocode_request", request)

    result = geo.geocode_location(
        {"city": "Coyoacán", "state": "Ciudad de México"},
        use_cache=False, author_geo=TIJUANA_AUTHOR,
    )

    assert result["precision_level"] == 3
    assert result["_author_context_used"] is True
    assert request.calls == [True]


def test_anchor_floor_ignores_colonia_and_street():
    """Only EST/MUN count — a KB-missing colonia resolving to its municipality
    must not trigger a retry on every record."""
    assert geo._anchor_floor(geo._build_mentions({"state": "Ciudad de México"})) == 2
    assert geo._anchor_floor(geo._build_mentions(
        {"state": "Ciudad de México", "city": "Coyoacán"})) == 3
    assert geo._anchor_floor(geo._build_mentions(
        {"neighborhood": "Del Valle", "street": "Insurgentes",
         "place_name": "Parque Hundido"})) == 0
