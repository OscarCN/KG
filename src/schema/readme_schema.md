This directory implements translation and parsing of data schemas into an easily specified target in order to be used in diverse data ingestion/translation pipelines
- diverse & customizable data types parsing
- contextual validation for each parsed object, implemented as functions
- contextual default values

Current schemas:
- news articles (`schemas/news.json`)
- source sites (`schemas/source.json`)

---

## Schema Definition

Schemas are defined in JSON files (`schemas/*.json`). Each file contains one or more object types with a `meta` section and a `schema` section:

```json
{
    "Source": {
        "meta": {
            "description": "Crawler target with domain, URLs, and crawling configuration"
        },
        "schema": {
            "domain": {"type": "str", "required": true},
            "sitio": {"type": "str", "default_fn": "default_sitio_from_domain"},
            "urls": {"type": "List[Url]", "required": true},
            "stats": {"type": "SourceStats"}
        }
    }
}
```

Field spec (specification) keys:
- `"type"` — always a string: primitives (`"str"`, `"int"`, `"float"`, `"bool"`, `"datetime"`), custom types (`"Url"`, `"EnumStr"`), generics (`"List[Url]"`, `"List[str]"`), nested/composite type names (`"SourceStats"`, `"LocationCoords"`), or lists of composite types (`"List[DateRangeFromUnstructured]"`)
- `"required"` — `true`/`false`
- `"required_fn"` — name of a callable validator (resolved from Python at load time)
- `"default"` — static default value
- `"default_fn"` — name of a callable default (resolved from Python at load time)
- `"enum"` — list of allowed values (for `EnumStr` types)
- `"meta"` — per-type metadata, currently supports `"description"`

## Schema Loading

`schemas/read_schema.py` loads JSON schemas and converts them to the Python dict format that `Parser` consumes:

```python
from src.schema.schemas.read_schema import load_schema

loaded = load_schema("schemas/news.json", callables={"date_now": date_now, ...})
# loaded["schemas"]  → {"News": {...}, "SourceExtra": {...}, ...}
# loaded["meta"]     → {"News": {"description": "..."}, ...}
```

`load_schema()`:
1. Resolves type strings to Python types via `types/registry.py:resolve_type_string()`
2. Resolves `default_fn` / `required_fn` to callables from the provided `callables` dict
3. Auto-resolves composite type dependencies from `types/composite_types.json`

Each schema's Python companion file (`schemas/source.py`, `schemas/news.py`) defines the callables and calls `load_schema()`. All loaded schemas are merged in `__init__.py`:

```python
from src.schema import SCHEMAS, META, normalize_record

# SCHEMAS: all loaded type schemas (Source, News, SourceStats, LocationCoords, ...)
# META: all type metadata (descriptions, etc.)

# Normalize a record
result = normalize_record(raw_record, "News")
```

## Example: normalizing a news record

```python
from src.schema import normalize_record

raw = {
    "title": "Inauguran nuevo parque en León",
    "body": "El alcalde inauguró el parque central...",
    "source": "milenio.com",
    "timestamp": "2026-03-15T10:30:00",
    "url": "https://milenio.com/nota/12345",
    "type": "news",
    "website_visits": 50000,        # flat key — mapped into source_extra.stats
    "likes": 120,
}

result = normalize_record(raw, "News")
# result["timestamp"]      → datetime(2026, 3, 15, 10, 30, tzinfo=...)
# result["timestamp_added"] → auto-filled with current time (default_fn: date_now)
# result["type"]           → "news" (validated against enum)
# result["source_extra"]["stats"]["website_visits"] → 50000
# result["source_extra"]["__FOUND_SOURCE__"]        → False (default_fn)
```

The system automatically:
- Maps flat data to nested object structure
- Converts types using dedicated parsers
- Applies defaults for missing values
- Auto-resolves composite type dependencies
- Validates required fields and data integrity

### Validation modes

