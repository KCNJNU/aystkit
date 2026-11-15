"""Standalone, loopback-only, GET-only single-frame relay with bounded JPEG previews.

No camera/ROS access, cache, remote URL, credentials, or proxy support. The CLI
always listens on 127.0.0.1:18081 and reads the existing 127.0.0.1:8080 preview.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import math
import re
import socket
from socketserver import BaseRequestHandler
import threading
import time

HOST = '127.0.0.1'
PORT = 18081
UPSTREAM_PORT = 8080
MAX_WORKERS = 5  # Four independently enabled cameras plus one status/diagnostic request.
MAX_REQUEST_HEADERS = 4096
MAX_RESPONSE_HEADERS = 8192
MAX_PART_HEADERS = 4096
MAX_STATUS_BYTES = 32768
MAX_JPEG_BYTES = 500000
MAX_INPUT_SIDE = 4096
MAX_INPUT_PIXELS = 8_000_000
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 480
PREVIEW_SIZES = {'640x480': (PREVIEW_WIDTH, PREVIEW_HEIGHT), '320x240': (320, 240)}
DEFAULT_PREVIEW_SIZE = '640x480'
PREVIEW_QUALITY = 60
UPSTREAM_TIMEOUT = 1.0
CLIENT_TIMEOUT = 2.5
KEEP_ALIVE_IDLE_SECONDS = 2.0
KEEP_ALIVE_LIFETIME_SECONDS = 120.0
KEEP_ALIVE_MAX_REQUESTS = 512
PATHS = frozenset(('/status', '/stream/camera1', '/stream/camera2', '/stream/camera3', '/stream/camera4'))
TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
REASONS = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed',
           422: 'Unprocessable Content',
           408: 'Request Timeout', 431: 'Request Header Fields Too Large',
           502: 'Bad Gateway', 503: 'Service Unavailable', 504: 'Gateway Timeout'}


class RelayError(Exception):
    def __init__(self, status):
        self.status = status


class PreviewDataError(RelayError):
    def __init__(self):
        super().__init__(422)


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _HeaderBudget:
    """Cap http.client's outer response headers before it parses their contents."""
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_RESPONSE_HEADERS

    def readline(self, limit=-1):
        if self.remaining is None:
            return self.stream.readline(limit)
        bound = self.remaining + 1 if limit < 0 else min(limit, self.remaining + 1)
        line = self.stream.readline(bound)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise PreviewDataError()
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = _HeaderBudget(self.fp)

    def begin(self):
        super().begin()
        if self.fp is not None:
            self.fp.remaining = None


def _unique_headers(pairs):
    headers = {}
    for name, value in pairs:
        name = name.lower()
        if name in headers:
            raise PreviewDataError()
        headers[name] = value.strip()
    return headers


def _length(value, maximum, minimum=0):
    if not value or not value.isascii() or not value.isdigit() or len(value) > 7:
        raise PreviewDataError()
    number = int(value)
    if not minimum <= number <= maximum:
        raise PreviewDataError()
    return number


def _part_line(response, budget):
    line = bytearray()
    while len(line) <= budget:
        value = response.read(1)
        if not value:
            raise PreviewDataError()
        line.extend(value)
        if value == b'\n':
            if not line.endswith(b'\r\n') or len(line) > budget:
                raise PreviewDataError()
            return bytes(line)
    raise PreviewDataError()


