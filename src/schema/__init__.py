from .schemas.source import loaded as _source_loaded
from .schemas.news import loaded as _news_loaded
from .parse_object import Parser
from .schemas.read_schema import load_schema

# Merged schemas and meta from all JSON schema files
SCHEMAS = {**_source_loaded["schemas"], **_news_loaded["schemas"]}
META = {**_source_loaded["meta"], **_news_loaded["meta"]}


def normalize_record(record: dict, type_name: str, context: dict = None) -> dict:
    """Normalize a record using the loaded schemas."""
    parser = Parser(SCHEMAS)
    return parser.normalize_record(record, type_name, context)


__all__ = [
    "SCHEMAS",
    "META",
    "Parser",
    "load_schema",
    "normalize_record",
]
