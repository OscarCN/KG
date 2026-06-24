"""Per-source accumulator (`_sources`) tests for GeoEventStrategy.

`create` then `merge` of a second source must produce a `_sources` list with
one entry per source, each carrying its OWN publication_date and news_type."""

from datetime import datetime

from src.entities.linking.strategy import DateWindow, GeoEventStrategy, PreparedEvent


class FakeIndex:
    def register(self, key, item_id):
        pass


def _prep(source_id, date_created, news_type):
    record = {
        "_source_id": source_id,
        "event_type": "accidente_vehicular_event",
        "_supertype": "accidente_event",
        "date_created": date_created,
        "news_type": news_type,
    }
    window = DateWindow(
        start=datetime.fromisoformat(date_created),
        end=datetime.fromisoformat(date_created),
        slack_days=2,
        source="publication",
    )
    return PreparedEvent(record, "accidente_vehicular_event", "noloc", window, partition="accidente_event")


def test_create_then_merge_accumulates_per_source():
    strat = GeoEventStrategy(geocode=False)
    idx = FakeIndex()

    p1 = _prep("src-A", "2026-01-01T00:00:00", "ElUniversal")
    _eid, linked = strat.create(p1, idx)

    sources = linked.get("_sources")
    assert sources is not None
    assert len(sources) == 1
    assert sources[0]["source_id"] == "src-A"
    assert sources[0]["publication_date"] == "2026-01-01T00:00:00"
    assert sources[0]["news_type"] == "ElUniversal"

    p2 = _prep("src-B", "2026-02-15T00:00:00", "Milenio")
    strat.merge(linked, p2, idx)

    sources = linked["_sources"]
    by_id = {s["source_id"]: s for s in sources}
    assert set(by_id) == {"src-A", "src-B"}
    assert by_id["src-A"]["news_type"] == "ElUniversal"
    assert by_id["src-A"]["publication_date"] == "2026-01-01T00:00:00"
    assert by_id["src-B"]["news_type"] == "Milenio"
    assert by_id["src-B"]["publication_date"] == "2026-02-15T00:00:00"


def test_merge_dedupes_by_source_id():
    strat = GeoEventStrategy(geocode=False)
    idx = FakeIndex()
    p1 = _prep("src-A", "2026-01-01T00:00:00", "ElUniversal")
    _eid, linked = strat.create(p1, idx)
    # Same source again — should not duplicate.
    strat.merge(linked, _prep("src-A", "2026-01-01T00:00:00", "ElUniversal"), idx)
    assert len(linked["_sources"]) == 1
