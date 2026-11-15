"""Bounded 2D marker summaries; deliberately independent from docking poses."""
from __future__ import annotations

import copy
import json
import math
import time

ARUCO_TOPIC = '/observer_camera/aruco_detections'
ARUCO_DICTIONARY = 'DICT_5X5_100'
ARUCO_KEYS = ('usbFront', 'usbLeft', 'usbHub3', 'usbHub2', 'color')
ARUCO_MAX_BYTES = 8192
ARUCO_TTL_MS = 5000


def _number(value, maximum):
    try:
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum
    except OverflowError:
        return False


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError('duplicate marker field')
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError('nonfinite marker number')


def _integer(value):
    if len(value) > 20:
        raise ValueError('marker integer exceeded')
    return int(value)


def _floating(value):
    if len(value) > 64:
        raise ValueError('marker number exceeded')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('nonfinite marker number')
    return result


def _waiting_row(status='WAITING_UPSTREAM'):
    return {'status': status, 'ageMs': None, 'dictionary': ARUCO_DICTIONARY, 'ids': []}


def waiting_aruco(status='WAITING_UPSTREAM', age_ms=None):
    return {'status': status, 'topic': ARUCO_TOPIC, 'dictionary': ARUCO_DICTIONARY,
            'ageMs': age_ms, 'cameras': {key: _waiting_row() for key in ARUCO_KEYS}}


def parse_aruco_summary(raw, now):
    """Validate once, copying only fixed public fields and charging transport age."""
    if (not isinstance(raw, str) or len(raw) > ARUCO_MAX_BYTES
            or len(raw.encode('utf-8')) > ARUCO_MAX_BYTES or not _number(now, 1e12)):
        raise ValueError('invalid marker summary')
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant,
                       parse_int=_integer, parse_float=_floating)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or not _number(value.get('timestamp'), 1e12) or value['timestamp'] <= 0
            or value['timestamp'] > now + .25 or not isinstance(value.get('cameras'), dict)):
        raise ValueError('invalid marker summary')
    transport_ms = max(0, (now - value['timestamp']) * 1000)
    if transport_ms >= ARUCO_TTL_MS:
        raise ValueError('expired marker summary')
    result = waiting_aruco('AVAILABLE', transport_ms)
    for key in ARUCO_KEYS:
        row = value['cameras'].get(key)
        if (not isinstance(row, dict) or row.get('dictionary') != ARUCO_DICTIONARY
                or row.get('status') not in ('AVAILABLE', 'WAITING_UPSTREAM', 'ERROR')
                or not isinstance(row.get('ids'), list) or len(row['ids']) > 12
                or any(type(identifier) is not int or not 0 <= identifier <= 99 for identifier in row['ids'])
                or (row.get('ageMs') is not None and not _number(row['ageMs'], 86_400_000))):
            raise ValueError('invalid marker camera')
        if row['status'] != 'AVAILABLE':
            # A producer cannot smuggle an old ID into an unavailable row.
            result['cameras'][key] = _waiting_row(row['status'])
            continue
        if not _number(row.get('ageMs'), 86_400_000):
            raise ValueError('missing marker source age')
        age = row['ageMs'] + transport_ms
        if age < ARUCO_TTL_MS:
            result['cameras'][key] = {
                'status': 'AVAILABLE', 'ageMs': age, 'dictionary': ARUCO_DICTIONARY,
                'ids': sorted(set(row['ids']))}
    return value['timestamp'], result


class ArucoPreview:
    """Age the latest summary without letting invalid/replayed input renew it."""

    def __init__(self, clock=time.monotonic, wall_clock=time.time):
        self.clock, self.wall_clock = clock, wall_clock
        self.value = None
        self.received = None
        self.timestamp = None

    def receive(self, raw):
        try:
            timestamp, value = parse_aruco_summary(raw, self.wall_clock())
            if self.timestamp is not None and timestamp <= self.timestamp:
                raise ValueError('replayed marker summary')
        except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
            self.value = None
            return False
        self.timestamp, self.value, self.received = timestamp, value, self.clock()
        return True

    def snapshot(self):
        if self.value is None:
            return waiting_aruco()
        elapsed_ms = max(0, self.clock() - self.received) * 1000
        age_ms = self.value['ageMs'] + elapsed_ms
        if age_ms >= ARUCO_TTL_MS:
            return waiting_aruco(age_ms=age_ms)
        result = copy.deepcopy(self.value)
        result['ageMs'] = age_ms
        for key, row in result['cameras'].items():
            if row['status'] == 'AVAILABLE':
                row['ageMs'] += elapsed_ms
                if row['ageMs'] >= ARUCO_TTL_MS:
                    result['cameras'][key] = _waiting_row()
        return result