def _jpeg_dimensions(raw):
    """Validate SOF bounds before allowing a compressed image to allocate pixels."""
    if not isinstance(raw, bytes) or not 16 <= len(raw) <= MAX_JPEG_BYTES or not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise PreviewDataError()
    offset, dimensions = 2, None
    while offset < len(raw) - 2:
        if raw[offset] != 0xff:
            raise PreviewDataError()
        while offset < len(raw) and raw[offset] == 0xff:
            offset += 1
        if offset >= len(raw):
            raise PreviewDataError()
        marker = raw[offset]
        offset += 1
        if marker == 0xd9:
            break
        if marker in (0xd8, 0x00) or 0xd0 <= marker <= 0xd7:
            raise PreviewDataError()
        if offset + 2 > len(raw):
            raise PreviewDataError()
        length = int.from_bytes(raw[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(raw):
            raise PreviewDataError()
        if marker in (0xc0, 0xc1, 0xc2):
            if dimensions is not None or length < 11:
                raise PreviewDataError()
            precision, components = raw[offset + 2], raw[offset + 7]
            height = int.from_bytes(raw[offset + 3:offset + 5], 'big')
            width = int.from_bytes(raw[offset + 5:offset + 7], 'big')
            if (precision != 8 or components not in (1, 3) or length != 8 + 3 * components
                    or not 0 < width <= MAX_INPUT_SIDE or not 0 < height <= MAX_INPUT_SIDE
                    or width * height > MAX_INPUT_PIXELS):
                raise PreviewDataError()
            dimensions = (width, height)
        if marker == 0xda:
            if dimensions is None or offset + length >= len(raw) - 2:
                raise PreviewDataError()
            return dimensions
        offset += length
    raise PreviewDataError()


def _codec():
    # Lazy import keeps status/transport code independently testable. The service
    # sets BLAS/OMP thread limits before Python starts; OpenCV uses one thread.
    import cv2
    import numpy as np
    return cv2, np


def _preview_bounds(preview_size):
    if not isinstance(preview_size, str) or preview_size not in PREVIEW_SIZES:
        raise ValueError('Preview size must be 640x480 or 320x240')
    return PREVIEW_SIZES[preview_size]


def _preview_jpeg(raw, preview_size=DEFAULT_PREVIEW_SIZE):
    limit_width, limit_height = _preview_bounds(preview_size)
    width, height = _jpeg_dimensions(raw)
    try:
        cv2, np = _codec()
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if image is None or image.shape != (height, width, 3):
            raise PreviewDataError()
        scale = min(1.0, limit_width / width, limit_height / height)
        if scale < 1:
            image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])
        if not ok:
            raise PreviewDataError()
        result = encoded.tobytes()
        preview_width, preview_height = _jpeg_dimensions(result)
        if preview_width > limit_width or preview_height > limit_height:
            raise PreviewDataError()
        return result
    except RelayError:
        raise
    except Exception:
        # Codec diagnostics may contain binary/metadata; expose only a fixed status.
        raise PreviewDataError() from None


def _multipart(raw):
    body = (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
            + str(len(raw)).encode('ascii') + b'\r\n\r\n' + raw + b'\r\n--frame--\r\n')
    return 'multipart/x-mixed-replace; boundary=frame', body


def _single_frame(response, content_type):
    parameters = [part.strip() for part in content_type.split(';')]
    parameters[0] = parameters[0].lower()
    if parameters not in (['multipart/x-mixed-replace', 'boundary=frame'],
                          ['multipart/x-mixed-replace', 'boundary="frame"']):
        raise PreviewDataError()
    if _part_line(response, 64) != b'--frame\r\n':
        raise PreviewDataError()
    budget, pairs = MAX_PART_HEADERS, []
    while True:
        line = _part_line(response, budget)
        budget -= len(line)
        if line == b'\r\n':
            break
        try:
            name, value = line[:-2].split(b':', 1)
            if not TOKEN.fullmatch(name) or any(byte < 32 or byte > 126 for byte in value):
                raise ValueError()
            pairs.append((name.decode('ascii'), value.decode('ascii')))
        except (ValueError, UnicodeError) as exc:
            raise PreviewDataError() from exc
    headers = _unique_headers(pairs)
    if headers.get('content-type', '').lower() != 'image/jpeg':
        raise PreviewDataError()
    length = _length(headers.get('content-length'), MAX_JPEG_BYTES, 16)
    raw = response.read(length)
    if len(raw) != length or not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise PreviewDataError()
    return raw


