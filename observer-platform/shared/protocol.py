"""Versioned bounded DTO validation shared by collector and cloud (no ROS)."""
from __future__ import annotations

from .faults import validate_events_payload
from .test_metrics import validate_test_metrics_payload

import base64
import binascii
import json
import math
import re
import struct
import time
import uuid
import zlib

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 1_048_576
MAX_OBSERVATION_BYTES = 262_144
MAX_MEDIA_BYTES = 512_000
# Ordinary USB previews have their own identities; never impersonate the ROS
# fisheye, depth-camera RGB, or depth frames when bridged from local HTTP.
HTTP_CAMERA_KEYS = frozenset({'usbFront', 'usbRear', 'usbLeft', 'usbHub2', 'usbHub3', 'usbPort21'})
CAMERA_KEYS = frozenset({'fisheye', 'color', 'depth'}) | HTTP_CAMERA_KEYS
MAP_KEYS = frozenset({'map', 'localCostmap', 'globalCostmap'})
CLOUD_KEYS = frozenset({'lidar', 'registered', 'depthPoints', 'bluesea', 'scan'})
MEDIA_KEYS = CAMERA_KEYS | MAP_KEYS | CLOUD_KEYS
STATUSES = frozenset({'AVAILABLE', 'WAITING_UPSTREAM', 'STALE', 'NO_DATA', 'ERROR',
                      'UNAVAILABLE', 'IDLE', 'DISCONNECTED', 'ONLINE', 'OFFLINE'})
OBSERVATION_KEYS = frozenset({'system', 'robot', 'devices', 'ros2', 'pages', 'errors'})
ENVELOPE_KEYS = frozenset({'schema_version', 'robot_id', 'source_id', 'session_id',
                         'sequence', 'kind', 'topic', 'message_timestamp',
                         'collector_timestamp', 'payload'})
MEDIA_FIELDS = frozenset({'key', 'kind', 'status', 'ageS', 'hz', 'frameId', 'frameSeq',
    'message_timestamp', 'label', 'topic', 'reason', 'source', 'width', 'height',
    'previewWidth', 'previewHeight', 'encoding', 'sourceHz', 'sourcePublisherCount',
    'error', 'previewHz', 'receivedFrames', 'sequence', 'note', 'totalPoints',
    'resolutionM', 'origin', 'orientation', 'content_hash', 'data', 'mime_type', 'points', 'received_timestamp'})
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


class ProtocolError(ValueError):
    """Invalid input; the message is safe to return without echoing request data."""


def _number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ProtocolError(f'invalid {name}')
    return value


