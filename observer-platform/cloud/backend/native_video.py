"""Opt-in, authenticated, GET-only proxy for an existing native fMP4 preview.

The upstream is one deployment-owned Unix socket, never a browser-provided
address. No camera is opened and no encoder, ROS node or robot control is run.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic, monotonic as stream_clock

import aiohttp
from aiohttp import web

from shared.detections import (DETECTION_MAX_BYTES, decode_detections, sanitize_detections,
                               video_session, waiting_detections)

NATIVE_VIDEO = web.AppKey('native_video', object)
# Transport aliases retain their physical sources even when labels are corrected.
DIRECTION_KEYS = {'front': 'usbLeft', 'rear': 'usbFront', 'left': 'usbHub2', 'right': 'usbHub3'}
SOURCE_IDS = {'front': 'camera1', 'rear': 'camera2', 'left': 'camera4', 'right': 'camera3'}
LABELS = {'front': '前视相机', 'rear': '后视相机', 'left': '左视相机', 'right': '右视相机'}
KEYS = frozenset(DIRECTION_KEYS.values())
MAX_BODY = 1_048_576
CHUNK_BYTES = 65_536
MAX_STREAMS = 16
MAX_SESSION_STREAMS = 4
MAX_FRAGMENT_BYTES = 1_048_576
MAX_GOP_BYTES = 2_097_152
MAX_GOP_FRAGMENTS = 16
MAX_GOP_AGE = 2.0
MAX_PENDING_BYTES = 2_097_152
MAX_PENDING_FRAGMENTS = 12
MAX_PENDING_AGE = 2.0
WRITE_TIMEOUT = 2.0
COOKIE = 'observer_session'
CAMERA_USAGE_TTL_MS = 6000
CAMERA_OWNERS = frozenset(('WEB', 'OTHER', 'NONE', 'UNKNOWN'))


def accepts_detection_gzip(headers):
    """Conservative explicit gzip negotiation; malformed/zero weights use identity."""
    values = headers.getall('Accept-Encoding', [])
    if len(values) > 4 or sum(len(value) for value in values) > 4096:
        return False
    found = None
    for token in ','.join(values).split(','):
        parts = [part.strip() for part in token.split(';')]
        if parts[0].lower() != 'gzip':
            continue
        if found is not None or len(parts) > 2:
            return False
        quality = 1.0
        if len(parts) == 2:
            match = re.fullmatch(r'q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)', parts[1], re.IGNORECASE)
            if not match:
                return False
            quality = float(match[1])
        found = quality > 0
    return found is True


def socket_path(value):
    if not value:
        return ''
    if (not isinstance(value, str) or len(value.encode()) > 107 or not value.startswith('/')
            or value == '/' or os.path.normpath(value) != value
            or '..' in PurePosixPath(value).parts or re.search(r'[\s\x00-\x1f\x7f:?#%]', value)):
        raise ValueError('NATIVE_VIDEO_SOCKET must be a normalized absolute Unix socket path')
    return value


def split_socket_option(value):
    if value not in ('true', 'false'):
        raise ValueError('NATIVE_VIDEO_SPLIT_SOCKETS must be true or false')
    return value == 'true'


def number(value, maximum):
    return value if (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and 0 <= value <= maximum) else None


def frame_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2 ** 53 - 1 else None


def nonnegative_finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def sanitize_camera_usage(value, elapsed_ms=0):
    """Only a short-lived observation, never a device lock or permission grant.

    The gateway may cache its device scan. Its observation age must survive the
    proxy request, rather than acquiring a fresh lease whenever a viewer polls.
    Expired or malformed observations cannot assert either occupancy or absence.
    """
    usage = {}
    elapsed = elapsed_ms if nonnegative_finite(elapsed_ms) else None
    for key in KEYS:
        row = value.get(key) if isinstance(value, dict) else None
        result = {'owner': 'UNKNOWN', 'connected': None, 'checkedAgeMs': None}
        if isinstance(row, dict) and elapsed is not None:
            owner, connected, age = row.get('owner'), row.get('connected'), row.get('checkedAgeMs')
            if (isinstance(owner, str) and owner in CAMERA_OWNERS and 'connected' in row
                    and (connected is None or isinstance(connected, bool))
                    and nonnegative_finite(age) and nonnegative_finite(age + elapsed)):
                age += elapsed
                result['checkedAgeMs'] = age
                if age < CAMERA_USAGE_TTL_MS:
                    result.update(owner=owner, connected=connected)
        usage[key] = result
    return usage


def sanitize_status(value, elapsed_ms=0):
    cameras = {}
    if not isinstance(value, dict) or not isinstance(value.get('cameras'), list):
        raise ValueError('invalid native preview status')
    if len(value['cameras']) > 32:
        raise ValueError('native preview camera list exceeded')
    ambiguous = set()
    for item in value['cameras']:
        if not isinstance(item, dict):
            continue
        direction, identity = item.get('direction'), item.get('id')
        if isinstance(identity, int) and not isinstance(identity, bool):
            identity = str(identity)
        if not isinstance(direction, str) or direction not in DIRECTION_KEYS or identity != SOURCE_IDS[direction]:
            continue
        key = DIRECTION_KEYS[direction]
        if key in cameras or key in ambiguous:
            cameras.pop(key, None)
            ambiguous.add(key)
            continue
        codec = item.get('codec')
        codec = codec.lower() if isinstance(codec, str) and re.fullmatch(r'(?:h264|avc|avc1(?:\.[0-9a-fA-F]{6})?)', codec.lower()) else None
        cameras[key] = {'id': identity, 'direction': direction, 'label': LABELS[direction], 'codec': codec,
            'width': number(item.get('width'), 8192), 'height': number(item.get('height'), 8192),
            **{field: number(item.get(field), 240) for field in ('requested_fps', 'actual_fps', 'encoded_fps')},
            'h264_mbps': number(item.get('h264_mbps'), 1000),
            'frame_age_ms': number(item.get('frame_age_ms'), 86_400_000),
            'frames': frame_count(item.get('frames')),
            'encoded_frames': frame_count(item.get('encoded_frames')),
            # Never return upstream stack traces, paths, credentials or HTML.
            'error': '相机暂不可用' if item.get('error') else None,
            'video_error': '视频暂不可用' if item.get('video_error') else None,
            'videoUrl': '/native-camera/' + key + '/video.mp4'}
    result = {'enabled': True, 'cameras': cameras}
    # Old native previews have no occupancy interface; keep them compatible.
    if 'cameraUsage' in value:
        result['cameraUsage'] = sanitize_camera_usage(value['cameraUsage'], elapsed_ms)
    return result


async def bounded_body(response, maximum=MAX_BODY):
    if response.content_length is not None and response.content_length > maximum:
        raise ValueError('native preview status exceeded')
    body = bytearray()
    async for chunk in response.content.iter_chunked(CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > maximum:
            raise ValueError('native preview status exceeded')
    return bytes(body)


async def mp4_initialization(content):
    """Bounded ftyp + moov admission before any bytes reach the viewer."""
    output = bytearray()
    found_ftyp = False
    while len(output) < MAX_BODY:
        header = await content.readexactly(8)
        size, kind = int.from_bytes(header[:4], 'big'), header[4:]
        if size == 1:
            extra = await content.readexactly(8)
            header += extra
            size = int.from_bytes(extra, 'big')
        if size < len(header) or size > MAX_BODY - len(output):
            raise ValueError('invalid native MP4 initialization size')
        if kind not in (b'ftyp', b'moov', b'free', b'skip', b'wide') or (not found_ftyp and kind != b'ftyp'):
            raise ValueError('invalid native MP4 initialization')
        output.extend(header)
        output.extend(await content.readexactly(size - len(header)))
        if kind == b'ftyp':
            if found_ftyp or size < len(header) + 8:
                raise ValueError('invalid native MP4 file type')
            found_ftyp = True
        elif kind == b'moov':
            return bytes(output)
    raise ValueError('native MP4 initialization exceeded')


def _children(data):
    """Iterate bounded complete MP4 boxes, including extended-size headers."""
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('truncated MP4 box')
        size, kind = int.from_bytes(data[offset:offset + 4], 'big'), data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError('truncated extended MP4 box')
            size, header = int.from_bytes(data[offset + 8:offset + 16], 'big'), 16
        if size < header or size > len(data) - offset:
            raise ValueError('invalid MP4 box size')
        yield kind, data[offset + header:offset + size]
        offset += size


def fragment_random_access(moof):
    """Admit the existing single-video FFmpeg default-base-is-moof contract.

    Source fragments are not all keyframes: the encoder's 0.5 s closed GOP is
    muxed into 0.2 s fragments. A late viewer must start at an independently
    decodable first sample, established by tfhd/trun flags, not any moof.
    """
    boxes = list(_children(moof))
    if len(boxes) != 1 or boxes[0][0] != b'moof':
        raise ValueError('expected moof')
    tracks = [body for kind, body in _children(boxes[0][1]) if kind == b'traf']
    if len(tracks) != 1:
        raise ValueError('single-track video fragments required')
    children = list(_children(tracks[0]))
    headers = [body for kind, body in children if kind == b'tfhd']
    runs = [body for kind, body in children if kind == b'trun']
    if len(headers) != 1 or not runs:
        raise ValueError('missing fragment track metadata')
    header = headers[0]
    if len(header) < 8 or header[0] != 0:
        raise ValueError('invalid tfhd')
    flags = int.from_bytes(header[1:4], 'big')
    # No absolute base-data-offset or empty-duration fragment is spliceable.
    if not flags & 0x020000 or flags & ~0x02003A or not int.from_bytes(header[4:8], 'big'):
        raise ValueError('relative moof addressing required')
    offset, default_flags = 8, None
    for flag in (0x2, 0x8, 0x10, 0x20):
        if flags & flag:
            if offset + 4 > len(header):
                raise ValueError('truncated tfhd default')
            if flag == 0x20:
                default_flags = int.from_bytes(header[offset:offset + 4], 'big')
            offset += 4
    if offset != len(header):
        raise ValueError('invalid tfhd length')
    first_flags = None
    for index, run in enumerate(runs):
        if len(run) < 8 or run[0] not in (0, 1):
            raise ValueError('invalid trun')
        flags, count = int.from_bytes(run[1:4], 'big'), int.from_bytes(run[4:8], 'big')
        if flags & ~0x000F05 or not 1 <= count <= 4096 or flags & 4 and flags & 0x400:
            raise ValueError('unsupported trun layout')
        offset = 8 + (4 if flags & 1 else 0)
        sample_flags = default_flags
        if flags & 4:
            if offset + 4 > len(run):
                raise ValueError('truncated first-sample flags')
            sample_flags = int.from_bytes(run[offset:offset + 4], 'big')
            offset += 4
        fields = [flag for flag in (0x100, 0x200, 0x400, 0x800) if flags & flag]
        if len(run) != offset + count * len(fields) * 4:
            raise ValueError('invalid trun sample length')
        if flags & 0x400:
            at = offset + fields.index(0x400) * 4
            sample_flags = int.from_bytes(run[at:at + 4], 'big')
        if index == 0:
            first_flags = sample_flags
    return first_flags is not None and first_flags & 0x03010000 == 0x02000000


async def _next_box(content):
    """Read one complete bounded box; EOF is legal only between boxes."""
    try:
        header = await content.readexactly(8)
    except asyncio.IncompleteReadError as error:
        if not error.partial:
            return None
        raise
    size, kind = int.from_bytes(header[:4], 'big'), header[4:8]
    if size == 1:
        extra = await content.readexactly(8)
        header += extra
        size = int.from_bytes(extra, 'big')
    if size < len(header) or size > MAX_FRAGMENT_BYTES:
        raise ValueError('native MP4 fragment exceeded')
    return kind, header + await content.readexactly(size - len(header))


@dataclass(frozen=True)
class Fragment:
    data: bytes
    random_access: bool
    received_at: float


class Subscriber:
    """Each viewer has a bounded queue; it never blocks the source reader."""
    def __init__(self, on_slow=None):
        self.queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.pending_bytes = 0
        self.started = self.ended = self.failed = False
        self.on_slow = on_slow
        self.slow_notified = False

    def slow(self):
        if self.slow_notified:
            return
        self.slow_notified = True
        self.finish(failed=True)
        if self.on_slow is not None:
            self.on_slow()

    def offer(self, fragment):
        if self.ended or not self.started and not fragment.random_access:
            return
        if (self.queue.qsize() >= MAX_PENDING_FRAGMENTS or
                self.pending_bytes + len(fragment.data) > MAX_PENDING_BYTES):
            self.slow()
            return
        self.started = True
        self.pending_bytes += len(fragment.data)
        self.queue.put_nowait(fragment)
        self.ready.set()

    def finish(self, failed=False):
        if self.ended:
            return
        self.ended, self.failed = True, failed
        if failed:
            while not self.queue.empty():
                self.queue.get_nowait()
            self.pending_bytes = 0
        self.queue.put_nowait(None)
        self.ready.set()

    async def get(self):
        fragment = await self.queue.get()
        if fragment is not None:
            self.pending_bytes -= len(fragment.data)
            if stream_clock() - fragment.received_at > MAX_PENDING_AGE:
                self.slow()
                raise RuntimeError('slow video subscriber')
        return fragment


class VideoHub:
    """A camera has one fixed-quality upstream, independent of viewer identity."""
    def __init__(self, client, path, key, on_closed, on_bytes):
        self.client, self.path, self.key = client, path, key
        self.on_closed, self.on_bytes = on_closed, on_bytes
        self.subscribers = set()
        self.initial, self.session, self.upstream = None, None, None
        self.cache, self.cache_bytes = deque(), 0
        self.accepting = True
        self.task = asyncio.create_task(self._read())

    def subscribe(self, on_slow=None):
        if not self.accepting:
            raise RuntimeError('source closed')
        subscriber = Subscriber(on_slow)
        self.subscribers.add(subscriber)
        if self.cache and stream_clock() - self.cache[0].received_at <= MAX_GOP_AGE:
            for fragment in self.cache:
                subscriber.offer(fragment)
        return subscriber

    def unsubscribe(self, subscriber):
        self.subscribers.discard(subscriber)
        if subscriber is not None:
            subscriber.finish(failed=True)
        if not self.subscribers:
            self.stop()

    def stop(self):
        self.accepting = False
        self.on_closed(self)
        if self.upstream is not None:
            self.upstream.close()
        if not self.task.done():
            self.task.cancel()
        self.cache.clear()
        self.cache_bytes = 0

    def publish(self, fragment):
        if fragment.random_access:
            self.cache.clear()
            self.cache_bytes = 0
        if fragment.random_access or self.cache:
            self.cache.append(fragment)
            self.cache_bytes += len(fragment.data)
            if (len(self.cache) > MAX_GOP_FRAGMENTS or self.cache_bytes > MAX_GOP_BYTES or
                    fragment.received_at - self.cache[0].received_at > MAX_GOP_AGE):
                # Established viewers continue; newcomers wait for the next IDR.
                self.cache.clear()
                self.cache_bytes = 0
        for subscriber in tuple(self.subscribers):
            subscriber.offer(fragment)

    async def _read(self):
        failed = False
        try:
            async with self.client.get('http://native-video.invalid' + self.path, allow_redirects=False,
                                       headers={'Accept': 'video/mp4'}) as upstream:
                self.upstream = upstream
                if (upstream.status != 200 or upstream.content_type != 'video/mp4'
                        or upstream.headers.get('Content-Encoding')):
                    raise ValueError('native video unavailable')
                self.initial = await asyncio.wait_for(mp4_initialization(upstream.content), timeout=10)
                self.on_bytes(self.key, len(self.initial))
                sessions = upstream.headers.getall('X-Video-Session', [])
                self.session = sessions[0] if len(sessions) == 1 and video_session(sessions[0]) else None
                while True:
                    box = await _next_box(upstream.content)
                    if box is None:
                        break
                    kind, moof = box
                    if kind == b'mfra':
                        break  # Finite-file FFmpeg index, never media to a new viewer.
                    if kind != b'moof':
                        raise ValueError('expected native moof')
                    random_access = fragment_random_access(moof)
                    box = await _next_box(upstream.content)
                    if box is None:
                        raise ValueError('missing native mdat')
                    kind, mdat = box
                    if kind != b'mdat' or len(moof) + len(mdat) > MAX_FRAGMENT_BYTES:
                        raise ValueError('expected bounded native mdat')
                    self.on_bytes(self.key, len(moof) + len(mdat))
                    self.publish(Fragment(moof + mdat, random_access, stream_clock()))
        except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                ValueError, UnicodeError, RecursionError, ConnectionError, RuntimeError):
            failed = True
        except asyncio.CancelledError:
            failed = True
        finally:
            self.accepting = False
            if self.upstream is not None:
                self.upstream.close()
            self.on_closed(self)
            self.cache.clear()
            self.cache_bytes = 0
            for subscriber in tuple(self.subscribers):
                subscriber.finish(failed=failed)


@dataclass(eq=False)
class Stream:
    task: asyncio.Task
    transport: object = None
    hub: object = None
    subscriber: object = None
    stopped: bool = False
    unauthorized: bool = False

    def stop(self, unauthorized=False):
        self.unauthorized = self.unauthorized or unauthorized
        if self.stopped:
            return
        self.stopped = True
        if self.subscriber is not None:
            self.subscriber.finish(failed=True)
        # A slow subscriber detected by its own handler already raises/returns;
        # cancelling that same task would interrupt its finally cleanup.
        if self.task is not asyncio.current_task():
            self.task.cancel()


class NativeVideo:
    def __init__(self, path, authorize, session_valid, max_playing=4, split_sockets=False):
        self.path = socket_path(path)
        if isinstance(max_playing, bool) or not isinstance(max_playing, int) or not 1 <= max_playing <= 4:
            raise ValueError('NATIVE_VIDEO_MAX_PLAYING must be 1..4')
        self.max_playing = max_playing
        if not isinstance(split_sockets, bool):
            raise ValueError('NATIVE_VIDEO_SPLIT_SOCKETS must be boolean')
        self.split_sockets = split_sockets
        self.video_paths = ({key: socket_path(str(PurePosixPath(self.path).parent / ('jetson-' + key + '.sock')))
                             for key in KEYS} if self.path and split_sockets else {})
        self.authorize, self.session_valid = authorize, session_valid
        self.client = None
        self.video_clients = {}
        self.streams = {}
        self.hubs = {}
        self.slow_disconnects = 0
        self.upstream_bytes = {key: 0 for key in KEYS}
        self.closing = False

    @staticmethod
    def connector_client(path, limit):
        return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=path, limit=limit),
            timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=10), trust_env=False,
            auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
            skip_auto_headers={'User-Agent', 'Accept-Encoding'})

    async def start(self, _app):
        if self.path:
            self.client = self.connector_client(self.path, MAX_STREAMS + 4)
            self.video_clients = {key: self.connector_client(path, 1) for key, path in self.video_paths.items()}

    def request_allowed(self, request):
        session = self.authorize(request)
        if request.query or '?' in request.raw_path:
            raise web.HTTPBadRequest()
        return session

    async def status_value(self):
        started = monotonic()
        async with self.client.get('http://native-video.invalid/status', allow_redirects=False,
                                   headers={'Accept': 'application/json'}) as response:
            if response.status != 200 or response.headers.get('Content-Encoding'):
                raise ValueError('native preview status unavailable')
            raw = await asyncio.wait_for(bounded_body(response), timeout=10)
            def invalid_constant(_):
                raise ValueError('invalid native status number')
            value = json.loads(raw, parse_constant=invalid_constant)
            return sanitize_status(value, elapsed_ms=(monotonic() - started) * 1000)

    async def status(self, request):
        self.request_allowed(request)
        if not self.path:
            return web.json_response({'enabled': False, 'cameras': {}, 'maxPlaying': self.max_playing})
        try:
            value = await self.status_value()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError, RecursionError):
            value = {'enabled': True, 'cameras': {}, 'error': '原生视频暂不可用'}
        self.authorize(request)  # A logout during the upstream read is final.
        value['maxPlaying'] = self.max_playing
        value['fanout'] = self.fanout_status()
        return web.json_response(value)

    async def detections_value(self):
        started = monotonic()
        async with self.client.get('http://native-video.invalid/detections', allow_redirects=False,
                                   headers={'Accept': 'application/json'}) as response:
            if (response.status != 200 or response.content_type != 'application/json'
                    or response.headers.get('Content-Encoding')):
                raise ValueError('detections unavailable')
            raw = await bounded_body(response, DETECTION_MAX_BYTES)
            return sanitize_detections(decode_detections(raw), elapsed_ms=(monotonic() - started) * 1000)

    async def detections(self, request):
        self.request_allowed(request)
        value = waiting_detections()
        if self.path:
            try:
                value = await asyncio.wait_for(self.detections_value(), timeout=2)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError, RecursionError):
                pass
        self.authorize(request)  # Revocation during the metadata read is final.
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        headers = {'Vary': 'Accept-Encoding'}
        # Only this bounded metadata response is compressed. Camera pixels,
        # MP4 streams and the Unix-socket upstream remain unchanged.
        if accepts_detection_gzip(request.headers):
            body = gzip.compress(body, compresslevel=5, mtime=0)
            headers['Content-Encoding'] = 'gzip'
        return web.Response(body=body, content_type='application/json', headers=headers)

    def fanout_status(self):
        cameras = {key: {'upstreams': int(key in self.hubs),
                         'subscribers': len(self.hubs[key].subscribers) if key in self.hubs else 0,
                         'upstreamBytes': self.upstream_bytes[key]}
                   for key in sorted(KEYS)}
        return {'upstreams': len(self.hubs),
                'subscribers': sum(row['subscribers'] for row in cameras.values()),
                'slowDisconnects': self.slow_disconnects,
                'upstreamBytes': sum(self.upstream_bytes.values()), 'cameras': cameras}

    def _source_bytes(self, key, count):
        self.upstream_bytes[key] += count

    def _hub_closed(self, hub):
        # A cancelled old source must never evict its already-started replacement.
        if self.hubs.get(hub.key) is hub:
            del self.hubs[hub.key]

    def subscribe(self, key, path, on_slow):
        if self.closing:
            raise web.HTTPServiceUnavailable()
        hub = self.hubs.get(key)
        if hub is None or not hub.accepting:
            client = self.video_clients[key] if self.split_sockets else self.client
            hub = VideoHub(client, path, key, self._hub_closed, self._source_bytes)
            self.hubs[key] = hub
        return hub, hub.subscribe(on_slow)

    def reserve(self, nonce, key, transport=None):
        if self.closing:
            raise web.HTTPServiceUnavailable()
        previous = self.streams.get(nonce, {}).get(key)
        if previous is not None and (previous.stopped or previous.task.done() or
                previous.transport is not None and previous.transport.is_closing() or
                previous.hub is not None and not previous.hub.accepting):
            # A disconnected request may overlap its replacement's admission.
            # Identity-checked release cannot remove the replacement's slot.
            previous.stop()
            self.release(nonce, key, previous)
        if (sum(len(group) for group in self.streams.values()) >= MAX_STREAMS
                or len(self.streams.get(nonce, {})) >= min(MAX_SESSION_STREAMS, self.max_playing)
                or key in self.streams.get(nonce, {})
                or (self.split_sockets and sum(key in group for group in self.streams.values()) >= 4)):
            raise web.HTTPTooManyRequests()
        stream = Stream(asyncio.current_task(), transport=transport)
        self.streams.setdefault(nonce, {})[key] = stream
        return stream

    def release(self, nonce, key, stream):
        group = self.streams.get(nonce)
        if group is not None and group.get(key) is stream:
            del group[key]
            if not group:
                del self.streams[nonce]

    def revoke(self, nonce):
        for stream in list(self.streams.get(nonce, {}).values()):
            stream.stop(unauthorized=True)

    async def video(self, request):
        session = self.request_allowed(request)
        key = request.match_info['key']
        if key not in KEYS:
            raise web.HTTPNotFound()
        if not self.path:
            raise web.HTTPServiceUnavailable()
        nonce, cookie = session['n'], request.cookies.get(COOKIE)
        stream = self.reserve(nonce, key, request.transport)
        downstream = None

        async def watch_session():
            while True:
                await asyncio.sleep(.25)
                if not self.session_valid(cookie):
                    self.revoke(nonce)
                    return
                if request.transport is None or request.transport.is_closing():
                    stream.stop()
                    return

        def slow_subscriber():
            self.slow_disconnects += 1
            stream.stop()
            # aiohttp finalizes a returned prepared response with write_eof.
            # Abort only this slow connection so that finalization cannot wait
            # forever or keep sending a backlog after its slot was released.
            if downstream is not None and downstream.prepared and request.transport is not None:
                request.transport.abort()

        watcher = asyncio.create_task(watch_session())
        try:
            status = await self.status_value()
            usage = status.get('cameraUsage', {}).get(key, {})
            # Occupancy describes capture ownership, never viewing permission.
            if usage.get('connected') is False:
                raise web.HTTPServiceUnavailable()
            item = status['cameras'].get(key)
            if not item or not item['codec'] or item['error'] or item['video_error']:
                raise web.HTTPServiceUnavailable()
            path = '/camera/' + item['id'] + '/video.mp4'
            hub, subscriber = self.subscribe(key, path, slow_subscriber)
            stream.hub, stream.subscriber = hub, subscriber
            await asyncio.wait_for(subscriber.ready.wait(), timeout=10)
            self.authorize(request)  # Revocation wins over a simultaneous source failure.
            if subscriber.failed or not subscriber.started or hub.initial is None:
                raise web.HTTPServiceUnavailable()
            headers = {'Content-Type': 'video/mp4', 'Cache-Control': 'no-store',
                       'X-Content-Type-Options': 'nosniff', 'X-Accel-Buffering': 'no'}
            if hub.session is not None:
                headers['X-Video-Session'] = hub.session
            downstream = web.StreamResponse(headers=headers)
            await asyncio.wait_for(downstream.prepare(request), timeout=WRITE_TIMEOUT)

            async def write(data):
                async def chunks():
                    for offset in range(0, len(data), CHUNK_BYTES):
                        await downstream.write(data[offset:offset + CHUNK_BYTES])
                try:
                    await asyncio.wait_for(chunks(), timeout=WRITE_TIMEOUT)
                except asyncio.TimeoutError:
                    subscriber.slow()
                    raise

            await write(hub.initial)
            while True:
                fragment = await subscriber.get()
                if fragment is None:
                    break
                await write(fragment.data)
            await asyncio.wait_for(downstream.write_eof(), timeout=WRITE_TIMEOUT)
            return downstream
        except asyncio.CancelledError:
            if not stream.stopped:
                raise
            if downstream is None:
                if stream.unauthorized:
                    self.authorize(request)  # Preserve pending-account 403 versus expired-session 401.
                    raise web.HTTPUnauthorized()
                raise web.HTTPServiceUnavailable()
            return downstream
        except web.HTTPException:
            raise
        except asyncio.TimeoutError:
            if downstream is not None and downstream.prepared:
                stream.subscriber.slow()
                return downstream
            raise web.HTTPServiceUnavailable()
        except (aiohttp.ClientError, asyncio.IncompleteReadError,
                ValueError, UnicodeError, RecursionError, ConnectionError, RuntimeError):
            if downstream is None:
                raise web.HTTPServiceUnavailable()
            return downstream
        finally:
            # Release ownership before any await. Transport.abort() can cause a
            # second cancellation while cleanup is awaiting the watchdog.
            watcher.cancel()
            tasks = [watcher]
            if stream.hub is not None:
                stream.hub.unsubscribe(stream.subscriber)
                if not stream.hub.subscribers:
                    tasks.append(stream.hub.task)
            self.release(nonce, key, stream)
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self, _app):
        self.closing = True
        tasks = []
        for group in list(self.streams.values()):
            for stream in list(group.values()):
                tasks.append(stream.task)
                stream.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        hubs = list(self.hubs.values())
        for hub in hubs:
            hub.stop()
        if hubs:
            await asyncio.gather(*(hub.task for hub in hubs), return_exceptions=True)
        clients = ([self.client] if self.client is not None else []) + list(self.video_clients.values())
        if clients:
            await asyncio.gather(*(client.close() for client in clients))
