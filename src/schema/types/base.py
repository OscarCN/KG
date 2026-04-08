from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime

from .string_helpers import _is_null

local_tz = datetime.now().astimezone().tzinfo

# Try to import pandas for Timestamp support
try:
    import pandas as pn
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False


class TypeParser:
    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> Any:
        return value

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        if spec:
            required = spec.get("required")

            is_required = bool(required)

            # Support callable required (like default values)
            if callable(required):

                if not required(full_object or {}, context or {}):
                    raise ValueError(f"Missing required field: {field_name or 'field'} by function {spec.get('required')}")

            if is_required and _is_null(value):
                raise ValueError(f"Missing required field: {field_name or 'field'}")
