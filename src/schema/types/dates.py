from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime

from dateutil.parser import parse as parse_datetime_str
from .string_helpers import _is_null
from .base import TypeParser, local_tz, PANDAS_AVAILABLE

if PANDAS_AVAILABLE:
    import pandas as pn


def parse_datetime(value: Any) -> Optional[datetime]:
    if _is_null(value) or value == "":
        ret = None

    if isinstance(value, datetime):
        ret = value

    # Handle pandas Timestamp
    if PANDAS_AVAILABLE and isinstance(value, pn.Timestamp):
        ret = value.to_pydatetime()

    if isinstance(value, str):
        s = value.strip()
        if not s:
            ret = None
        try:
            ret = parse_datetime_str(s, dayfirst=True)
        except Exception:
            try:
                ret = parse_datetime_str(s)
            except Exception:
                ret = None

    if isinstance(ret, datetime):
        if ret.tzinfo is None:
            ret = ret.replace(tzinfo=local_tz)

    return ret


class DateTimeParser(TypeParser):
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Optional[datetime]:
        return parse_datetime(value)

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)
        if value is not None and not isinstance(value, datetime):
            raise ValueError(f"{field_name or 'field'} must be datetime")
