#!/usr/bin/env python3
"""Independent, loopback-only camera status and existing-video receiver.

This service never opens a camera or changes its owner. The background probe
only uses camera_owner.SystemOps.snapshot(); HTTP handlers cannot invoke its
switching engine. NONE means no holder was observed by the unprivileged probe,
not a guarantee that every other Linux user's descriptors could be inspected.
"""
from __future__ import annotations

import argparse
import copy
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
from socketserver import BaseRequestHandler
import threading
import time
from http.server import ThreadingHTTPServer

try:
    from . import camera_owner
except ImportError:  # Installed next to the independent helper on Jetson.
    import camera_owner

try:
    from shared.detections import (DETECTION_FILE_TTL_MS, DETECTION_MAX_BYTES, decode_detections,
                                   sanitize_detections, video_session, waiting_detections)
except ImportError:  # The audited shared validator is installed beside this helper.
    from detections import (DETECTION_FILE_TTL_MS, DETECTION_MAX_BYTES, decode_detections,
                            sanitize_detections, video_session, waiting_detections)

HOST = '127.0.0.1'
PORT = 8081
UPSTREAM_PORT = 8080
CHECK_INTERVAL = 2.0
LEASE_MS = 6000
MAX_BODY = 1_048_576
MAX_HEADERS = 16_384
MAX_REQUEST = 8192
MAX_WORKERS = 20
MAX_STREAMS = 16
CHUNK_BYTES = 65_536
HEADER_TIMEOUT = 3.0
STATUS_TIMEOUT = 2.0
READ_TIMEOUT = 10.0
KEY_PORTS = camera_owner.KEY_PORTS
IDS = {identity: direction for identity, (_, direction, _) in camera_owner.CAMERA_IDENTITIES.items()}
PREVIEW_PROFILES = ('standard', 'balanced', 'compact', 'economy')
VIDEO_PATHS = frozenset('/camera/' + identity + '/' + name
    for identity in IDS
    for name in ('video.mp4', *(f'video-{profile}.mp4' for profile in PREVIEW_PROFILES)))
PATHS = VIDEO_PATHS | {'/status', '/detections'}
NATIVE_UNITS = frozenset((camera_owner.NEW_WEB_UNIT, 'yolo-native-direct-preview.service',
                          camera_owner.SHARED_UNIT))
TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
REASONS = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed',
           408: 'Request Timeout', 431: 'Request Header Fields Too Large',
           502: 'Bad Gateway', 503: 'Service Unavailable'}


class GatewayError(Exception):
    def __init__(self, status=502):
        self.status = status


class DetectionFile:
    """Read one private, same-user sidecar file; never accept a request path."""
    def __init__(self, path=None, clock=time.monotonic):
        self.path = Path(path) if path is not None else Path.home() / '.local/state/robot-cloud/yolo-detections.json'
        self.clock = clock

    def value(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, 'rb') as stream:
                details = os.fstat(stream.fileno())
                if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
                        or details.st_mode & 0o077 or details.st_size > DETECTION_MAX_BYTES):
                    return waiting_detections()
                raw = stream.read(DETECTION_MAX_BYTES + 1)
            value = decode_detections(raw)
            generated = value.get('generatedMonotonic') if isinstance(value, dict) else None
            now = self.clock()
            if (_finite(generated, 2 ** 53 - 1) is None or generated > now
                    or (now - generated) * 1000 >= DETECTION_FILE_TTL_MS):
                return waiting_detections()
            return sanitize_detections(value, elapsed_ms=(now - generated) * 1000)
        except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
            return waiting_detections()


