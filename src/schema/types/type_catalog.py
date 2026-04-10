"""
Type Catalog — registry of all implemented data types and their parsers.

Each entry maps a type key to its parser class and source file within types/.
Use this as a reference when adding new types or looking up existing ones.

To add a new type:
  1. Create or extend the appropriate file in types/ (e.g. dates.py, primitives.py)
  2. Implement the parser class (subclass of TypeParser from base.py)
  3. Add the entry to TYPE_CATALOG below
  4. Register it in registry.py TYPE_PARSER_MAP
  5. Re-export from types/__init__.py if needed externally
"""

TYPE_CATALOG = {
    # Primitives (types/primitives.py)
    "int":      {"parser": "IntParser",      "file": "types/primitives.py", "description": "Integer parsing with bool and string handling"},
    "float":    {"parser": "FloatParser",    "file": "types/primitives.py", "description": "Float parsing with bool and string handling"},
    "str":      {"parser": "StrParser",      "file": "types/primitives.py", "description": "String parsing with whitespace trimming and null detection"},
    "bool":     {"parser": "BoolParser",     "file": "types/primitives.py", "description": "Boolean parsing from various string representations (true/false, yes/no, 1/0)"},

    # Dates (types/dates.py)
    "datetime": {"parser": "DateTimeParser", "file": "types/dates.py",      "description": "Datetime parsing with dateutil, pandas Timestamp support, and local timezone"},

    # Strings / URLs (types/strings.py)
    "Url":      {"parser": "UrlParser",      "file": "types/strings.py",    "description": "URL string validation"},
    "EnumStr":  {"parser": "EnumStrParser",  "file": "types/strings.py",    "description": "String validated against an allowed set of enum values"},

    # Lists (types/lists.py)
    "list":     {"parser": "ListParser",     "file": "types/lists.py",      "description": "List parsing with optional element-level parsing (supports generics like List[str])"},
}

# Composite types — reusable multi-field types defined in types/composite_types.json
# These are nested object schemas (not parsers) that can be referenced by name from any entity schema.
COMPOSITE_TYPE_CATALOG = {
    "LocationCoords":             {"file": "types/composite_types.json", "description": "Geographic coordinates (latitude, longitude)"},
    "PeriodDates":                {"file": "types/composite_types.json", "description": "Date period with a start and end datetime"},
    "DateRange":                  {"file": "types/composite_types.json", "description": "Date range with timezone"},
    "DateRangeFromUnstructured":  {"file": "types/composite_types.json", "description": "Date period extracted from unstructured text, with original mention and precision in days"},
    "Location":                   {"file": "types/composite_types.json", "description": "Structured location with hierarchical geographic fields and optional named place"},
    "PriceRange":                 {"file": "types/composite_types.json", "description": "Price/cost range with original mention, numeric bounds, and currency"},
    "Attendance":                 {"file": "types/composite_types.json", "description": "Estimated attendance with original mention and numeric estimate"},
    "VenueCapacity":              {"file": "types/composite_types.json", "description": "Venue capacity with original mention and numeric value"},
    "CasualtyCount":              {"file": "types/composite_types.json", "description": "Casualty count (dead, injured, missing) with original mention"},
    "CountMention":               {"file": "types/composite_types.json", "description": "Numeric count with original text mention and confidence range"},
    "PersonReference":            {"file": "types/composite_types.json", "description": "Reference to a person with name, role, and organization"},
}