class _PreviewSource:
    def __init__(self, upstream_port=UPSTREAM_PORT, *, preview_size=DEFAULT_PREVIEW_SIZE):
        if type(upstream_port) is not int or not 1 <= upstream_port <= 65535:
            raise ValueError('Invalid internal test port')
        self.port = upstream_port
        _preview_bounds(preview_size)
        self.preview_size = preview_size

    def fetch(self, path):
        if path == '/status':
            content_type, raw = self._fetch_closed(path)
            return content_type, raw, {}
        if path not in PATHS:
            raise RelayError(404)
        started = time.monotonic()
        # The audited source stream returns its latest cached JPEG. A status
        # read immediately before it conservatively bounds the frame's age.
        _, status_raw = self._fetch_closed('/status')
        item = json.loads(status_raw).get(path.rsplit('/', 1)[1])
        if not isinstance(item, dict) or item.get('error') not in ('', None):
            raise PreviewDataError()
        age = item.get('age_s')
        try:
            finite_age = type(age) in (int, float) and math.isfinite(age) and age >= 0
        except OverflowError:
            finite_age = False
        if not finite_age:
            raise PreviewDataError()
        content_type, raw = self._fetch_closed(path)
        # _fetch_closed has closed the upstream response/socket and cancelled
        # its timer before any image decode/resize work starts.
        digest = hashlib.sha256(raw).hexdigest()
        content_type, body = _multipart(_preview_jpeg(raw, self.preview_size))
        age += max(0, time.monotonic() - started)
        if not math.isfinite(age):
            raise PreviewDataError()
        return content_type, body, {'X-Preview-Contract': '1', 'X-Source-SHA256': digest,
                                    'X-Source-Age-S': format(age, '.12g')}

    def _fetch_closed(self, path):
        if path not in PATHS:
            raise RelayError(404)
        connection = http.client.HTTPConnection(HOST, self.port, timeout=UPSTREAM_TIMEOUT)
        connection.response_class = _BoundedResponse
        deadline, expired, timer = time.monotonic() + UPSTREAM_TIMEOUT, threading.Event(), None
        try:
            connection.connect()
            sock = connection.sock
            def expire():
                expired.set()
                _shutdown(sock)
            # Absolute deadline also bounds a slow byte-at-a-time upstream.
            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            connection.request('GET', path, headers={'Connection': 'close', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            try:
                if response.status != 200:
                    if response.status in (502, 503, 504):
                        raise RelayError(502)
                    raise PreviewDataError()  # Redirects are never followed.
                headers = _unique_headers(response.getheaders())
                if headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise PreviewDataError()
                if 'transfer-encoding' in headers and headers['transfer-encoding'].lower() != 'chunked':
                    raise PreviewDataError()
                if 'transfer-encoding' in headers and 'content-length' in headers:
                    raise PreviewDataError()
                content_type = headers.get('content-type', '')
                if path != '/status':
                    return 'image/jpeg', _single_frame(response, content_type)
                if content_type.split(';', 1)[0].lower() != 'application/json':
                    raise PreviewDataError()
                declared_length = (_length(headers['content-length'], MAX_STATUS_BYTES)
                                   if 'content-length' in headers else None)
                raw = response.read(MAX_STATUS_BYTES + 1)
                if len(raw) > MAX_STATUS_BYTES or (declared_length is not None and len(raw) != declared_length):
                    raise PreviewDataError()
                def reject_constant(value):
                    raise ValueError('Nonfinite JSON')
                try:
                    parsed = json.loads(raw, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise PreviewDataError() from None
                if not isinstance(parsed, dict):
                    raise PreviewDataError()
                return 'application/json', raw
            finally:
                # Never leave the multipart source open or drain subsequent frames.
                response.close()
        except RelayError:
            if expired.is_set():
                raise RelayError(504) from None
            raise
        except (TimeoutError, socket.timeout):
            raise RelayError(504) from None
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            raise RelayError(504 if expired.is_set() else 502) from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=.1)
            connection.close()


def _send(connection, status, body=b'', content_type='text/plain; charset=utf-8', metadata=None,
          *, keep_alive=False):
    extra = ''.join('\r\n' + name + ': ' + value for name, value in (metadata or {}).items())
    header = ('HTTP/1.1 ' + str(status) + ' ' + REASONS[status]
              + '\r\nConnection: ' + ('keep-alive' if keep_alive else 'close')
              + '\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff'
              + '\r\nContent-Type: ' + content_type + '\r\nContent-Length: ' + str(len(body)) + extra
              + '\r\n\r\n').encode('ascii')
    connection.sendall(header + body)


class _RelayHandler(BaseRequestHandler):
    def handle(self):
        timer = None
        lifetime = math.inf
        try:
            if self.server.keep_alive:
                lifetime = time.monotonic() + KEEP_ALIVE_LIFETIME_SECONDS
                # This deadline never resets on traffic. Even a client slowly
                # reading replies cannot keep its socket beyond the hard cap.
                timer = threading.Timer(KEEP_ALIVE_LIFETIME_SECONDS, _shutdown, args=(self.request,))
                timer.daemon = True
                timer.start()
            count = KEEP_ALIVE_MAX_REQUESTS if self.server.keep_alive else 1
            for index in range(count):
                if not self._request(index, lifetime):
                    return
        except RelayError as exc:
            try:
                self.request.settimeout(.1)
                _send(self.request, exc.status)
            except OSError:
                pass
        except (OSError, TimeoutError):
            pass
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(timeout=.1)

    def _request(self, index, lifetime):
        first_deadline = min(lifetime, time.monotonic()
                             + (KEEP_ALIVE_IDLE_SECONDS if index else CLIENT_TIMEOUT))
        deadline = None
        data = bytearray()
        while b'\r\n\r\n' not in data:
            remaining = (deadline or first_deadline) - time.monotonic()
            if remaining <= 0:
                raise RelayError(408)
            self.request.settimeout(remaining)
            chunk = self.request.recv(min(1024, MAX_REQUEST_HEADERS + 1 - len(data)))
            if not chunk:
                return False  # Graceful EOF/half-close, never a synthetic success.
            if deadline is None:
                # Initial requests retain their original absolute deadline;
                # a reused socket has a separate idle and per-request budget.
                deadline = first_deadline if index == 0 else min(lifetime, time.monotonic() + CLIENT_TIMEOUT)
            data.extend(chunk)
            if len(data) > MAX_REQUEST_HEADERS:
                raise RelayError(431)
        head, body = bytes(data).split(b'\r\n\r\n', 1)
        lines = head.split(b'\r\n')
        request = lines[0].split(b' ')
        if len(request) != 3 or request[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
            raise RelayError(400)
        method, target, version = request
        if method != b'GET':
            raise RelayError(405)
        headers = {}
        for line in lines[1:]:
            if b':' not in line:
                raise RelayError(400)
            name, value = line.split(b':', 1)
            if not TOKEN.fullmatch(name) or name.lower() in headers or any(byte < 32 or byte > 126 for byte in value):
                raise RelayError(400)
            headers[name.lower()] = value.strip()
        # Never discard extra bytes then interpret the following request. This
        # endpoint deliberately rejects bodies and coalesced HTTP pipelining.
        if body or b'transfer-encoding' in headers or headers.get(b'content-length', b'0') != b'0':
            raise RelayError(400)
        if b'?' in target or b'#' in target:
            raise RelayError(400)
        try:
            path = target.decode('ascii')
        except UnicodeError as exc:
            raise RelayError(400) from exc
        if path not in PATHS:
            raise RelayError(404)
        content_type, payload, metadata = self.server.source.fetch(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError(408)
        self.request.settimeout(remaining)
        connection_tokens = {token.strip().lower() for token in headers.get(b'connection', b'').split(b',')}
        keep_alive = (self.server.keep_alive and version == b'HTTP/1.1' and path != '/status'
                      and b'close' not in connection_tokens and index + 1 < KEEP_ALIVE_MAX_REQUESTS)
        _send(self.request, 200, payload, content_type, metadata, keep_alive=keep_alive)
        return keep_alive


class _RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = MAX_WORKERS
    allow_reuse_address = True

    def __init__(self, address=(HOST, PORT), *, upstream_port=UPSTREAM_PORT, preview_size=DEFAULT_PREVIEW_SIZE,
                 keep_alive=False):
        if address[0] != HOST:
            raise ValueError('Relay must bind fixed loopback')
        if type(keep_alive) is not bool:
            raise ValueError('Keep-alive must be an explicit boolean')
        self.keep_alive = keep_alive
        self.source = _PreviewSource(upstream_port, preview_size=preview_size)
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__(address, _RelayHandler)

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
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never emit headers, upstream bodies, frames, or request data.


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fixed loopback read-only single-frame preview relay')
    parser.add_argument('--preview-size', choices=tuple(PREVIEW_SIZES), default=DEFAULT_PREVIEW_SIZE,
                        help='JPEG bounding box; proportional downscaling only (default: 640x480)')
    parser.add_argument('--keep-alive', action='store_true',
                        help='Reuse successful single-frame HTTP connections with hard idle/lifetime/request limits')
    options = parser.parse_args(argv)  # No options can change either endpoint.
    try:
        cv2, _ = _codec()
        cv2.setNumThreads(1)
    except Exception:
        parser.error('JPEG preview requires working OpenCV and NumPy')
    with _RelayServer(preview_size=options.preview_size, keep_alive=options.keep_alive) as server:
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
