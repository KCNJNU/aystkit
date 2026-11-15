"""Strict parser and monotonic health tracking for production telemetry."""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import deque
from typing import Any, Callable

from .truth import Status, json_safe

TOPIC = "/chassis_host/telemetry"
TOPIC_TYPE = "std_msgs/msg/String"
STALE_SECONDS = 2.0
RATE_WINDOW_SECONDS = 5.0
KNOWN_PAGES = {
    "0x00": "legacy",
    "0x10": "motion",
    "0x11": "brush",
    "0x12": "lift",
    "0x13": "fault",
}


class TelemetryParseError(ValueError):
    pass


def _reject_constant(token: str) -> None:
    raise TelemetryParseError(f"non-finite JSON constant: {token}")


def non_finite_count(value: Any) -> int:
    """Count sanitization without substituting commands or fabricated values."""
    if isinstance(value, float) and not math.isfinite(value):
        return 1
    if isinstance(value, dict):
        return sum(non_finite_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(non_finite_count(item) for item in value)
    return 0


def parse_telemetry(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise TelemetryParseError("empty String.data")
    if len(text.encode('utf-8')) > 262144:
        raise TelemetryParseError("telemetry JSON exceeds 256 KiB")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError, TelemetryParseError) as exc:
        raise TelemetryParseError(str(exc)) from exc
    if not isinstance(value, dict):
        raise TelemetryParseError("payload must be a JSON object")
    if type(value.get("telemetryTransportSchema")) is not int or value.get("telemetryTransportSchema") != 1:
        raise TelemetryParseError("telemetryTransportSchema must equal 1")
    if value.get("telemetrySourceNode") != "/chassis_host":
        raise TelemetryParseError("telemetrySourceNode must equal /chassis_host")
    serial = value.get("serialDebug")
    if serial is not None and not isinstance(serial, dict):
        raise TelemetryParseError("serialDebug must be an object or null")
    if isinstance(serial, dict) and serial.get("telemetryPages") is not None and not isinstance(serial["telemetryPages"], dict):
        raise TelemetryParseError("telemetryPages must be an object or null")
    return json_safe(value)


class SlidingRate:
    def __init__(self, window_s: float = RATE_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self.samples: deque[float] = deque()

    def add(self, timestamp: float | None = None) -> None:
        now = self.clock() if timestamp is None else timestamp
        self.samples.append(now)
        self._trim(now)

    def value(self, now: float | None = None) -> float | None:
        current = self.clock() if now is None else now
        self._trim(current)
        if len(self.samples) < 2:
            return None
        duration = self.samples[-1] - self.samples[0]
        return None if duration <= 0 else (len(self.samples) - 1) / duration

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        while self.samples and self.samples[0] < cutoff:
            self.samples.popleft()


class PageRateTracker:
    def __init__(self, window_s: float = RATE_WINDOW_SECONDS):
        self.window_s = window_s
        self.samples: dict[str, deque[tuple[float, int]]] = {
            page: deque() for page in KNOWN_PAGES
        }

    def update(self, payload: dict[str, Any], now: float) -> None:
        raw_pages = (payload.get("serialDebug") or {}).get("telemetryPages") or {}
        by_name = {str(v).lower(): k for k, v in KNOWN_PAGES.items()}
        for key, item in raw_pages.items():
            if not isinstance(item, dict):
                continue
            page = str(key).lower()
            if page not in self.samples:
                page = by_name.get(page, "")
            count = item.get("count")
            if page in self.samples and isinstance(count, int) and not isinstance(count, bool):
                series = self.samples[page]
                series.append((now, count))
                self._trim(series, now)

    def rate(self, page: str, now: float) -> float | None:
        series = self.samples[page]
        self._trim(series, now)
        if len(series) < 2:
            return None
        elapsed = series[-1][0] - series[0][0]
        delta = series[-1][1] - series[0][1]
        return None if elapsed <= 0 or delta < 0 else delta / elapsed

    def _trim(self, series: deque[tuple[float, int]], now: float) -> None:
        cutoff = now - self.window_s
        while len(series) > 1 and series[1][0] < cutoff:
            series.popleft()


class TelemetryStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.publisher_count = 0
        self.observed_types: list[str] = []
        self.contract_conflict: str | None = None
        self.message_count = 0
        self.parse_error_count = 0
        self.last_error: str | None = None
        self.non_finite_count = 0
        self.last_message_monotonic: float | None = None
        self.last_valid_monotonic: float | None = None
        self.latest: dict[str, Any] | None = None
        self.rate = SlidingRate(clock=clock)
        self.pages = PageRateTracker()

    def set_publisher_count(self, count: int) -> None:
        with self.lock:
            self.publisher_count = max(0, int(count))

    def set_graph(self, publisher_count: int, observed_types: list[str]) -> None:
        with self.lock:
            self.publisher_count = max(0, int(publisher_count))
            self.observed_types = sorted(set(observed_types))
            invalid = [kind for kind in self.observed_types if kind != TOPIC_TYPE]
            self.contract_conflict = (f"Contract expects {TOPIC_TYPE}; graph reports {', '.join(invalid)}"
                                      if invalid else None)

    def receive(self, text: str) -> bool:
        now = self.clock()
        with self.lock:
            self.message_count += 1
            self.last_message_monotonic = now
            try:
                payload = parse_telemetry(text)
            except TelemetryParseError as exc:
                self.parse_error_count += 1
                self.last_error = str(exc)
                return False
            self.non_finite_count += non_finite_count(json.loads(text))
            self.latest = payload
            self.last_valid_monotonic = now
            self.last_error = None
            self.rate.add(now)
            self.pages.update(payload, now)
            return True

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            age = None if self.last_valid_monotonic is None else max(0.0, now - self.last_valid_monotonic)
            if self.contract_conflict:
                status = Status.ERROR
                reason = self.contract_conflict
            elif self.latest is None:
                if self.parse_error_count:
                    status = Status.ERROR
                    reason = self.last_error
                elif self.publisher_count:
                    status = Status.NO_DATA
                    reason = "Publisher visible; no valid telemetry received"
                else:
                    status = Status.WAITING_UPSTREAM
                    reason = "Telemetry publisher not discovered"
            elif age is not None and age > STALE_SECONDS:
                status = Status.STALE
                reason = f"Last valid telemetry older than {STALE_SECONDS:g}s"
            else:
                status = Status.AVAILABLE
                reason = None
            payload = copy.deepcopy(self.latest)
            pages = self._page_snapshot(payload, status, now)
            return {
                "topic": TOPIC,
                "topicType": TOPIC_TYPE,
                "status": status.value,
                "reason": reason,
                "publisherCount": self.publisher_count,
                "observedTypes": list(self.observed_types),
                "contractConflict": self.contract_conflict,
                "messageCount": self.message_count,
                "parseErrorCount": self.parse_error_count,
                "nonFiniteCount": self.non_finite_count,
                "lastError": self.last_error,
                "ageS": age,
                "hz": self.rate.value(now),
                "payload": payload,
                "pages": pages,
            }

    def _page_snapshot(self, payload: dict[str, Any] | None, telemetry_status: Status,
                       now: float) -> dict[str, Any]:
        source = ((payload or {}).get("serialDebug") or {}).get("telemetryPages") or {}
        by_name = {str(v).lower(): k for k, v in KNOWN_PAGES.items()}
        normalized: dict[str, Any] = {}
        for key, item in source.items():
            page = str(key).lower()
            if page not in KNOWN_PAGES:
                page = by_name.get(page, "")
            if page:
                normalized[page] = item
        result = {}
        for page, name in KNOWN_PAGES.items():
            item = normalized.get(page) if isinstance(normalized.get(page), dict) else {}
            raw_count = item.get("count")
            count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else None
            age_ms = item.get("ageMs")
            if count is None or count <= 0:
                status = Status.WAITING_UPSTREAM
            elif telemetry_status is Status.ERROR:
                status = Status.ERROR
            elif (item.get('active') is False or telemetry_status is Status.STALE
                  or (isinstance(age_ms, (int, float)) and age_ms > 2000)):
                status = Status.STALE
            else:
                status = Status.AVAILABLE
            result[page] = {
                "name": name,
                "count": count,
                "hz": self.pages.rate(page, now),
                "ageMs": age_ms,
                "status": status.value,
            }
        return result
