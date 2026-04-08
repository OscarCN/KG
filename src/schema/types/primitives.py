from __future__ import annotations

from typing import Any, Dict, Optional

from .string_helpers import _is_null
from .base import TypeParser


def parse_int(value: Any) -> Optional[int]:
    if _is_null(value) or value == "":
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).strip()))
    except Exception:
        return None


def parse_float(value: Any) -> Optional[float]:
    if _is_null(value) or value == "":
        return None
    try:
        if isinstance(value, bool):
            return float(int(value))
        return float(str(value).strip())
    except Exception:
        return None


def parse_str(value: Any) -> Optional[str]:
    if _is_null(value):
        return None
    s = str(value).strip()
    return s if s != "" else None


def parse_bool(value: Any) -> Optional[bool]:
    if _is_null(value):
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return True
    if s in {"false", "0", "no", "n"}:
        return False
    return None


class IntParser(TypeParser):
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[int]:
        return parse_int(value)

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)
        if value is not None and not isinstance(value, int):
            raise ValueError(f"{field_name or 'field'} must be int")


class FloatParser(TypeParser):
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[float]:
        return parse_float(value)

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)
        if value is not None and not isinstance(value, (int, float)):
            raise ValueError(f"{field_name or 'field'} must be float")


class StrParser(TypeParser):
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
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{field_name or 'field'} must be str")


class BoolParser(TypeParser):
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[bool]:
        return parse_bool(value)

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{field_name or 'field'} must be bool")
