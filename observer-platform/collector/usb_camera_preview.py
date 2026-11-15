"""Independent, fixed-device, low-resolution USB preview. No ROS or robot control.

The four port identities were audited before deployment. Changing this map is a
code/configuration review, never an HTTP request or an arbitrary CLI pathname.
Each V4L2 reader is process-isolated so a blocked USB ioctl cannot strand the
other cameras or prevent the supervisor from closing its device descriptors.
"""
from __future__ import annotations

import argparse
from collections import deque
import fcntl
from http.server import ThreadingHTTPServer
import json
import math
import multiprocessing
import os
import re
import signal
from socketserver import BaseRequestHandler
import stat
import threading
import time

HOST, PORT = '127.0.0.1', 8080
WIDTH, HEIGHT, JPEG_QUALITY, ENCODE_FPS = 640, 360, 70, 5
FRESH_SECONDS, WATCHDOG_SECONDS, RETRY_SECONDS = 2.0, 3.0, 2.0
MAX_JPEG_BYTES, MAX_HEADERS, MAX_HTTP_WORKERS = 500_000, 4096, 5
MAX_METADATA_BYTES, MAX_READ_GAP = 8192, .5
DEVICE_PREFIX = '/dev/v4l/by-path/platform-3610000.usb-usb-0:'
CAMERAS = {
    'camera1': {'port': '2.3', 'label': '左视相机', 'fps': 10},
    'camera2': {'port': '2.4', 'label': '前视相机', 'fps': 10},
    'camera3': {'port': '2.1.3', 'label': '扩展相机 · 接口 2.1.3', 'fps': 30},
    'camera4': {'port': '2.1.2', 'label': '扩展相机 · 接口 2.1.2', 'fps': 30},
}
PATHS = frozenset({'/status'} | {'/stream/' + key for key in CAMERAS})
TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
REASONS = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 405: 'Method Not Allowed',
           408: 'Request Timeout', 422: 'Unprocessable Content', 431: 'Request Header Fields Too Large',
           503: 'Service Unavailable'}


def device_path(key):
    return DEVICE_PREFIX + CAMERAS[key]['port'] + ':1.0-video-index0'


def _codec():
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    import cv2
    cv2.setNumThreads(1)
    return cv2


class LatestSlot:
    """Fixed shared memory; no pipe/feeder can block on a partially written JPEG.

    A killed writer may leave its lock held. Readers time out in 10 ms and the
    watchdog replaces the whole slot/lock when it restarts that one process.
    """
    def __init__(self, context):
        self.buffer = context.RawArray('B', 4 + MAX_METADATA_BYTES + MAX_JPEG_BYTES)
        self.length, self.version = context.RawValue('I', 0), context.RawValue('Q', 0)
        self.lock = context.Lock()

    def publish(self, packet):
        raw = packet.get('jpeg', b'')
        metadata = json.dumps({key: value for key, value in packet.items() if key != 'jpeg'},
                              ensure_ascii=False, allow_nan=False).encode('utf-8')
        if not isinstance(raw, bytes) or len(raw) > MAX_JPEG_BYTES or len(metadata) > MAX_METADATA_BYTES:
            raise ValueError('Capture packet limit exceeded')
        data = len(metadata).to_bytes(4, 'big') + metadata + raw
        if not self.lock.acquire(timeout=.01):
            return False
        try:
            memoryview(self.buffer).cast('B')[:len(data)] = data
            self.length.value = len(data)
            self.version.value += 1
        finally:
            self.lock.release()
        return True

    def read_after(self, version):
        if not self.lock.acquire(timeout=.01):
            return None
        try:
            current, length = self.version.value, self.length.value
            if current == version or not length:
                return None
            if length > len(self.buffer) or length < 4:
                raise ValueError('Invalid capture packet length')
            data = bytes(memoryview(self.buffer).cast('B')[:length])
        finally:
            self.lock.release()
        metadata_length = int.from_bytes(data[:4], 'big')
        if not 0 < metadata_length <= MAX_METADATA_BYTES or 4 + metadata_length > len(data):
            raise ValueError('Invalid capture metadata length')
        packet = json.loads(data[4:4 + metadata_length])
        if len(data) > 4 + metadata_length:
            packet['jpeg'] = data[4 + metadata_length:]
        return current, packet