`Parser.normalize_record()` accepts a `raise_validation_error` flag (default `True`) that controls how failed field validations are reported:

| `raise_validation_error` | Behavior on validation failure |
|---|---|
| `True` (default) | Raises the underlying validator exception (e.g. `ValueError("Missing required field: event_type")`) — the record is rejected. |
| `False` | Prints a `WARNING: schema validation issue for <Type>.<field>: <message>` line to stderr and continues; the record is still returned with whatever fields were parsed. All fields are checked (one warning per failure) instead of stopping at the first failure. |

```python
parser.normalize_record(record, "News", raise_validation_error=False)
# Bad/missing fields surface as stderr warnings, no exception raised.
```

The flag also propagates from `EntityExtractor.extract()` / `extract_supertype()` (in `src/entities/extraction/extract.py`) so PoC scripts can opt into warn-only mode without changing the schema layer call sites.

## Types

Type parsers convert raw values to Python types. Each parser has `parse()` and `validate()` methods.

Types are organized in `types/` by category:
- `types/primitives.py` — int, float, str, bool
- `types/dates.py` — datetime
- `types/strings.py` — Url, EnumStr
- `types/lists.py` — list (with generic element parsing, e.g. `List[Url]`, `List[str]`)
- `types/base.py` — TypeParser base class
- `types/registry.py` — `TYPE_PARSER_MAP` (Python type → parser instance), `TYPE_STRING_MAP` (string → Python type), `resolve_type_string()`, `resolve_parser_from_spec()`, `extract_list_object_type()`
- `types/string_helpers.py` — `_is_null`, `_is_valid_url`

See `types/type_catalog.py` for the full registry of implemented types, their parser classes, and source files.

### Composite types

Composite types are reusable multi-field types (nested object schemas) that can be referenced by name from any entity schema. They are defined in `types/composite_types.json` and loaded by `types/composite_types.py`.

When a schema references a composite type — directly (e.g. `"type": "DateRangeFromUnstructured"`) or inside a list (e.g. `"type": "List[DateRangeFromUnstructured]"`) — the loader automatically includes it and its transitive dependencies. No manual wiring needed.

Lists of composite types receive the full parsing pipeline: each item is processed as a nested object with type parsing, defaults, and validation applied recursively.

Defined in `types/composite_types.json`:

- **LocationCoords** — `{lat: float, lon: float}`
- **PeriodDates** — `{start: datetime, end: datetime}`
- **DateRange** — `{date_range: PeriodDates, timezone: str}`
- **DateRangeFromUnstructured** — `{date_range: PeriodDates, timezone: str, mention: str, precision_days: int}`
- **Location** — `{country: str, state: str, city: str, neighborhood: str, zone: str, street: str, number: str, place_name: str}`
- **PriceRange** — `{mention: str, lower: float, upper: float, currency: str}`
- **Attendance** — `{mention: str, estimate: int}`
- **VenueCapacity** — `{mention: str, capacity: int}`
- **CasualtyCount** — `{mention: str, dead: int, injured: int, missing: int}`
- **PersonReference** — `{name: str, role: str, organization: str}`

Example value (DateRangeFromUnstructured):
```json
{"date_range": {"start": "2026-01-07", "end": null}, "timezone": null, "mention": "se llevará a cabo en la segunda semana de enero", "precision_days": 7}
```

Example value (Location):
```json
{"country": "Mexico", "state": "Guanajuato", "city": "Leon", "neighborhood": null, "zone": "zona centro", "street": "Blvd. Lopez Mateos", "number": null, "place_name": "Teatro del Bicentenario"}
```

To add a new composite type:
1. Add it to `types/composite_types.json`
2. Add an entry to `COMPOSITE_TYPE_CATALOG` in `types/type_catalog.py`
3. Reference it by name from any schema — the loader auto-resolves it

See `types/type_catalog.py` `COMPOSITE_TYPE_CATALOG` for the full registry.
