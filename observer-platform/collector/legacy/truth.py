"""Shared data-truth primitives. No missing value is converted to zero."""
from __future__ import annotations

import math
from enum import Enum
from typing import Any


class Status(str, Enum):
    ERROR = "ERROR"
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NO_DATA = "NO_DATA"
    WAITING_UPSTREAM = "WAITING_UPSTREAM"
    UNAVAILABLE = "UNAVAILABLE"


def finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with null while preserving real zero."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def metric(value: Any, status: Status | str, source: str, *, unit: str | None = None,
           kind: str = "RUNTIME", age_ms: float | None = None,
           reason: str | None = None) -> dict[str, Any]:
    return {
        "value": json_safe(value),
        "status": status.value if isinstance(status, Status) else status,
        "source": source,
        "unit": unit,
        "type": kind,
        "ageMs": finite_number(age_ms),
        "reason": reason,
    }