def _fourcc(value):
    if not math.isfinite(value) or value < 0 or value > 0xffffffff:
        return ''
    number = int(value)
    return ''.join(chr((number >> (index * 8)) & 255) for index in range(4))


def _capture_session(key, output, stopping, cv2, clock=time.monotonic, exists=os.path.exists):
    """Continuously drain one capture; all outputs are bounded, immutable packets."""
    capture, negotiated, frames, encoded_frames = None, None, 0, 0
    def report(error, **extra):
        output.publish(dict(error=error, negotiated=negotiated, frames=frames,
                            encoded_frames=encoded_frames, **extra))
    try:
        if not exists(device_path(key)):
            report('相机设备不存在')
            return
        report('正在打开相机')
        capture = cv2.VideoCapture()
        if not capture.open(device_path(key), cv2.CAP_V4L2):
            report('打开相机失败')
            return
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, CAMERAS[key]['fps'])
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        width, height, fps = (float(capture.get(prop)) for prop in
                              (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT, cv2.CAP_PROP_FPS))
        fourcc = _fourcc(float(capture.get(cv2.CAP_PROP_FOURCC)))
        buffers = float(capture.get(cv2.CAP_PROP_BUFFERSIZE))
        negotiated = {'width': width if math.isfinite(width) else None,
                      'height': height if math.isfinite(height) else None,
                      'fps': fps if math.isfinite(fps) else None, 'fourcc': fourcc,
                      'buffer_size': buffers if math.isfinite(buffers) else None}
        if width != WIDTH or height != HEIGHT or fourcc != 'MJPG' or not math.isfinite(fps) or abs(fps - CAMERAS[key]['fps']) > .5:
            report('相机未接受请求的 MJPG 分辨率或帧率')
            return
        if buffers != 1:
            report('相机未接受单帧采集缓冲')
            return
        report('等待首帧')
        next_encode, times = -math.inf, deque(maxlen=60)
        last_read = clock()
        while not stopping.is_set():
            started = clock()
            if frames and started - last_read > MAX_READ_GAP:
                report('取帧间隔过长，重新建立采集')
                return
            ok, frame = capture.read()
            observed = clock()
            last_read = observed
            if stopping.is_set():
                break
            if not ok or frame is None or getattr(frame, 'shape', None) != (HEIGHT, WIDTH, 3):
                report('读取相机失败')
                return
            if observed - started > FRESH_SECONDS:
                report('相机读取超时')
                return
            frames += 1
            times.append(observed)
            # Drain startup buffers and keep consuming every driver frame even
            # while encoding/publishing at a much lower bounded preview rate.
            if frames <= 4 or observed < next_encode:
                continue
            next_encode = observed + 1 / ENCODE_FPS
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            raw = encoded.tobytes() if ok else b''
            if not 16 <= len(raw) <= MAX_JPEG_BYTES or not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
                report('JPEG 编码失败')
                return
            # This is a conservative read-observation bound, NOT a hardware
            # exposure timestamp. Encoding/IPC/network time never renews it.
            capture_at = started - 1 / fps
            if clock() - capture_at > FRESH_SECONDS:
                report('相机图像已过期')
                return
            encoded_frames += 1
            rate = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else None
            report('', jpeg=raw, observed_at=capture_at, capture_hz=rate)
    except Exception:
        report('相机采集异常')  # No exception text, paths or image bytes in logs.
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def _capture_worker(key, output, stopping):
    try:
        cv2 = _codec()
        while not stopping.is_set():
            _capture_session(key, output, stopping, cv2)
            stopping.wait(RETRY_SECONDS)
    except Exception:
        output.publish({'error': '相机编码依赖不可用', 'negotiated': None, 'frames': 0, 'encoded_frames': 0})


