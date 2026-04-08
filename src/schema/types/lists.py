from __future__ import annotations

from typing import Any, Dict, List, Optional
from ast import literal_eval

from .string_helpers import _is_null
from .base import TypeParser


class ListParser(TypeParser):
    def __init__(self, element_parser: Optional[TypeParser] = None):
        self.element_parser = element_parser

    def parse(self, value: Any, spec: Optional[Dict[str, Any]] = None) -> List[Any]:
        if _is_null(value):
            result = []
        elif isinstance(value, list):
            result = value
        elif isinstance(value, str):
            v = value.strip()
            try:
                parsed = literal_eval(v)
                if isinstance(parsed, list):
                    result = parsed
                else:
                    result = [v]
            except Exception:
                result = [v]
        else:
            result = [value]

        if self.element_parser is not None:
            result = [self.element_parser.parse(item, spec) for item in result]
        return result

    def validate(
        self,
        value: Any,
        spec: Optional[Dict[str, Any]] = None,
        field_name: Optional[str] = None,
        full_object: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        super().validate(value, spec, field_name, full_object, context)

        # List must be a non-empty list if required
        if spec and spec.get("required") and (_is_null(value) or (isinstance(value, list) and len(value) == 0)):
            raise ValueError(f"Missing required field: {field_name or 'field'} (empty list)")

        if value is not None:
            if not isinstance(value, list):
                raise ValueError(f"{field_name or 'field'} must be a list")
            if self.element_parser is not None:
                for i, item in enumerate(value):
                    self.element_parser.validate(
                        item, spec,
                        field_name=f"{field_name or 'field'}[{i}]",
                        full_object=full_object, context=context
                    )