def _safe(value, depth=0, budget=None):
    if budget is None:
        budget = [24_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 18:
        raise ProtocolError('DTO nesting or field count exceeded')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2 ** 63 - 1:
            raise ProtocolError('integer out of range')
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if len(value) > 8192 or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
            raise ProtocolError('invalid text field')
        return value
    if isinstance(value, list):
        if len(value) > 4096:
            raise ProtocolError('collection too long')
        return [_safe(v, depth + 1, budget) for v in value]
    if isinstance(value, dict):
        if len(value) > 512 or not all(isinstance(k, str) and 0 < len(k) <= 200 for k in value):
            raise ProtocolError('invalid object fields')
        if 'status' in value and (not isinstance(value['status'], str) or value['status'] not in STATUSES):
            raise ProtocolError('invalid status enum')
        if ('state' in value and value.get('type') in ('COMMAND', 'ACTUAL')
                and (not isinstance(value['state'], str) or value['state'] not in STATUSES)):
            raise ProtocolError('invalid engineering field state enum')
        return {k: _safe(v, depth + 1, budget) for k, v in value.items()}
    raise ProtocolError('unsupported JSON value')


def encode_envelope(envelope):
    """Serialize finite JSON only. Call validate_envelope first for untrusted DTOs."""
    return json.dumps(envelope, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def decode_envelope(raw, now=None):
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError('request too large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError('duplicate JSON key')
            result[key] = value
        return result
    try:
        obj = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError('invalid JSON') from exc
    return validate_envelope(obj, now=now)


def _jpeg_dimensions(raw):
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise ProtocolError('invalid JPEG signature')
    position, dimensions, saw_scan = 2, None, False
    while position < len(raw) - 2:
        if raw[position] != 0xff:
            raise ProtocolError('invalid JPEG marker')
        while position < len(raw) and raw[position] == 0xff:
            position += 1
        if position >= len(raw):
            break
        marker = raw[position]
        position += 1
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        if position + 2 > len(raw):
            raise ProtocolError('truncated JPEG')
        length = int.from_bytes(raw[position:position + 2], 'big')
        if length < 2 or position + length > len(raw):
            raise ProtocolError('invalid JPEG length')
        if marker in {0xc0, 0xc1, 0xc2}:
            if length < 11:
                raise ProtocolError('invalid JPEG dimensions')
            precision, components = raw[position + 2], raw[position + 7]
            if precision != 8 or components not in (1, 3) or length != 8 + 3 * components:
                raise ProtocolError('unsupported JPEG components')
            height, width = struct.unpack('>HH', raw[position + 3:position + 7])
            dimensions = (width, height)
        if marker == 0xda:
            components = raw[position + 2] if length > 2 else 0
            if components not in (1, 3) or length != 6 + 2 * components or position + length >= len(raw) - 2:
                raise ProtocolError('invalid JPEG scan')
            saw_scan = True
            break
        position += length
    if dimensions is None or not saw_scan:
        raise ProtocolError('JPEG lacks image or scan')
    return dimensions


def _png_dimensions(raw):
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ProtocolError('invalid PNG signature')
    position, dimensions, idat, ended = 8, None, [], False
    bit_depth = color_type = None
    while position + 12 <= len(raw):
        length = int.from_bytes(raw[position:position + 4], 'big')
        typ = raw[position + 4:position + 8]
        end = position + 12 + length
        if end > len(raw):
            raise ProtocolError('truncated PNG')
        data = raw[position + 8:position + 8 + length]
        crc = int.from_bytes(raw[position + 8 + length:end], 'big')
        if zlib.crc32(typ + data) & 0xffffffff != crc:
            raise ProtocolError('invalid PNG checksum')
        if dimensions is None and typ != b'IHDR':
            raise ProtocolError('PNG missing initial header')
        if typ == b'IHDR':
            if dimensions is not None or length != 13:
                raise ProtocolError('invalid PNG header')
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack('>IIBBBBB', data)
            if bit_depth != 8 or color_type not in (0, 2, 4, 6) or compression or filter_method or interlace:
                raise ProtocolError('unsupported PNG encoding')
            dimensions = (width, height)
            _check_dimensions(dimensions)
        elif typ == b'IDAT':
            idat.append(data)
        elif typ == b'IEND':
            if length or end != len(raw):
                raise ProtocolError('invalid PNG end')
            ended = True
            break
        elif typ[0] & 32 == 0:
            raise ProtocolError('unsupported PNG critical chunk')
        position = end
    if not ended or not idat or dimensions is None:
        raise ProtocolError('incomplete PNG')
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    expected = dimensions[1] * (1 + dimensions[0] * channels)
    try:
        stream = zlib.decompressobj()
        pixels = stream.decompress(b''.join(idat), expected + 1)
        if len(pixels) != expected or not stream.eof or stream.unused_data or stream.unconsumed_tail:
            raise ProtocolError('invalid PNG pixel data')
        row_size = dimensions[0] * channels + 1
        if any(pixels[p] > 4 for p in range(0, len(pixels), row_size)):
            raise ProtocolError('invalid PNG filter')
    except zlib.error as exc:
        raise ProtocolError('invalid PNG compression') from exc
    return dimensions


def _check_dimensions(dimensions):
    width, height = dimensions
    if not 0 < width <= 1920 or not 0 < height <= 1080 or width * height > 2_073_600:
        raise ProtocolError('media dimensions exceeded')


def decode_media(payload):
    """Return validated immutable JPEG/PNG bytes, or None for cloud/metadata."""
    if 'data' not in payload:
        return None
    encoded = payload['data']
    if not isinstance(encoded, str) or len(encoded) > (MAX_MEDIA_BYTES + 2) // 3 * 4:
        raise ProtocolError('media too large')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProtocolError('invalid media base64') from exc
    if not 16 <= len(raw) <= MAX_MEDIA_BYTES:
        raise ProtocolError('invalid media size')
    mime = payload.get('mime_type')
    if payload['kind'] == 'image' and mime == 'image/jpeg':
        dimensions = _jpeg_dimensions(raw)
    elif payload['kind'] == 'map' and mime == 'image/png':
        dimensions = _png_dimensions(raw)
    else:
        raise ProtocolError('invalid media MIME type')
    _check_dimensions(dimensions)
    return raw


def validate_envelope(envelope, now=None):
    """Validate schema and bounds, preserving true zero and converting NaN/Inf to null."""
    now = time.time() if now is None else now
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS:
        raise ProtocolError('invalid envelope fields')
    if type(envelope['schema_version']) is not int or envelope['schema_version'] != 1:
        raise ProtocolError('unsupported schema version')
    for key in ('robot_id', 'source_id'):
        if not isinstance(envelope[key], str) or not IDENTIFIER.fullmatch(envelope[key]):
            raise ProtocolError(f'invalid {key}')
    try:
        session = uuid.UUID(envelope['session_id'])
        if str(session) != envelope['session_id']:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProtocolError('invalid session_id') from exc
    sequence = envelope['sequence']
    if type(sequence) is not int or not 1 <= sequence <= 2 ** 63 - 1:
        raise ProtocolError('invalid sequence')
    kind, topic = envelope['kind'], envelope['topic']
    if not isinstance(kind, str) or kind not in ('observation', 'heartbeat', 'media', 'events', 'test_metrics'):
        raise ProtocolError('invalid kind')
    if not isinstance(topic, str) or len(topic) > 200 or not topic.startswith('/') or any(ord(c) < 33 for c in topic):
        raise ProtocolError('invalid topic')
    for key in ('collector_timestamp', 'message_timestamp'):
        value = envelope[key]
        if key == 'message_timestamp' and value is None:
            continue
        if _number(value, key) > now + 300:
            raise ProtocolError('timestamp too far in future')
    payload = envelope['payload']
    if not isinstance(payload, dict):
        raise ProtocolError('payload must be an object')
    if kind == 'observation':
        if set(payload) != OBSERVATION_KEYS or topic != '/observer/observation':
            raise ProtocolError('invalid observation fields or topic')
        if not all(isinstance(payload[k], dict) for k in OBSERVATION_KEYS):
            raise ProtocolError('observation sections must be objects')
        payload = _safe(payload)
        if len(encode_envelope(payload)) > MAX_OBSERVATION_BYTES:
            raise ProtocolError('observation too large')
    elif kind == 'events':
        if topic != '/observer/events':
            raise ProtocolError('invalid events topic')
        try:
            payload = validate_events_payload(payload)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ProtocolError('invalid events payload') from exc
    elif kind == 'test_metrics':
        if topic != '/observer/test_metrics':
            raise ProtocolError('invalid test metrics topic')
        try:
            payload = validate_test_metrics_payload(payload, now=now)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ProtocolError('invalid test metrics payload') from exc
    elif kind == 'heartbeat':
        if set(payload) != {'uptime_s'} or topic != '/observer/heartbeat':
            raise ProtocolError('invalid heartbeat fields or topic')
        _number(payload['uptime_s'], 'uptime_s')
        payload = dict(payload)
    else:
        if set(payload) - MEDIA_FIELDS or not {'key', 'kind', 'status'} <= set(payload):
            raise ProtocolError('invalid media fields')
        key = payload['key']
        if not isinstance(key, str) or key not in MEDIA_KEYS or not isinstance(payload['status'], str) or payload['status'] not in STATUSES:
            raise ProtocolError('invalid media key or status')
        expected = 'image' if key in CAMERA_KEYS else 'map' if key in MAP_KEYS else 'cloud'
        if payload['kind'] != expected:
            raise ProtocolError('media key/kind mismatch')
        # The base64 field is checked separately, not treated as a general text DTO.
        payload = dict(_safe({k: v for k, v in payload.items() if k != 'data'}), **({'data': payload['data']} if 'data' in payload else {}))
        for field in ('ageS', 'hz', 'previewHz', 'sourceHz', 'resolutionM', 'message_timestamp', 'received_timestamp'):
            if payload.get(field) is not None:
                _number(payload[field], field)
                if field.endswith('timestamp') and payload[field] > now + 300:
                    raise ProtocolError('media timestamp too far in future')
        if 'message_timestamp' in payload and payload['message_timestamp'] != envelope['message_timestamp']:
            raise ProtocolError('media sampling timestamp mismatch')
        for field in ('width', 'height', 'previewWidth', 'previewHeight', 'frameSeq', 'sequence', 'receivedFrames', 'totalPoints', 'sourcePublisherCount'):
            if payload.get(field) is not None and (type(payload[field]) is not int or payload[field] < 0):
                raise ProtocolError(f'invalid {field}')
        if expected == 'cloud':
            if 'data' in payload or 'mime_type' in payload:
                raise ProtocolError('cloud cannot contain binary data')
            points = payload.get('points', [])
            if not isinstance(points, list) or len(points) > 4000:
                raise ProtocolError('too many cloud points')
            for point in points:
                if not isinstance(point, list) or len(point) != 3:
                    raise ProtocolError('invalid XYZ point')
                for value in point:
                    _number(value, 'XYZ coordinate', -1_000_000)
                    if value > 1_000_000:
                        raise ProtocolError('XYZ coordinate out of range')
            if payload['status'] == 'AVAILABLE' and 'points' not in payload:
                raise ProtocolError('available cloud lacks points')
        elif payload['status'] == 'AVAILABLE' and 'data' not in payload:
            raise ProtocolError('available image lacks data')
        decode_media(payload)
    result = dict(envelope, payload=payload)
    if len(encode_envelope(result)) > MAX_REQUEST_BYTES:
        raise ProtocolError('request too large')
    return result