def _shutdown(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _finite(value, maximum):
    try:
        return (value if type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= maximum else None)
    except OverflowError:
        return None


def _node_mapping(settings):
    """Resolve sysfs/by-path links only; never stat/open a video device itself."""
    connected, parents, nodes = {}, {}, {}
    for key, port in camera_owner.camera_ports(settings).items():
        try:
            parents[key] = camera_owner.camera_usb_parent(settings, key, port)
            connected[key] = True
        except FileNotFoundError:
            connected[key] = False
        except OSError:
            connected[key] = None
    for entry in Path(settings.sys_video_root).glob('video*'):
        try:
            physical = (entry / 'device').resolve(strict=True).parent
            matched = [key for key, parent in parents.items() if parent == physical]
            if len(matched) == 1:
                nodes['/dev/' + entry.name] = matched[0]
        except OSError:
            continue
    return nodes, connected


def _usage(snapshot, uid, mapping, connected, ports=None):
    """Redact all process/device/unit details before handing metadata to HTTP."""
    result = {key: {'owner': 'UNKNOWN' if connected[key] is None else 'NONE',
                    'connected': connected[key]} for key in KEY_PORTS}
    if snapshot.scan_errors:
        return {key: {'owner': 'UNKNOWN', 'connected': None} for key in KEY_PORTS}
    for key, port in (ports or KEY_PORTS).items():
        if (port in snapshot.missing) != (connected[key] is False):
            result[key] = {'owner': 'UNKNOWN', 'connected': None}
        elif connected[key] and key not in mapping.values():
            result[key]['owner'] = 'UNKNOWN'
    observed, ambiguous = {key: set() for key in KEY_PORTS}, set()
    for holder in snapshot.holders:
        keys = {mapping[node] for node in holder.nodes if node in mapping}
        if holder.unreadable or not keys or any(node not in mapping for node in holder.nodes):
            # fuser's corroborated PID may list all inspected nodes: there is
            # no evidence of WHICH camera it holds, so do not invent four BUSYs.
            ambiguous.update(KEY_PORTS)
            continue
        unit = snapshot.units.get(holder.unit)
        native = (holder.uid == uid and holder.unit in NATIVE_UNITS and unit is not None
                  and unit.active and unit.verified)
        for key in keys:
            observed[key].add('WEB' if native else 'OTHER')
    for key in KEY_PORTS:
        if result[key]['connected'] is not True or result[key]['owner'] == 'UNKNOWN':
            continue
        if 'OTHER' in observed[key]:
            result[key]['owner'] = 'OTHER'
        elif key in ambiguous:
            result[key]['owner'] = 'UNKNOWN'
        elif 'WEB' in observed[key]:
            result[key]['owner'] = 'WEB'
    return result


class UsageCache:
    """One background probe; GET never starts another scan or mutating action."""
    def __init__(self, ops=None, *, mapper=_node_mapping, clock=time.monotonic):
        self.ops = ops or camera_owner.SystemOps(camera_owner.Settings(unit_timeout=1.0))
        self.mapper, self.clock = mapper, clock
        self._lock, self._probing, self._stop = threading.Lock(), threading.Lock(), threading.Event()
        self._checked, self._rows, self._thread = None, None, None

    def probe_once(self):
        if not self._probing.acquire(blocking=False):
            return False
        started = self.clock()
        try:
            try:
                before_ports = camera_owner.camera_ports(self.ops.settings)
                before_map, before_connected = self.mapper(self.ops.settings)
                snapshot = self.ops.snapshot()
                after_map, after_connected = self.mapper(self.ops.settings)
                after_ports = camera_owner.camera_ports(self.ops.settings)
                if before_map != after_map or before_connected != after_connected or before_ports != after_ports:
                    rows = {key: {'owner': 'UNKNOWN', 'connected': None} for key in KEY_PORTS}
                else:
                    rows = _usage(snapshot, self.ops.uid, after_map, after_connected, after_ports)
            except Exception:
                # Never reveal /proc paths, process arguments, diagnostics or secrets.
                rows = {key: {'owner': 'UNKNOWN', 'connected': None} for key in KEY_PORTS}
            with self._lock:
                self._rows, self._checked = rows, started
        finally:
            self._probing.release()
        return True

    def value(self):
        with self._lock:
            rows, checked = copy.deepcopy(self._rows), self._checked
        age = max(0, (self.clock() - checked) * 1000) if checked is not None else LEASE_MS + 1
        age = _finite(age, 86_400_000)
        age = LEASE_MS + 1 if age is None else age
        if rows is None or age >= LEASE_MS:
            rows = {key: {'owner': 'UNKNOWN', 'connected': None} for key in KEY_PORTS}
        return {key: dict(rows[key], checkedAgeMs=age) for key in KEY_PORTS}

    def start(self):
        if self._thread is not None:
            return
        def run():
            while not self._stop.is_set():
                self.probe_once()
                self._stop.wait(CHECK_INTERVAL)
        self._thread = threading.Thread(target=run, name='camera-usage-readonly', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=.3)


class _HeaderBudget:
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_HEADERS

    def readline(self, limit=-1):
        if self.remaining is None:
            return self.stream.readline(limit)
        line = self.stream.readline(self.remaining + 1 if limit < 0 else min(limit, self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0:
            raise GatewayError()
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        self._io_lock = threading.RLock()
        super().__init__(*args, **kwargs)
        self.fp = _HeaderBudget(self.fp)

    def begin(self):
        super().begin()
        if self.fp is not None:
            self.fp.remaining = None

    def read(self, amt=None):
        with self._io_lock:
            return super().read(amt)

    def read1(self, amt=-1):
        with self._io_lock:
            return super().read1(amt)

    def close(self):
        # Python 3.10 HTTPResponse is not concurrent read/close safe. Source
        # shutdown first interrupts raw sockets, then this lock waits only for
        # the unblocked reader before discarding its buffer and descriptor.
        with self._io_lock:
            super().close()


def _headers(response, content_type, maximum=None):
    headers = {}
    for name, value in response.getheaders():
        name = name.lower()
        if name in headers:
            raise GatewayError()
        headers[name] = value.strip()
    if (response.status != 200 or headers.get('content-type', '').split(';', 1)[0].strip().lower() != content_type
            or headers.get('content-encoding', 'identity').lower() != 'identity'
            or headers.get('transfer-encoding', 'chunked').lower() != 'chunked'
            or ('transfer-encoding' in headers and 'content-length' in headers)):
        raise GatewayError()
    if 'content-length' in headers:
        length = headers['content-length']
        if not length.isascii() or not length.isdigit() or len(length) > 16:
            raise GatewayError()
        if maximum is not None and int(length) > maximum:
            raise GatewayError()
    return headers


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GatewayError()
        result[key] = value
    return result


def _cameras(raw):
    def invalid_constant(_):
        raise GatewayError()
    def bounded_int(value):
        if len(value) > 20:
            raise GatewayError()
        return int(value)
    def bounded_float(value):
        if len(value) > 64:
            raise GatewayError()
        return float(value)
    obj = json.loads(raw, object_pairs_hook=_unique_json, parse_constant=invalid_constant,
                     parse_int=bounded_int, parse_float=bounded_float)
    if not isinstance(obj, dict) or not isinstance(obj.get('cameras'), list) or len(obj['cameras']) > 32:
        raise GatewayError()
    result, seen = [], set()
    for row in obj['cameras']:
        if not isinstance(row, dict):
            continue
        identity = row.get('id')
        if not isinstance(identity, str) or identity not in IDS or row.get('direction') != IDS[identity]:
            continue
        if identity in seen:
            raise GatewayError()
        seen.add(identity)
        clean = {'id': identity, 'direction': IDS[identity]}
        codec = row.get('codec')
        clean['codec'] = (codec if isinstance(codec, str) and re.fullmatch(
            r'(?:h264|avc|avc1(?:\.[0-9a-fA-F]{6})?)', codec.lower()) else None)
        for field, maximum in (('width', 8192), ('height', 8192), ('frame_age_ms', 86_400_000),
                               ('requested_fps', 240), ('actual_fps', 240), ('encoded_fps', 240),
                               ('h264_mbps', 1000), ('frames', 2 ** 53 - 1), ('encoded_frames', 2 ** 53 - 1)):
            value = _finite(row.get(field), maximum)
            clean[field] = value if field not in ('frames', 'encoded_frames') or type(value) is int else None
        clean['error'] = '相机暂不可用' if row.get('error') else None
        clean['video_error'] = '视频暂不可用' if row.get('video_error') else None
        result.append(clean)
    return result


def _read_exact(response, size):
    raw = response.read(size)
    if len(raw) != size:
        raise GatewayError()
    return raw


def _mp4_initialization(response):
    initial, found_ftyp = bytearray(), False
    while len(initial) < MAX_BODY:
        header = _read_exact(response, 8)
        size, kind = int.from_bytes(header[:4], 'big'), header[4:]
        if size == 1:
            extra = _read_exact(response, 8)
            header += extra
            size = int.from_bytes(extra, 'big')
        if (size < len(header) or size > MAX_BODY - len(initial)
                or kind not in (b'ftyp', b'moov', b'free', b'skip', b'wide')
                or (not found_ftyp and kind != b'ftyp')):
            raise GatewayError()
        initial.extend(header)
        initial.extend(_read_exact(response, size - len(header)))
        if kind == b'ftyp':
            if found_ftyp or size < len(header) + 8:
                raise GatewayError()
            found_ftyp = True
        elif kind == b'moov':
            return bytes(initial)
    raise GatewayError()


class PreviewSource:
    """Only fixed-origin GET; no forwarding request headers or following redirects."""
    def __init__(self, port=UPSTREAM_PORT):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid test upstream port')
        self.port, self._lock, self._connections = port, threading.Lock(), {}
        self._responses = {}
        self._closed = False

    def _remember(self, connection, sock=None):
        with self._lock:
            if self._closed:
                _shutdown(sock)
                raise GatewayError(503)
            self._connections[connection] = sock

    def _remember_response(self, connection, response):
        with self._lock:
            if self._closed:
                raise GatewayError(503)
            self._responses[connection] = response

    def _open(self, path, mime, maximum=None):
        if path not in PATHS:
            raise GatewayError(404)
        connection = http.client.HTTPConnection(HOST, self.port, timeout=STATUS_TIMEOUT)
        connection.response_class = _BoundedResponse
        self._remember(connection)
        response, timer = None, None
        try:
            expired = threading.Event()
            def expire():
                expired.set()
                with self._lock:
                    sock = self._connections.get(connection)
                _shutdown(sock)
            timer = threading.Timer(STATUS_TIMEOUT, expire)
            timer.daemon = True
            timer.start()
            connection.connect()
            sock = connection.sock
            self._remember(connection, sock)
            if expired.is_set():
                raise GatewayError()
            connection.request('GET', path, headers={'Connection': 'close', 'Accept': mime,
                                                      'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            self._remember_response(connection, response)
            _headers(response, mime, maximum)
            if expired.is_set():
                raise GatewayError()
            return connection, response, sock
        except BaseException:
            if response is not None:
                response.close()
            self._release(connection)
            raise
        finally:
            if timer is not None:
                timer.cancel()

    def _release(self, connection):
        with self._lock:
            self._connections.pop(connection, None)
            response = self._responses.pop(connection, None)
        if response is not None:
            response.close()
        connection.close()

    def status(self):
        started = time.monotonic()
        connection, response, timer = None, None, None
        try:
            # The timer includes connect/headers/body, even a drip-fed response.
            # _open registers the connection before its first network operation.
            connection = http.client.HTTPConnection(HOST, self.port, timeout=STATUS_TIMEOUT)
            connection.response_class = _BoundedResponse
            self._remember(connection)
            expired = threading.Event()
            def expire():
                expired.set()
                with self._lock:
                    sock = self._connections.get(connection)
                _shutdown(sock)
            timer = threading.Timer(STATUS_TIMEOUT, expire)
            timer.daemon = True
            timer.start()
            connection.connect()
            self._remember(connection, connection.sock)
            if expired.is_set() or time.monotonic() - started >= STATUS_TIMEOUT:
                raise GatewayError()
            connection.request('GET', '/status', headers={'Connection': 'close', 'Accept': 'application/json',
                                                          'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            self._remember_response(connection, response)
            headers = _headers(response, 'application/json', MAX_BODY)
            raw = response.read(MAX_BODY + 1)
            if (len(raw) > MAX_BODY or expired.is_set() or time.monotonic() - started >= STATUS_TIMEOUT
                    or ('content-length' in headers and int(headers['content-length']) != len(raw))):
                raise GatewayError()
            return _cameras(raw)
        except (GatewayError, OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            return []  # Occupation remains available even when source preview is off.
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                self._release(connection)

    def stream(self, path, downstream):
        if path not in VIDEO_PATHS:
            raise GatewayError(404)
        connection, response, timer = None, None, None
        try:
            connection, response, sock = self._open(path, 'video/mp4')
            sock.settimeout(READ_TIMEOUT)
            expired = threading.Event()
            def expire():
                expired.set()
                _shutdown(sock)
            timer = threading.Timer(READ_TIMEOUT, expire)
            timer.daemon = True
            timer.start()
            initial = _mp4_initialization(response)
            if expired.is_set():
                raise GatewayError()
            timer.cancel()
            # A connection is tied to this encoder session, never an arbitrary
            # upstream header or a browser-provided source identity.
            session = video_session(response.getheader('X-Video-Session'))
            _send(downstream, 200, content_type='video/mp4', stream=True, session=session)
            downstream.sendall(initial)
            while True:
                chunk = response.read1(CHUNK_BYTES)
                if not chunk:
                    return
                downstream.sendall(chunk)
        except GatewayError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            if response is None:
                raise GatewayError() from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                self._release(connection)

    def close(self):
        with self._lock:
            self._closed = True
            connections = list(self._connections.items())
            responses = list(self._responses.values())
            self._connections.clear()
            self._responses.clear()
        # Shutdown raw sockets FIRST: HTTPResponse.close() can wait for the
        # buffered-reader lock held by a worker doing a blocking network read.
        for connection, sock in connections:
            _shutdown(sock)
        # A detached response can already contain buffered frames. Closing
        # only its socket would leave that buffer and file descriptor alive.
        for response in responses:
            response.close()
        for connection, sock in connections:
            connection.close()


def _send(connection, status, body=b'', content_type='text/plain; charset=utf-8', *, stream=False, session=None):
    header = ('HTTP/1.1 ' + str(status) + ' ' + REASONS[status]
              + '\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff'
              + '\r\nContent-Type: ' + content_type
              + ('\r\nX-Accel-Buffering: no' if stream else '\r\nContent-Length: ' + str(len(body)))
              + ('\r\nX-Video-Session: ' + session if stream and video_session(session) else '')
              + '\r\n\r\n').encode('ascii')
    connection.sendall(header + body)


def _request_path(connection):
    deadline, data = time.monotonic() + HEADER_TIMEOUT, bytearray()
    while b'\r\n\r\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError(408)
        connection.settimeout(remaining)
        chunk = connection.recv(min(1024, MAX_REQUEST + 1 - len(data)))
        if not chunk:
            return None
        data.extend(chunk)
        if len(data) > MAX_REQUEST:
            raise GatewayError(431)
    header, body = bytes(data).split(b'\r\n\r\n', 1)
    lines, headers = header.split(b'\r\n'), {}
    request = lines[0].split(b' ')
    if len(request) != 3 or request[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
        raise GatewayError(400)
    if request[0] != b'GET':
        raise GatewayError(405)
    for line in lines[1:]:
        if b':' not in line:
            raise GatewayError(400)
        name, value = line.split(b':', 1)
        if (not TOKEN.fullmatch(name) or name.lower() in headers
                or any(byte < 32 or byte > 126 for byte in value)):
            raise GatewayError(400)
        headers[name.lower()] = value.strip()
    if body or b'transfer-encoding' in headers or headers.get(b'content-length', b'0') != b'0':
        raise GatewayError(400)
    if b'?' in request[1] or b'#' in request[1]:
        raise GatewayError(400)
    try:
        path = request[1].decode('ascii')
    except UnicodeError:
        raise GatewayError(400) from None
    if path not in PATHS:
        raise GatewayError(404)
    return path


class _Handler(BaseRequestHandler):
    def handle(self):
        reserved = False
        try:
            path = _request_path(self.request)
            if path is None:
                return
            self.request.settimeout(READ_TIMEOUT)
            if path == '/status':
                value = {'cameras': self.server.source.status(), 'cameraUsage': self.server.usage.value()}
                body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
                _send(self.request, 200, body, 'application/json; charset=utf-8')
                return
            if path == '/detections':
                body = json.dumps(self.server.detections.value(), ensure_ascii=False, allow_nan=False,
                                  separators=(',', ':')).encode('utf-8')
                _send(self.request, 200, body, 'application/json; charset=utf-8')
                return
            reserved = self.server.stream_slots.acquire(blocking=False)
            if not reserved:
                raise GatewayError(503)
            self.server.source.stream(path, self.request)
        except GatewayError as exc:
            try:
                self.request.settimeout(.1)
                _send(self.request, exc.status)
            except OSError:
                pass
        except (OSError, http.client.HTTPException, ValueError):
            pass  # Do not append an HTML or JSON error to an admitted MP4 stream.
        finally:
            if reserved:
                self.server.stream_slots.release()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = MAX_WORKERS

    def __init__(self, address=(HOST, PORT), *, source=None, usage=None, detections=None):
        if address[0] != HOST:
            raise ValueError('Gateway must bind fixed loopback')
        self.source, self.usage = source or PreviewSource(), usage or UsageCache()
        self.detections = detections or DetectionFile()
        self.slots, self.stream_slots = threading.BoundedSemaphore(MAX_WORKERS), threading.BoundedSemaphore(MAX_STREAMS)
        self._request_lock, self._requests = threading.Lock(), set()
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(.05)
                _send(request, 503)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self._request_lock:
            self._requests.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._request_lock:
                self._requests.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._request_lock:
                self._requests.discard(request)
            self.slots.release()

    def handle_error(self, request, client_address):
        pass

    def server_close(self):
        self.usage.close()
        self.source.close()
        with self._request_lock:
            requests = list(self._requests)
        for request in requests:
            _shutdown(request)
            request.close()
        super().server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Loopback-only read-only camera occupation and video gateway')
    parser.add_argument('--capture-config', default=camera_owner.Settings().capture_config,
                        help='Audited four-camera capture JSON')
    args = parser.parse_args(argv)  # No endpoint/command/owner-changing options exist.
    ops = camera_owner.SystemOps(camera_owner.Settings(unit_timeout=1.0, capture_config=args.capture_config))
    with GatewayServer(usage=UsageCache(ops)) as server:
        server.usage.start()
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
