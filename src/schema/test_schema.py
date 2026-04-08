"""
Test / example: List[CompositeType] support in the schema system.

Demonstrates that composite types can be used inside List[...] generics,
with full type parsing, defaults, and validation applied to each list item.

Run:
    python -m src.schema.test_schema
"""

from src.schema.schemas.read_schema import load_schema
from src.schema.parse_object import Parser


# -- Schema definition --------------------------------------------------------
# An "Event" with a list of date ranges extracted from unstructured text,
# a list of locations, and a single nested composite type.

EVENT_SCHEMA = {
    "Event": {
        "meta": {"description": "An event with multiple dates and locations"},
        "schema": {
            "name":      {"type": "str", "required": True},
            "dates":     {"type": "List[DateRangeFromUnstructured]"},
            "locations": {"type": "List[LocationCoords]"},
            "main_location": {"type": "LocationCoords"},
        },
    }
}


def test_list_of_composite_types():
    """List[CompositeType] items are fully parsed (types, defaults, validation)."""

    loaded = load_schema(EVENT_SCHEMA)
    schemas = loaded["schemas"]

    # Composite types and their transitive deps are auto-resolved
    assert "DateRangeFromUnstructured" in schemas
    assert "PeriodDates" in schemas  # transitive dep of DateRangeFromUnstructured
    assert "LocationCoords" in schemas

    parser = Parser(schemas)

    raw = {
        "name": "Festival de Música",
        "dates": [
            {
                "date_range": {"start": "2026-06-01", "end": "2026-06-03"},
                "timezone": "America/Mexico_City",
                "mention": "del 1 al 3 de junio",
                "precision_days": 3,
            },
            {
                "date_range": {"start": "2026-07-15", "end": None},
                "timezone": None,
                "mention": "mediados de julio",
                "precision_days": 15,
            },
        ],
        "locations": [
            {"lat": "19.4326", "lon": "-99.1332"},
            {"lat": "20.6597", "lon": "-103.3496"},
        ],
        "main_location": {"lat": "19.4326", "lon": "-99.1332"},
    }

    result = parser.normalize_record(raw, "Event")

    # -- List[DateRangeFromUnstructured] --
    assert len(result["dates"]) == 2

    # Datetime fields inside each item are parsed
    from datetime import datetime
    d0 = result["dates"][0]
    assert isinstance(d0["date_range"]["start"], datetime)
    assert isinstance(d0["date_range"]["end"], datetime)
    assert d0["mention"] == "del 1 al 3 de junio"
    assert isinstance(d0["precision_days"], int)

    d1 = result["dates"][1]
    assert isinstance(d1["date_range"]["start"], datetime)
    assert d1["date_range"]["end"] is None  # null preserved
    assert d1["precision_days"] == 15

    # -- List[LocationCoords] --
    assert len(result["locations"]) == 2
    assert isinstance(result["locations"][0]["lat"], float)
    assert result["locations"][0]["lat"] == 19.4326
    assert isinstance(result["locations"][1]["lon"], float)

    # -- Single composite type still works --
    assert isinstance(result["main_location"]["lat"], float)

    print("All assertions passed.")
    print()
    print("Parsed result:")
    _print_result(result)


def test_empty_and_missing_lists():
    """Empty / missing lists default to []."""

    loaded = load_schema(EVENT_SCHEMA)
    parser = Parser(loaded["schemas"])

    result = parser.normalize_record({"name": "Empty event"}, "Event")

    assert result["dates"] == []
    assert result["locations"] == []
    # Single composite type gets its fields initialized (with None defaults)
    assert isinstance(result["main_location"], dict)
    assert "lat" in result["main_location"]
    assert "lon" in result["main_location"]

    print("Empty/missing list test passed.")


def test_list_of_primitive_types_unchanged():
    """Existing List[Url] and List[str] behaviour is unaffected."""

    schema = {
        "Article": {
            "meta": {},
            "schema": {
                "tags": {"type": "List[str]"},
                "urls": {"type": "List[Url]"},
            },
        }
    }

    loaded = load_schema(schema)
    parser = Parser(loaded["schemas"])

    result = parser.normalize_record(
        {"tags": ["politics", "mexico"], "urls": '["https://example.com"]'},
        "Article",
    )

    assert result["tags"] == ["politics", "mexico"]
    assert result["urls"] == ["https://example.com"]

    print("Primitive list types test passed.")


def _print_result(result, indent=0):
    prefix = "  " * indent
    if isinstance(result, dict):
        for k, v in result.items():
            if isinstance(v, (dict, list)):
                print(f"{prefix}{k}:")
                _print_result(v, indent + 1)
            else:
                print(f"{prefix}{k}: {v!r}")
    elif isinstance(result, list):
        for i, item in enumerate(result):
            print(f"{prefix}[{i}]:")
            _print_result(item, indent + 1)
    else:
        print(f"{prefix}{result!r}")


if __name__ == "__main__":
    test_list_of_composite_types()
    print()
    test_empty_and_missing_lists()
    print()
    test_list_of_primitive_types_unchanged()
