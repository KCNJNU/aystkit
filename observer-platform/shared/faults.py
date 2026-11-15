"""Bounded, version-independent fault-event DTO validation (no ROS imports)."""
from __future__ import annotations

import json
import math
import re
import uuid

EVENT_FIELDS = frozenset({'id', 'timestamp', 'level', 'code', 'component', 'message', 'details'})
EVENT_LEVELS = frozenset({'INFO', 'WARN', 'ERROR', 'FATAL'})
EVENT_CODE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
MAX_EVENT_BATCH = 32
MAX_EVENTS_BYTES = 131072

# A warning must describe one of the operator's key failures. Generic warnings,
# recovery and startup information stay out of the durable fault history.
KEY_WARNING_CODES = frozenset({
    'LIDAR_TIMESTAMP_GAP', 'IMU_TIMESTAMP_GAP', 'STREAM_TIMESTAMP_GAP', 'SENSOR_TIME_GAP_REPORTED',
    'DEVICE_DATA_INTERRUPTED', 'SENSOR_STALE', 'CAMERA_PREVIEW_INTERRUPTED',
    'COLLECTOR_CONNECTION_LOST', 'ROS_DIAGNOSTIC_STALE',
    'NAVIGATION_TIMEOUT', 'NAVIGATION_FAILURE', 'LOCALIZATION_WARNING',
    'CONTROLLER_OSCILLATION', 'OBSTACLE_STOP_REPORTED',
    'HOST_TEMPERATURE_HIGH', 'FAULT_MONITOR_LIMITED',
})


def should_record_fault(level, code, details=None):
    """Shared recording policy; caller still validates every event before ACK."""
    if level in ('ERROR', 'FATAL'):
        return True
    if level != 'WARN':
        return False
    if code == 'REPEATED_FAULT_SUMMARY':
        code = details.get('original_code') if isinstance(details, dict) else None
    return code in KEY_WARNING_CODES


# These codes already state the failure type; changing a measured delta, goal
# identifier or source text does not create a different incident.
GROUPED_FAULT_CODES = KEY_WARNING_CODES | frozenset({
    'LIDAR_TIMESTAMP_ROLLBACK', 'IMU_TIMESTAMP_ROLLBACK', 'STREAM_TIMESTAMP_ROLLBACK',
    'LIDAR_POINT_TIME_WRAP', 'LIDAR_POINT_TIME_NONMONOTONIC',
    'SENSOR_TIME_ROLLBACK_REPORTED', 'SENSOR_TIME_SYNC_REPORTED',
    'POINT_TIME_WRAP_REPORTED', 'NAVIGATION_ABORTED', 'FAULT_LOG_OVERFLOW',
})
_DYNAMIC_UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)
_DYNAMIC_HEX = re.compile(r'(?<![\w])0x[0-9a-f]+(?![\w])', re.I)
_DYNAMIC_NUMBER = re.compile(r'(?<![\w])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w])')
_SEMANTIC_NUMBER_PREFIX = re.compile(
    r'(?:\b(?:errno|error(?:[ _-]*code)?|status(?:[ _-]*code)?|signal|code)|错误码|故障码|状态码|信号)\s*[:：=#(\[]?\s*$', re.I)


def _normalize_dynamic_number(match):
    # Numeric failure/status codes express distinct failures, unlike measured
    # deltas and timestamps. Preserve them when their semantics are explicit.
    before = match.string[max(0, match.start() - 40):match.start()]
    return match.group() if _SEMANTIC_NUMBER_PREFIX.search(before) else '<number>'


def fault_group_key(code, component, message):
    """Stable incident identity without collapsing distinct generic errors."""
    if code in GROUPED_FAULT_CODES:
        normalized = ''
    else:
        normalized = _DYNAMIC_UUID.sub('<uuid>', message)
        normalized = _DYNAMIC_HEX.sub('<hex>', normalized)
        normalized = _DYNAMIC_NUMBER.sub(_normalize_dynamic_number, normalized)
        normalized = ' '.join(normalized.split())
    return code, component, normalized


def _text(value, maximum, allow_empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not value and not allow_empty)
            or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
        raise ValueError('invalid event text')
    return value


def _details(value, depth=0, budget=None):
    budget = [512] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 6:
        raise ValueError('event details exceed bounds')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if abs(value) > 2 ** 63 - 1 or not math.isfinite(value):
            raise ValueError('invalid event number')
        return value
    if isinstance(value, str):
        return _text(value, 2048, True)
    if isinstance(value, list) and len(value) <= 64:
        return [_details(v, depth + 1, budget) for v in value]
    if isinstance(value, dict) and len(value) <= 64:
        return {_text(k, 100): _details(v, depth + 1, budget) for k, v in value.items()}
    raise ValueError('invalid event details')


def validate_event(event):
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise ValueError('invalid event fields')
    try:
        if str(uuid.UUID(event['id'])) != event['id']:
            raise ValueError()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('invalid event id') from exc
    timestamp = event['timestamp']
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not 0 < timestamp <= 253402300799 or not math.isfinite(timestamp):
        raise ValueError('invalid event timestamp')
    if not isinstance(event['level'], str) or event['level'] not in EVENT_LEVELS:
        raise ValueError('invalid event level')
    code = event['code']
    if not isinstance(code, str) or not EVENT_CODE.fullmatch(code):
        raise ValueError('invalid event code')
    component = _text(event['component'], 160)
    message = _text(event['message'], 2048)
    if not isinstance(event['details'], dict):
        raise ValueError('event details must be object')
    details = _details(event['details'])
    if len(json.dumps(details, ensure_ascii=False, allow_nan=False).encode()) > 8192:
        raise ValueError('event details too large')
    return dict(event, component=component, message=message, details=details)


def validate_events_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'events'}:
        raise ValueError('invalid events payload')
    events = payload['events']
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENT_BATCH:
        raise ValueError('invalid event batch size')
    result = {'events': [validate_event(e) for e in events]}
    if len({e['id'] for e in result['events']}) != len(result['events']):
        raise ValueError('duplicate event id in batch')
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_EVENTS_BYTES:
        raise ValueError('events batch too large')
    return result
