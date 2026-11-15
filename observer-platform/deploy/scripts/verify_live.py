#!/usr/bin/env python3
"""Read-only HTTPS/WSS acceptance check; run in the ECS verification container.

Requires Python 3.12+ and aiohttp. Mount the initial-access JSON read-only at
/run/observer-initial-access.json. Only login/logout change server session state;
no ingestion requests or WebSocket application messages are sent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import socket
import ssl
import stat
import sys

ORIGIN = 'https://aystkit.cn'
CREDENTIALS = '/run/observer-initial-access.json'
COOKIE = 'observer_session'
HTTP_CAMERAS = ('usbFront', 'usbRear', 'usbLeft')
ALL_HTTP_CAMERAS = HTTP_CAMERAS + ('usbHub2', 'usbHub3', 'usbPort21')
CAMERAS = ALL_HTTP_CAMERAS + ('fisheye', 'color', 'depth')
VISUALS = ('map', 'localCostmap', 'globalCostmap', 'lidar', 'registered', 'depthPoints',
           'bluesea', 'scan')
SCANS = ('bluesea', 'scan')
ASSETS = ('app.css', 'app.js', 'formatters.js', 'charts.js', 'media.js',
          'cloud.js', 'cloud.css', 'login.js')
STATUSES = frozenset(('AVAILABLE', 'WAITING_UPSTREAM', 'STALE', 'NO_DATA', 'ERROR',
                      'UNAVAILABLE', 'IDLE', 'DISCONNECTED', 'ONLINE', 'OFFLINE'))
MAX_BODY = 2 * 1024 * 1024


class VerificationError(Exception):
    """Messages contain only fixed check names, never response or credential data."""


def camera_keys_value(value):
    keys = tuple(item.strip() for item in value.split(',')) if isinstance(value, str) else ()
    if not keys or len(keys) != len(set(keys)) or any(key not in ALL_HTTP_CAMERAS for key in keys):
        raise argparse.ArgumentTypeError('camera-keys must be unique known HTTP camera names')
    return keys


def require(condition, check):
    if not condition:
        raise VerificationError(check)


def report(check, **values):
    print(json.dumps({'check': check, **values}, separators=(',', ':'),
                     allow_nan=False), flush=True)


def section(value):
    return value if isinstance(value, dict) else {}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def status(value):
    return value if isinstance(value, str) and value in STATUSES else 'UNKNOWN'


def credentials(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077,
                'credentials_private_regular_file')
        raw = stream.read(4097)
    require(len(raw) <= 4096, 'credentials_size')
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) == {'username', 'password'},
            'credentials_fields')
    require(isinstance(value['username'], str) and 1 <= len(value['username']) <= 64
            and isinstance(value['password'], str) and bool(value['password']),
            'credentials_values')
    return value


async def read_json(response):
    require(response.content_type == 'application/json', 'json_content_type')
    raw = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        raw.extend(chunk)
        require(len(raw) <= MAX_BODY, 'response_size')
    value = json.loads(raw)
    require(isinstance(value, dict), 'json_object')
    return value


def jpeg_dimensions(raw):
    """Bounded JPEG header/scan validation, without logging camera pixels."""
    require(raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9'), 'usb_jpeg_signature')
    offset, dimensions = 2, None
    while offset < len(raw) - 2:
        require(raw[offset] == 0xff, 'usb_jpeg_marker')
        while offset < len(raw) and raw[offset] == 0xff:
            offset += 1
        require(offset < len(raw), 'usb_jpeg_marker_end')
        marker = raw[offset]
        offset += 1
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        require(offset + 2 <= len(raw), 'usb_jpeg_segment_header')
        length = int.from_bytes(raw[offset:offset + 2], 'big')
        require(length >= 2 and offset + length <= len(raw), 'usb_jpeg_segment_size')
        if marker in (0xc0, 0xc1, 0xc2):
            require(length >= 11, 'usb_jpeg_frame_size')
            components = raw[offset + 7]
            require(raw[offset + 2] == 8 and components in (1, 3)
                    and length == 8 + 3 * components, 'usb_jpeg_frame_format')
            height = int.from_bytes(raw[offset + 3:offset + 5], 'big')
            width = int.from_bytes(raw[offset + 5:offset + 7], 'big')
            require(0 < width <= 4096 and 0 < height <= 4096, 'usb_jpeg_dimensions')
            dimensions = (width, height)
        if marker == 0xda:
            components = raw[offset + 2] if length > 2 else 0
            require(dimensions is not None and components in (1, 3)
                    and length == 6 + 2 * components
                    and offset + length < len(raw) - 2, 'usb_jpeg_scan')
            return dimensions
        offset += length
    raise VerificationError('usb_jpeg_missing_scan')


def frame_identity(value):
    require(isinstance(value, str) and len(value) <= 256, 'usb_frame_sequence_present')
    session, separator, counter = value.rpartition(':')
    require(bool(session) and separator == ':' and counter.isascii() and counter.isdigit(),
            'usb_frame_sequence_format')
    return session, int(counter)


async def verify_http_cameras(session, args, snapshot):
    cameras = section(section(snapshot.get('devices')).get('cameras'))

    async def sample(key):
        async with session.get(args.origin + f'/camera/{key}.jpg', allow_redirects=False) as response:
            require(response.status in (200, 204), 'usb_camera_http_status')
            if response.status == 204:
                return None
            require(response.content_type == 'image/jpeg', 'usb_camera_content_type')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                raw.extend(chunk)
                require(len(raw) <= MAX_BODY, 'usb_camera_size')
            dimensions = jpeg_dimensions(raw)
            identity = frame_identity(response.headers.get('X-Frame-Sequence'))
            return dimensions, identity, len(raw)

    for key in args.camera_keys:
        metadata = section(cameras.get(key))
        state = status(metadata.get('status'))
        require(state != 'UNKNOWN', 'usb_camera_metadata_present')
        first = await sample(key)
        if args.require_usb_cameras:
            require(state == 'AVAILABLE' and first is not None, 'usb_camera_live_required')
        if first is None:
            report('http_camera:' + key, status=state, jpegAvailable=False)
            continue
        dimensions, identity, byte_count = first
        require(dimensions == (number(metadata.get('previewWidth', metadata.get('width'))),
                               number(metadata.get('previewHeight', metadata.get('height')))),
                'usb_jpeg_metadata_dimensions')
        second = None
        for _ in range(8):
            await asyncio.sleep(.6)
            candidate = await sample(key)
            if candidate is not None and candidate[1] != identity:
                second = candidate
                break
        advanced = bool(second and second[1][0] == identity[0] and second[1][1] > identity[1])
        if args.require_usb_cameras:
            require(advanced, 'usb_camera_sequence_progress')
        report('http_camera:' + key, status=state, jpegAvailable=True,
               jpegHeaderValid=True, width=dimensions[0], height=dimensions[1],
               bytes=byte_count, frameSequenceAdvanced=advanced)


def summarize(snapshot, visuals):
    cloud = section(snapshot.get('cloud'))
    robot = section(snapshot.get('robot'))
    topics = section(section(snapshot.get('ros2')).get('topics'))
    telemetry = section(robot.get('telemetry') or topics.get('/chassis_host/telemetry'))
    report('cloud', online=cloud.get('online') if type(cloud.get('online')) is bool else None,
           status=status(cloud.get('status')), ageS=number(cloud.get('ageS')))
    report('telemetry', status=status(telemetry.get('status')),
           publisherCount=number(telemetry.get('publisherCount')),
           messageCount=number(telemetry.get('messageCount')),
           transport='LOCAL_WS_RX' if telemetry.get('transport') == 'LOCAL_WS_RX' else 'ROS2',
           hz=number(telemetry.get('hz')), ageS=number(telemetry.get('ageS')))
    pages = section(snapshot.get('pages'))
    report('pages', **{key: {'status': status(section(pages.get(key)).get('status')),
                           'count': number(section(pages.get(key)).get('count'))}
                        for key in ('0x00', '0x10', '0x11', '0x12', '0x13')})
    fields = section(section(section(robot.get('data')).get('debug')).get('fields'))
    report('actual_feedback', **{key: {
        'value': number(section(fields.get(key)).get('value')),
        'state': status(section(fields.get(key)).get('state')),
        'actual': section(fields.get(key)).get('type') == 'ACTUAL'}
        for key in ('busVoltage', 'leftWheelMps', 'rightWheelMps', 'actualVxMps', 'actualWzRadps')})
    cameras = section(section(snapshot.get('devices')).get('cameras'))
    report('modules', **{key: status(section(cameras.get(key)).get('status')) for key in CAMERAS},
           **{key: status(section(visuals.get(key)).get('status')) for key in VISUALS})
    sensors = section(section(snapshot.get('devices')).get('sensors'))
    report('scan_receivers', **{key: {
        'status': status(section(visuals.get(key)).get('status')),
        'publisherCount': number(section(sensors.get(key)).get('publisherCount')),
        'messageCount': number(section(sensors.get(key)).get('messageCount')),
        'hz': number(section(sensors.get(key)).get('hz')),
        'sampledPoints': len(section(visuals.get(key)).get('points') or [])}
        for key in SCANS})
    system = section(snapshot.get('rk3588'))
    npu = section(system.get('npu'))
    report('system_metrics', status=status(system.get('status')),
           cpuPercent=number(section(system.get('cpu')).get('value')),
           ramPercent=number(section(section(system.get('ram')).get('usage')).get('value')),
           diskPercent=number(section(section(system.get('disk')).get('usage')).get('value')),
           uptimeS=number(system.get('uptimeS')),
           temperatureC=number(section(system.get('primaryThermal')).get('temperatureC')),
           npuStatus=status(npu.get('status')),
           npuPercent=number(section(npu.get('utilization')).get('value')),
           npuFrequencyHz=number(section(npu.get('frequencyHz')).get('value')))


async def verify(args):
    import aiohttp

    class LoopbackResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            require(host == 'aystkit.cn', 'resolver_hostname')
            return [{'hostname': host, 'host': '127.0.0.1', 'port': port,
                     'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP,
                     'flags': socket.AI_NUMERICHOST}]

        async def close(self):
            pass

    # The URL hostname controls Host and TLS SNI/hostname validation even when
    # only the TCP destination is overridden. Never disable certificate checks.
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(),
        resolver=LoopbackResolver() if args.connect_loopback else None)
    async with aiohttp.ClientSession(connector=connector,
            timeout=aiohttp.ClientTimeout(total=20), trust_env=False,
            headers={'Origin': args.origin}, cookie_jar=aiohttp.CookieJar()) as session:
        async def request(check, path, expected, *, method='GET', parse=False, cookies=None):
            async with session.request(method, args.origin + path, cookies=cookies,
                                       allow_redirects=False) as response:
                report(check, status=response.status)
                require(response.status == expected, check + '_http_status')
                return await read_json(response) if parse else None

        for path, expected in (('/healthz', 'ok'), ('/readyz', 'ready')):
            value = await request(path, path, 200, parse=True)
            require(value.get('status') == expected, 'health_readiness_body')
        for path in ('/snapshot.json', '/visuals.json', '/ws', '/camera/fisheye.jpg',
                     *(f'/camera/{key}.jpg' for key in ALL_HTTP_CAMERAS)):
            await request('anonymous:' + path, path, 401)

        logged_in, issued_cookie = False, None
        try:
            login = credentials(args.credentials)
            try:
                async with session.post(args.origin + '/api/login', json=login,
                                        allow_redirects=False) as response:
                    report('login', status=response.status)
                    require(response.status == 200, 'login_http_status')
                    logged_in = True
                    cookie = response.cookies.get(COOKIE)
                    issued_cookie = cookie.value if cookie else None
                    require(cookie is not None and issued_cookie, 'session_cookie_present')
                    require(cookie['secure'] and cookie['httponly']
                            and cookie['samesite'].lower() == 'strict'
                            and cookie['path'] == '/'
                            and cookie['max-age'].isdigit() and int(cookie['max-age']) > 0,
                            'session_cookie_flags')
                    report('session_cookie_flags', status='PASS')
                    value = await read_json(response)
                    require(value.get('authenticated') is True, 'login_authenticated')
            finally:
                login.clear()

            value = await request('session', '/api/session', 200, parse=True)
            require(value.get('authenticated') is True, 'session_authenticated')
            await request('homepage', '/', 200)
            for name in ASSETS:
                await request('asset:' + name, '/' + name, 200)
            snapshot = await request('snapshot', '/snapshot.json', 200, parse=True)
            visuals = await request('visuals', '/visuals.json', 200, parse=True)
            require(all(key in visuals and status(section(visuals[key]).get('status')) != 'UNKNOWN'
                        for key in VISUALS), 'visual_modules_present')
            report('visual_modules_present', status='PASS', count=len(VISUALS))
            require(section(snapshot.get('meta')).get('readOnly') is True, 'snapshot_read_only')
            timestamps = []
            # Exactly one authenticated WSS connection; receive only. Disable
            # automatic pings/pongs; the two frames arrive before server heartbeat.
            async with session.ws_connect('wss://aystkit.cn/ws', origin=args.origin,
                    autoping=False, heartbeat=None, max_msg_size=MAX_BODY) as ws:
                for _ in range(2):
                    frame = await asyncio.wait_for(ws.receive(), timeout=10)
                    require(frame.type == aiohttp.WSMsgType.TEXT, 'wss_text_frame')
                    snapshot = json.loads(frame.data)
                    require(isinstance(snapshot, dict), 'wss_snapshot_object')
                    meta = section(snapshot.get('meta'))
                    require(meta.get('readOnly') is True, 'wss_read_only')
                    timestamp = number(meta.get('timestamp'))
                    require(timestamp is not None, 'wss_timestamp_present')
                    timestamps.append(timestamp)
            require(timestamps[1] > timestamps[0], 'wss_timestamp_progress')
            report('wss', status='PASS', frames=2, timestampAdvanced=True)
            summarize(snapshot, visuals)
            await verify_http_cameras(session, args, snapshot)
        finally:
            if logged_in:
                value = await request('logout', '/api/logout', 200, method='POST', parse=True)
                require(value.get('authenticated') is False, 'logout_authenticated_false')
                session.cookie_jar.clear()
                require(issued_cookie is not None, 'revoked_cookie_present')
                # Replay the original cookie to verify server-side revocation.
                revoked = {COOKIE: issued_cookie}
                await request('revoked_session', '/api/session', 401, cookies=revoked)
                await request('revoked_snapshot', '/snapshot.json', 401, cookies=revoked)
    report('verification', status='PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', choices=(ORIGIN,), default=ORIGIN)
    parser.add_argument('--credentials', choices=(CREDENTIALS,), default=CREDENTIALS)
    parser.add_argument('--connect-loopback', action='store_true',
                        help='Connect TCP to 127.0.0.1, retaining domain URL, SNI and TLS validation')
    parser.add_argument('--require-usb-cameras', action='store_true',
                        help='Require all selected real HTTP cameras and advancing JPEG frame sequences')
    parser.add_argument('--camera-keys', type=camera_keys_value, default=HTTP_CAMERAS,
                        help='Comma-separated known HTTP camera keys; legacy three by default')
    args = parser.parse_args()
    try:
        asyncio.run(verify(args))
    except VerificationError as exc:
        report('verification', status='FAIL', reason=str(exc))
        return 1
    except KeyboardInterrupt:
        report('verification', status='FAIL', reason='interrupted')
        return 130
    except Exception as exc:
        # Exception messages, response bodies and tracebacks may contain secrets.
        report('verification', status='FAIL', reason=type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
