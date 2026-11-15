"""Synthetic fixtures stay in isolated tests; they are never uploaded to a robot."""
import base64
import copy
import json
import struct
import time
import unittest
import uuid
import zlib

from shared.protocol import (CAMERA_KEYS, HTTP_CAMERA_KEYS, ProtocolError, decode_envelope, decode_media,
                             encode_envelope, validate_envelope)


def png():
    def chunk(typ, data):
        return struct.pack('>I', len(data)) + typ + data + struct.pack('>I', zlib.crc32(typ + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\x00')) + chunk(b'IEND', b''))


def jpeg():
    """A real 1x1 grayscale JPEG generated for isolated tests, without cv2 at runtime."""
    return base64.b64decode(
        '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAME'
        'BgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/wAALCAABAAEBAREA'
        '/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAA'
        'F9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJico'
        'KSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJip'
        'KTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl'
        '5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AP5/6//Z')


def envelope(kind='observation', sequence=1, timestamp=None, session=None):
    timestamp = time.time() if timestamp is None else timestamp
    payload = {k: {} for k in ('system', 'robot', 'devices', 'ros2', 'pages', 'errors')}
    if kind == 'heartbeat':
        payload = {'uptime_s': 0}
    elif kind == 'media':
        payload = {'key': 'map', 'kind': 'map', 'status': 'AVAILABLE', 'ageS': 0,
                   'data': base64.b64encode(png()).decode(), 'mime_type': 'image/png'}
    return {'schema_version': 1, 'robot_id': 'test-robot', 'source_id': 'rk3588',
            'session_id': session or str(uuid.uuid4()), 'sequence': sequence,
            'kind': kind, 'topic': {'observation': '/observer/observation',
                                   'heartbeat': '/observer/heartbeat', 'media': '/map'}[kind],
            'message_timestamp': None, 'collector_timestamp': timestamp, 'payload': payload}


class ProtocolTests(unittest.TestCase):
    def test_truth_preserved_and_non_finite_null(self):
        obj = envelope()
        obj['payload']['robot'] = {'zero': 0, 'missing': None, 'bad': float('nan'),
                                   'bad2': float('inf'), 'command': 1, 'actual': 0}
        result = validate_envelope(obj)
        self.assertEqual(result['payload']['robot'], {'zero': 0, 'missing': None, 'bad': None,
                                                     'bad2': None, 'command': 1, 'actual': 0})
        json.loads(encode_envelope(result))

    def test_reject_schema_identity_and_timestamp(self):
        for field, value in [('schema_version', True), ('sequence', True), ('sequence', 0),
                             ('session_id', 'not-uuid'), ('robot_id', '../robot'),
                             ('collector_timestamp', True), ('collector_timestamp', time.time() + 1000),
                             ('kind', {}), ('topic', 'http://localhost/')]:
            with self.subTest(field=field, value=value):
                obj = envelope()
                obj[field] = value
                with self.assertRaises(ProtocolError):
                    validate_envelope(obj)
        obj = envelope()
        obj['payload']['extra'] = {}
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)

    def test_duplicate_json_and_collection_depth_bounds(self):
        with self.assertRaises(ProtocolError):
            decode_envelope(b'{"schema_version":1,"schema_version":1}')
        obj = envelope()
        obj['payload']['system']['items'] = [0] * 4097
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)
        obj = envelope()
        node = obj['payload']['system']
        for _ in range(20):
            node['child'] = {}
            node = node['child']
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)

    def test_valid_png_and_reject_signature_crc_bomb(self):
        obj = envelope('media')
        self.assertEqual(decode_media(validate_envelope(obj)['payload']), png())
        for bad in (b'<script>not a PNG</script>', png()[:-5], png() + b'extra'):
            obj = envelope('media')
            obj['payload']['data'] = base64.b64encode(bad).decode()
            with self.assertRaises(ProtocolError):
                validate_envelope(obj)
        obj = envelope('media')
        obj['payload']['mime_type'] = 'text/html'
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)

    def test_cloud_reject_bool_nonfinite_and_bound(self):
        obj = envelope('media')
        obj['payload'] = {'key': 'lidar', 'kind': 'cloud', 'status': 'AVAILABLE', 'points': [[0, 0, 0]]}
        self.assertEqual(validate_envelope(obj)['payload']['points'], [[0, 0, 0]])
        for points in ([[True, 0, 0]], [[float('nan'), 0, 0]], [[1, 2]], [[0, 0, 0]] * 4001):
            obj['payload']['points'] = points
            with self.assertRaises(ProtocolError):
                validate_envelope(obj)

    def test_scan_keys_use_bounded_cloud_contract_not_images(self):
        for key in ('bluesea', 'scan'):
            with self.subTest(key=key):
                obj = envelope('media')
                obj['topic'] = '/bluesea_new/scan' if key == 'bluesea' else '/scan'
                obj['payload'] = {'key': key, 'kind': 'cloud', 'status': 'AVAILABLE',
                                  'points': [[1.0, 0.0, 0.0]], 'totalPoints': 1,
                                  'frameId': 'isolated_scan_frame', 'note': '二维扫描，Z=0'}
                self.assertEqual(validate_envelope(obj)['payload']['points'], [[1.0, 0.0, 0.0]])
                for bad in ({'kind': 'image'}, {'points': [[float('inf'), 0, 0]]},
                            {'points': [[0, 0, 0]] * 4001}, {'points': [[1_000_001, 0, 0]]},
                            {'data': ''}, {'mime_type': 'image/jpeg'}):
                    invalid = copy.deepcopy(obj)
                    invalid['payload'].update(bad)
                    with self.assertRaises(ProtocolError):
                        validate_envelope(invalid)
                for status in ('WAITING_UPSTREAM', 'NO_DATA', 'STALE', 'ERROR'):
                    obj['payload'] = {'key': key, 'kind': 'cloud', 'status': status}
                    if status == 'NO_DATA':
                        obj['payload'].update(points=[], totalPoints=0)
                    self.assertEqual(validate_envelope(obj)['payload']['status'], status)

    def test_unknown_media_fields_and_null_keys(self):
        obj = envelope('media')
        obj['payload']['url'] = 'http://private-device/'
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)

    def test_http_camera_keys_are_distinct_bounded_jpeg_images(self):
        self.assertEqual(HTTP_CAMERA_KEYS, {'usbFront', 'usbRear', 'usbLeft', 'usbHub2', 'usbHub3', 'usbPort21'})
        self.assertEqual(CAMERA_KEYS - HTTP_CAMERA_KEYS, {'fisheye', 'color', 'depth'})
        for key in sorted(CAMERA_KEYS):
            with self.subTest(key=key):
                obj = envelope('media')
                obj['topic'] = '/observer/http_camera/' + key
                obj['payload'] = {'key': key, 'kind': 'image', 'status': 'AVAILABLE',
                                  'data': base64.b64encode(jpeg()).decode(),
                                  'mime_type': 'image/jpeg', 'frameSeq': 0,
                                  'ageS': 0, 'message_timestamp': None}
                result = validate_envelope(obj)
                self.assertEqual(decode_media(result['payload']), jpeg())
                self.assertEqual(result['payload']['frameSeq'], 0)
                self.assertIsNone(result['message_timestamp'])
                for update in ({'kind': 'cloud'}, {'kind': 'map'}, {'mime_type': 'image/png'},
                               {'data': base64.b64encode(png()).decode()}, {'data': ''},
                               {'frameSeq': -1}, {'frameSeq': True}, {'ageS': -1},
                               {'key': 'usbRight'}):
                    invalid = copy.deepcopy(obj)
                    invalid['payload'].update(update)
                    with self.assertRaises(ProtocolError):
                        validate_envelope(invalid)
                invalid = copy.deepcopy(obj)
                invalid['payload'].pop('data')
                with self.assertRaises(ProtocolError):
                    validate_envelope(invalid)
                oversized = bytearray(jpeg())
                start = oversized.index(b'\xff\xc0')
                oversized[start + 7:start + 9] = struct.pack('>H', 1921)
                invalid['payload']['data'] = base64.b64encode(oversized).decode()
                with self.assertRaises(ProtocolError):
                    validate_envelope(invalid)
                for status in ('WAITING_UPSTREAM', 'NO_DATA', 'STALE', 'ERROR'):
                    obj['payload'] = {'key': key, 'kind': 'image', 'status': status,
                                      'ageS': None, 'message_timestamp': None}
                    self.assertEqual(validate_envelope(obj)['payload']['status'], status)

    def test_jpeg_empty_scan_rejected(self):
        obj = envelope('media')
        obj['payload'] = {'key': 'color', 'kind': 'image', 'status': 'AVAILABLE',
                          'mime_type': 'image/jpeg', 'data': base64.b64encode(b'\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x01\x01\xff\xda\x00\x02\xff\xd9').decode()}
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)

    def test_status_enum_checked_docking_state_preserved(self):
        obj = envelope()
        obj['payload']['devices'] = {'status': 'AVAILABLE', 'state': 'searching_for_dock'}
        self.assertEqual(validate_envelope(obj)['payload']['devices']['state'], 'searching_for_dock')
        obj['payload']['devices']['status'] = 'pretend_online'
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)
        obj = envelope()
        obj['payload']['robot'] = {'field': {'value': 0, 'type': 'ACTUAL', 'state': 'pretend_online'}}
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)
        obj = envelope('media')
        obj['payload']['key'] = {}
        with self.assertRaises(ProtocolError):
            validate_envelope(obj)


if __name__ == '__main__':
    unittest.main()
