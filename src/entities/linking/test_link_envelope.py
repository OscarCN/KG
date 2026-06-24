"""Regression: the linker envelope must preserve non-schema provenance fields.

`_normalize_envelope` strips fields that aren't part of the supertype schema
before running the schema Parser (which would otherwise drop them), then
re-attaches the provenance fields. `news_type` is such a provenance field — it
rides on the extracted record and must survive into the linked record so the
persistence layer can write a faithful per-source `entities_documents` row.
"""

from __future__ import annotations

from src.entities.linking.link import EntityLinker


def test_normalize_envelope_preserves_news_type():
    linker = EntityLinker(geocode=False)
    raw = {
        "_source_id": "https://example.com/a",
        "_supertype": "paid_mass_event",
        "date_created": "2026-06-23T11:00:00-06:00",
        "news_type": "article",
        "event_type": "concert",
        "name": "Festival",
        "description": "Un festival en el centro.",
    }
    record = linker._normalize_envelope(raw, "paid_mass_event")
    assert record.get("news_type") == "article"
    # the other provenance fields still ride through
    assert record.get("_source_id") == "https://example.com/a"
    assert record.get("date_created") == "2026-06-23T11:00:00-06:00"


def test_normalize_envelope_news_type_absent_is_fine():
    linker = EntityLinker(geocode=False)
    raw = {
        "_source_id": "https://example.com/b",
        "_supertype": "paid_mass_event",
        "date_created": "2026-06-23T11:00:00-06:00",
        "event_type": "concert",
        "name": "Festival",
        "description": "Un festival en el centro.",
    }
    record = linker._normalize_envelope(raw, "paid_mass_event")
    assert record.get("news_type") is None
