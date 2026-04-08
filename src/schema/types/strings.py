from __future__ import annotations

from typing import Any, Dict, Optional

from .string_helpers import _is_valid_url, _is_null
from .base import TypeParser
from .primitives import parse_str


class Url:  # marker type
    pass


class EnumStr:  # marker type
    pass


class UrlParser(TypeParser):

    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
        s = parse_str(value)
        if _is_null(s):
            return None
        return s

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)
        if value is not None:
            if not isinstance(value, str) or not _is_valid_url(value):
                raise ValueError(f"{field_name or 'field'} must be a valid URL")


class EnumStrParser(TypeParser):
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
        return parse_str(value)

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)

        if _is_null(value):
            return

        if not isinstance(value, str):
            raise ValueError(f"{field_name or 'field'} must be str")

        allowed = (spec or {}).get("enum")
        if allowed is not None and value not in allowed:
            raise ValueError(f"{field_name or 'field'} must be one of {allowed}")
