"""Tests for `_coalesce_location_list` (extract.py): the extraction prompts allow
`location` to be a LIST for one coordinated multi-site event, but the schema types
it as a single Location and the Parser silently nulls any non-dict value. The
coalescer keeps the common administrative prefix, preserves the full list as
`_locations`, and `_validate_all_entities` carries `_locations` through
normalization (found 2026-08-30: CDMX marathon closures lost all geo)."""

from src.entities.extraction.extract import (
    _coalesce_location_list,
    _validate_all_entities,
)


def _loc(**over):
    base = {
        "country": "México", "state": "Ciudad de México", "city": "Ciudad de México",
        "neighborhood": None, "zone": None, "street": None, "number": None,
        "place_name": None,
    }
    base.update(over)
    return base


def test_multi_site_list_keeps_common_prefix_and_nulls_divergent_fields():
    entity = {"location": [_loc(street="Av. Insurgentes"), _loc(street="Paseo de la Reforma")]}
    _coalesce_location_list(entity)
    loc = entity["location"]
    assert loc["country"] == "México"
    assert loc["state"] == "Ciudad de México"
    assert loc["city"] == "Ciudad de México"
    assert loc["street"] is None  # divergent → null, never a fabricated pick
    assert entity["_locations"] == [
        _loc(street="Av. Insurgentes"), _loc(street="Paseo de la Reforma"),
    ]


def test_single_element_list_unwraps_without_loss():
    entity = {"location": [_loc(street="Av. Insurgentes")]}
    _coalesce_location_list(entity)
    assert entity["location"] == _loc(street="Av. Insurgentes")
    assert entity["_locations"] == [_loc(street="Av. Insurgentes")]


def test_all_null_or_empty_list_becomes_none_without_locations():
    for raw in ([], [{"country": None, "state": None}]):
        entity = {"location": raw}
        _coalesce_location_list(entity)
        assert entity["location"] is None
        assert "_locations" not in entity


def test_dict_and_missing_location_untouched():
    entity = {"location": _loc()}
    _coalesce_location_list(entity)
    assert entity["location"] == _loc()
    assert "_locations" not in entity
    entity = {"name": "ev"}
    _coalesce_location_list(entity)
    assert "location" not in entity


def test_locations_survives_schema_normalization():
    entity = {
        "event_type": "closure",
        "status": "ongoing",
        "name": "Cierres por Maratón",
        "description": "Cortes a la circulación en varias avenidas.",
        "date_range": {
            "date_range": {"start": "2026-08-30T05:00:00", "end": None},
            "timezone": None, "mention": "este domingo", "precision_days": None,
        },
        "location": [_loc(street="Av. Insurgentes"), _loc(street="Paseo de la Reforma")],
        "cause": "Maratón CDMX",
        "_source_id": "https://example.com/nota",
        "_supertype": "closures_interruptions_event",
        "date_created": "2026-08-30T09:00:00-06:00",
    }
    _coalesce_location_list(entity)
    [out] = _validate_all_entities(
        [entity], "closures_interruptions_event", raise_validation_error=False,
    )
    assert out["location"]["city"] == "Ciudad de México"  # the Parser kept the merge
    assert len(out["_locations"]) == 2                    # full list rode through
    assert out["_source_id"] == "https://example.com/nota"