class PreviewState:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.values = {key: {'error': '等待相机', 'negotiated': None, 'frames': 0, 'encoded_frames': 0} for key in CAMERAS}

    def apply(self, key, packet):
        with self.lock:
            self.values[key] = dict(packet)

    def fail(self, key, error):
        with self.lock:
            previous = self.values[key]
            self.values[key] = {name: previous.get(name) for name in ('negotiated', 'frames', 'encoded_frames')}
            self.values[key]['error'] = error

    def _item(self, key, now):
        value = self.values[key]
        stamp = value.get('observed_at')
        age = max(0, now - stamp) if type(stamp) in (float, int) and math.isfinite(stamp) else None
        error = value.get('error', '等待相机')
        if not error and (age is None or age > FRESH_SECONDS or not value.get('jpeg')):
            error = '相机图像已过期'
        metadata = {'error': error, 'age_s': age if not error else None,
                    'status': 'AVAILABLE' if not error else 'ERROR',
                    'label': CAMERAS[key]['label'], 'device': device_path(key),
                    'requested': {'width': WIDTH, 'height': HEIGHT, 'fourcc': 'MJPG', 'fps': CAMERAS[key]['fps']},
                    'negotiated': value.get('negotiated'), 'frames': value.get('frames', 0),
                    'encoded_frames': value.get('encoded_frames', 0),
                    'capture_hz': value.get('capture_hz') if not error else None,
                    'age_basis': 'conservative_read_observation', 'preview_max_fps': ENCODE_FPS}
        return metadata, value.get('jpeg') if not error else None

    def status(self):
        with self.lock:
            now = self.clock()
            return {key: self._item(key, now)[0] for key in CAMERAS}

    def frame(self, key):
        with self.lock:
            return self._item(key, self.clock())[1]


class CaptureSupervisor:
    def __init__(self, state, context=None):
        self.state, self.context = state, context or multiprocessing.get_context('spawn')
        self.stopping, self.children = threading.Event(), {}
        self.thread = None

    def _start(self, key):
        output, stop = LatestSlot(self.context), self.context.Event()
        process = self.context.Process(target=_capture_worker, args=(key, output, stop), daemon=True,
                                       name='usb-preview-' + key)
        process.start()
        return {'process': process, 'slot': output, 'stop': stop,
                'heard': time.monotonic(), 'retry': 0, 'version': 0}

    @staticmethod
    def _end(child):
        child['stop'].set()
        process = child['process']
        process.join(timeout=.15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=.3)
        if process.is_alive():
            process.kill()
            process.join(timeout=.3)
        if process.is_alive():
            return False
        return True

    def _monitor(self):
        try:
            while not self.stopping.is_set():
                for key in CAMERAS:
                    if self.stopping.is_set():
                        break
                    child, now = self.children.get(key), time.monotonic()
                    if child is None or child.get('ended'):
                        if child is None or now >= child['retry']:
                            try:
                                self.children[key] = self._start(key)
                            except Exception:
                                self.state.fail(key, '无法启动独立相机采集')
                                self.children[key] = {'ended': True, 'retry': now + RETRY_SECONDS}
                        continue
                    try:
                        received = child['slot'].read_after(child['version'])
                        if received is not None:
                            child['version'], packet = received
                            child['heard'] = now
                            self.state.apply(key, packet)
                    except (ValueError, TypeError, OSError):
                        self.state.fail(key, '采集进程数据异常')
                    if not child['process'].is_alive() or now - child['heard'] > WATCHDOG_SECONDS:
                        self.state.fail(key, '相机采集进程中断或超时')
                        if self._end(child):
                            child.update(ended=True, retry=now + RETRY_SECONDS)
                self.stopping.wait(.05)
        finally:
            # Only the supervisor thread owns/changes children; stop() never
            # races an in-progress spawn or iterates a concurrently changing map.
            for key, child in self.children.items():
                if not child.get('ended'):
                    self._end(child)
                self.state.fail(key, '相机预览已停止')

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._monitor, name='usb-preview-supervisor', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=7)


