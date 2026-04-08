This directory implements translation and parsing of data schemas into an easily specified target in order to be used in diverse data ingestion/translation pipelines
- diverse & customizable data types parsing
- contextual validation for each parsed object, implemented as functions
- contextual default values

Now, it contains schemas for: 
- news articles
- source sites

Easy adaptation for any schema and source

schema.* contains schemas and general types parsing and translation logic

---

## Schema Definition

Schemas are defined in JSON files (`schemas/*.json`) with string type references and optional metadata. Each JSON file contains one or more object types:

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

- `"type"` is always a string (`"str"`, `"int"`, `"datetime"`, `"Url"`, `"EnumStr"`, `"List[Url]"`, nested object names like `"SourceStats"`)
- Static defaults use `"default"` (literal JSON values)
- Callable defaults use `"default_fn"` (resolved from a Python callables registry at load time)
- Callable required validators use `"required_fn"` (same mechanism)
- `"meta"` holds optional metadata per type, including a `"description"`

JSON schemas are loaded and converted to Python dicts via `schemas/read_schema.py`:

```python
from schemas.read_schema import load_schema
loaded = load_schema("schemas/source.json", callables={"date_now": date_now, ...})
SOURCE_SCHEMA = loaded["schemas"]["Source"]
meta = loaded["meta"]["Source"]  # {"description": "..."}
```

## Schema Usage

The schema system provides a unified API for data normalization:

```python
from schema import Parser, SOURCE_SCHEMA, SOURCE_STATS_SCHEMA

# Initialize parser with schemas
parser = Parser({
    "Source": SOURCE_SCHEMA,
    "SourceStats": SOURCE_STATS_SCHEMA
})

# Normalize a record
normalized = parser.normalize_record(raw_record, "Source")
```

The system automatically:
- Maps flat data to nested object structure
- Converts types using dedicated parsers
- Applies defaults for missing values
- Validates required fields and data integrity

Useful for handling complex data types

## Types

Every type should be specified with specific classes and parsers using Python types as a base.

Types are organized in `types/` by category:
- `types/primitives.py` — int, float, str, bool
- `types/dates.py` — datetime
- `types/strings.py` — Url, EnumStr
- `types/lists.py` — list (with generic element parsing)
- `types/base.py` — TypeParser base class
- `types/registry.py` — TYPE_PARSER_MAP, TYPE_STRING_MAP, resolve_parser_from_spec(), resolve_type_string()
- `types/string_helpers.py` — _is_null, _is_valid_url

See `types/type_catalog.py` for the full registry of implemented types, their parser classes, and source files.

Python typing compatible, e.g. Url custom type and parser, List[Url] the list parser parses as list, and each element in it is parsed as Url.

### Composite types

Composite types are reusable multi-field types (nested object schemas) that can be referenced by name from any entity schema. They are defined in `types/composite_types.json` and loaded by `types/composite_types.py`.

When a schema references a composite type (e.g. `"type": "DateRangeFromUnstructured"`), the auto-resolving loader in `schemas/read_schema.py` automatically includes it and its transitive dependencies in the loaded schemas dict — no manual wiring needed.

Defined in `types/composite_types.json`:

- **LocationCoords** — `{lat: float, lon: float}`
- **PeriodDates** — `{start: datetime, end: datetime}`
- **DateRange** — `{date_range: PeriodDates, timezone: str}`
- **DateRangeFromUnstructured** — `{date_range: PeriodDates, timezone: str, mention: str, precision_days: int}`

Example value:
```json
{"date_range": {"start": "2026-01-07", "end": null}, "timezone": null, "mention": "se llevará a cabo en la segunda semana de enero", "precision_days": 7}
```

To add a new composite type:
1. Add it to `types/composite_types.json`
2. Add an entry to `COMPOSITE_TYPE_CATALOG` in `types/type_catalog.py`
3. Reference it by name from any schema — the loader auto-resolves it

See `types/type_catalog.py` `COMPOSITE_TYPE_CATALOG` for the full registry.











