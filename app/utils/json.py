from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """
    Recursively convert an object into a JSON-serializable format.
    
    Handles:
    - Decimal -> str
    - datetime/date -> ISO string
    - dataclass -> dict (via asdict)
    - Enum -> value
    - list/tuple -> list of converted items
    - dict -> dict with converted keys and values
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    
    # Avoid recursion on Mock objects in tests
    if "unittest.mock" in str(type(obj)):
        return str(obj)

    # Fallback for objects that might have a to_dict method (like our models)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return to_jsonable(obj.to_dict())
        
    return obj


def safe_json_dumps(obj: Any, **kwargs: Any) -> str:
    """
    Safely dump an object to a JSON string using to_jsonable for unknown types.
    """
    # We still provide default=str as a final safety net for things to_jsonable might miss
    if "default" not in kwargs:
        kwargs["default"] = str
    return json.dumps(to_jsonable(obj), **kwargs)