class RequestError(Exception):
    def __init__(self, status):
        self.status = status


def _send(connection, status, body=b'', content_type='text/plain; charset=utf-8'):
    header = ('HTTP/1.1 ' + str(status) + ' ' + REASONS[status] + '\r\nConnection: close'
              '\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Type: '
              + content_type + '\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode('ascii')
    connection.sendall(header + body)


class PreviewHandler(BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + 1
        try:
            data = bytearray()
            while b'\r\n\r\n' not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RequestError(408)
                self.request.settimeout(remaining)
                chunk = self.request.recv(min(1024, MAX_HEADERS + 1 - len(data)))
                if not chunk:
                    return
                data.extend(chunk)
                if len(data) > MAX_HEADERS:
                    raise RequestError(431)
            head, body = bytes(data).split(b'\r\n\r\n', 1)
            lines = head.split(b'\r\n')
            request = lines[0].split(b' ')
            if len(request) != 3 or request[2] not in (b'HTTP/1.0', b'HTTP/1.1'):
                raise RequestError(400)
            method, target, _ = request
            if method != b'GET':
                raise RequestError(405)
            headers = {}
            for line in lines[1:]:
                name, separator, value = line.partition(b':')
                if not separator or not TOKEN.fullmatch(name) or name.lower() in headers or any(byte < 32 or byte > 126 for byte in value):
                    raise RequestError(400)
                headers[name.lower()] = value.strip()
            if body or b'transfer-encoding' in headers or headers.get(b'content-length', b'0') != b'0':
                raise RequestError(400)
            if b'?' in target or b'#' in target:
                raise RequestError(400)
            path = target.decode('ascii')
            if path not in PATHS:
                raise RequestError(404)
            if path == '/status':
                payload = json.dumps(self.server.state.status(), ensure_ascii=False, allow_nan=False).encode('utf-8')
                if len(payload) > 32768:
                    raise RequestError(503)
                content_type = 'application/json'
            else:
                raw = self.server.state.frame(path.rsplit('/', 1)[1])
                if raw is None:
                    raise RequestError(422)
                payload = (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                           + str(len(raw)).encode('ascii') + b'\r\n\r\n' + raw + b'\r\n--frame--\r\n')
                content_type = 'multipart/x-mixed-replace; boundary=frame'
            self.request.settimeout(max(.01, deadline - time.monotonic()))
            _send(self.request, 200, payload, content_type)
        except (RequestError, UnicodeError) as exc:
            try:
                self.request.settimeout(.1)
                _send(self.request, exc.status if isinstance(exc, RequestError) else 400)
            except OSError:
                pass
        except OSError:
            pass


class PreviewServer(ThreadingHTTPServer):
    daemon_threads, allow_reuse_address = True, True
    request_queue_size = MAX_HTTP_WORKERS

    def __init__(self, state, address=(HOST, PORT)):
        if address[0] != HOST:
            raise ValueError('Preview binds only fixed loopback')
        self.state, self.slots = state, threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        super().__init__(address, PreviewHandler)

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
        pass


def _instance_lock():
    path = '/tmp/robot-cloud-usb-camera-preview-' + str(os.getuid()) + '.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError('Unsafe preview lock file')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent fixed-port 640x360 MJPG USB camera preview')
    parser.parse_args(argv)  # No path, network origin or camera controls from CLI.
    descriptor = _instance_lock()
    state, stopping = PreviewState(), threading.Event()
    supervisor = CaptureSupervisor(state)
    try:
        # Bind before opening devices; an existing source on 8080 prevents any
        # competing capture. Deployment additionally checks old video owners.
        with PreviewServer(state) as server:
            server.timeout = .2
            signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            signal.signal(signal.SIGINT, lambda *_: stopping.set())
            supervisor.start()
            try:
                while not stopping.is_set():
                    server.handle_request()
            finally:
                supervisor.stop()
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
