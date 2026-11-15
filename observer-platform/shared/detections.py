"""Bounded, public-only metadata for five read-only detection overlays.

No images, paths, exception messages or arbitrary labels cross this contract.
Age belongs to the source frame and can only increase at each network hop.
"""
from __future__ import annotations

import json
import math
import re

DETECTION_KEYS = ('usbFront', 'usbLeft', 'usbHub3', 'usbHub2', 'color')
DETECTION_MAX_BYTES = 131_072
DETECTION_FILE_TTL_MS = 3000
DETECTION_RESULT_TTL_MS = 5000
MODEL = 'YOLO26n'
ARUCO_DICTIONARY = 'DICT_5X5_100'
DETECTION_MAX_MARKERS = 12
_CORNER_CROSS_EPSILON = 1e-10
_SESSION = re.compile(r'[0-9a-f]{32}\Z')
_IMAGE = re.compile(r'[0-9a-f]{64}\Z')
_RESULT = re.compile(r'([0-9a-f]{32}):(0|[1-9][0-9]{0,15})\Z')
_MAX_COUNTER = 2 ** 53 - 1


def video_session(value):
    return value if isinstance(value, str) and _SESSION.fullmatch(value) else None


def _number(value, maximum):
    try:
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum
    except OverflowError:
        return False


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def waiting_detections(session=None):
    return {'version': 1, 'session': video_session(session), 'model': MODEL,
            'cameras': {key: {'status': 'WAITING_UPSTREAM', 'results': []} for key in DETECTION_KEYS}}


def decode_detections(raw):
    """Reject ambiguous documents and oversized numbers before conversion."""
    if not isinstance(raw, bytes) or len(raw) > DETECTION_MAX_BYTES:
        raise ValueError('invalid detection document')

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate detection field')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('nonfinite detection number')

    def integer(value):
        if len(value) > 20:
            raise ValueError('detection integer exceeded')
        return int(value)

    def floating(value):
        if len(value) > 64:
            raise ValueError('detection number exceeded')
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite detection number')
        return result

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                      parse_int=integer, parse_float=floating)


def _markers(value):
    if not isinstance(value, list) or len(value) > DETECTION_MAX_MARKERS:
        return None
    clean = []
    for marker in value:
        if (not isinstance(marker, dict) or marker.get('dictionary') != ARUCO_DICTIONARY
                or not _integer(marker.get('id'), 0, 99)):
            return None
        corners = marker.get('corners')
        if (not isinstance(corners, list) or len(corners) != 4
                or any(not isinstance(point, list) or len(point) != 2
                       or not all(_number(coordinate, 1) for coordinate in point) for point in corners)):
            return None
        # OpenCV returns the marker's ordered corners. Preserve that orientation,
        # while rejecting collapsed, concave, or crossed quadrilaterals.
        crosses = []
        for index in range(4):
            first, middle, last = (corners[(index + offset) % 4] for offset in range(3))
            cross = ((middle[0] - first[0]) * (last[1] - middle[1])
                     - (middle[1] - first[1]) * (last[0] - middle[0]))
            crosses.append(cross)
        if not (all(cross > _CORNER_CROSS_EPSILON for cross in crosses)
                or all(cross < -_CORNER_CROSS_EPSILON for cross in crosses)):
            return None
        clean.append({'dictionary': ARUCO_DICTIONARY, 'id': marker['id'],
                      'corners': [list(point) for point in corners]})
    return clean


def _result(value, key, session, elapsed_ms):
    if not isinstance(value, dict):
        return None
    identity = value.get('id')
    match = _RESULT.fullmatch(identity) if isinstance(identity, str) else None
    if not match or match[1] != session or int(match[2]) > _MAX_COUNTER:
        return None
    age = value.get('ageMs')
    if not _number(age, 86_400_000) or age + elapsed_ms >= DETECTION_RESULT_TTL_MS:
        return None
    if (not _integer(value.get('width'), 1, 8192) or not _integer(value.get('height'), 1, 8192)
            or not _number(value.get('inferenceMs'), 10_000)):
        return None
    stream, media, image = value.get('streamSession'), value.get('mediaTime'), value.get('imageId')
    if key == 'color':
        if stream is not None or media is not None or not isinstance(image, str) or not _IMAGE.fullmatch(image):
            return None
    elif not video_session(stream) or not _number(media, _MAX_COUNTER) or image is not None:
        return None
    boxes = value.get('boxes')
    if not isinstance(boxes, list) or len(boxes) > 12:
        return None
    clean_boxes = []
    for box in boxes:
        if (not isinstance(box, dict) or not _integer(box.get('classId'), 0, 79)
                or not _number(box.get('confidence'), 1)):
            return None
        xyxy = box.get('xyxy')
        if (not isinstance(xyxy, list) or len(xyxy) != 4 or not all(_number(item, 1) for item in xyxy)
                or xyxy[0] >= xyxy[2] or xyxy[1] >= xyxy[3]):
            return None
        clean_boxes.append({'classId': box['classId'], 'confidence': box['confidence'], 'xyxy': list(xyxy)})
    clean_markers = _markers(value.get('markers', []))
    if clean_markers is None:
        return None
    return {'id': identity, 'streamSession': stream, 'mediaTime': media, 'imageId': image,
            'ageMs': age + elapsed_ms, 'width': value['width'], 'height': value['height'],
            'inferenceMs': value['inferenceMs'], 'boxes': clean_boxes, 'markers': clean_markers}


def sanitize_detections(value, elapsed_ms=0):
    """Copy only explicit public fields; malformed rows cannot draw detections."""
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('model') != MODEL or not isinstance(value.get('cameras'), dict)
            or not _number(elapsed_ms, 86_400_000)):
        raise ValueError('invalid detection document')
    session = value.get('session')
    if session is not None and not video_session(session):
        raise ValueError('invalid detection session')
    result = waiting_detections(session)
    seen = set()
    for key in DETECTION_KEYS:
        row = value['cameras'].get(key)
        if not isinstance(row, dict):
            continue
        status, rows = row.get('status'), row.get('results')
        if status not in ('AVAILABLE', 'WAITING_UPSTREAM', 'ERROR') or not isinstance(rows, list) or len(rows) > 10:
            continue
        if status != 'AVAILABLE':
            result['cameras'][key]['status'] = status
            continue
        if session is None:
            continue
        clean = []
        for item in rows:
            item = _result(item, key, session, elapsed_ms)
            if item is None:
                continue
            if item['id'] in seen:
                raise ValueError('duplicate detection result')
            seen.add(item['id'])
            clean.append(item)
        if clean:
            result['cameras'][key] = {'status': 'AVAILABLE', 'results': clean}
    return result
