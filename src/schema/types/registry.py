from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Type, get_origin, get_args
from datetime import datetime

from .base import TypeParser
from .primitives import IntParser, FloatParser, StrParser, BoolParser
from .dates import DateTimeParser
from .strings import Url, EnumStr, UrlParser, EnumStrParser
from .lists import ListParser


# Registry mapping python types/markers to parser instances
TYPE_PARSER_MAP: Dict[Type[Any], TypeParser] = {
    int: IntParser(),
    float: FloatParser(),
    str: StrParser(),
    bool: BoolParser(),
    datetime: DateTimeParser(),
    list: ListParser(),
    Url: UrlParser(),
    EnumStr: EnumStrParser(),
}

# Registry mapping type name strings to Python types
TYPE_STRING_MAP: Dict[str, Any] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "datetime": datetime,
    "list": list,
    "Url": Url,
    "EnumStr": EnumStr,
}

_GENERIC_RE = re.compile(r"^(List)\[(.+)\]$")


def resolve_type_string(type_str: str) -> Any:
    """Resolve a type name string to a Python type.

    Handles primitives ("str"), custom markers ("Url"), and generics ("List[Url]").
    Unrecognized names are returned as-is (strings), which is how Parser
    handles nested object type references.
    """
    # Direct lookup
    if type_str in TYPE_STRING_MAP:
        return TYPE_STRING_MAP[type_str]

    # Generic type, e.g. "List[Url]"
    m = _GENERIC_RE.match(type_str)
    if m:
        element_type = resolve_type_string(m.group(2))
        return List[element_type]

    # Unrecognized — treat as nested object reference (keep as string)
    return type_str


# Cache for dynamically created generic parsers (e.g. List[str], List[Url])
_generic_parser_cache: Dict[Any, TypeParser] = {}


def resolve_parser_from_spec(spec: Dict[str, Any]) -> Optional[TypeParser]:

    field_type = spec.get("type")

    # Direct lookup (handles list, int, str, Url, etc.)
    if isinstance(field_type, type) and field_type in TYPE_PARSER_MAP:
        return TYPE_PARSER_MAP[field_type]

    # Generic type (e.g. List[str], List[Url])
    origin = get_origin(field_type)
    if origin is list:
        if field_type in _generic_parser_cache:
            return _generic_parser_cache[field_type]

        args = get_args(field_type)
        element_parser = None
        if args:
            element_parser = resolve_parser_from_spec({"type": args[0]})

        parser = ListParser(element_parser)
        _generic_parser_cache[field_type] = parser
        return parser

    return None
