#!/usr/bin/env python3
"""Read existing camera caches; publish bounded detection metadata, never video.

No V4L2 access, camera ownership changes, image files, remote URLs, model
downloads or robot control imports are used by this process. Its only ROS
publisher reports read-only 2-D marker IDs in a separate observer namespace.
"""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import struct
import tempfile
import threading
import time
import uuid


NATIVE = {'usbFront': 'camera2', 'usbLeft': 'camera1',
          'usbHub3': 'camera3', 'usbHub2': 'camera4'}
KEYS = (*NATIVE, 'color')
COLOR_TOPIC = '/observer_camera/color/compressed'
MAX_JPEG = 8 * 1024 * 1024
MAX_FRAME_AGE = 1.5
HEX32 = re.compile(r'^[0-9a-f]{32}$')


def jpeg_dimensions(raw):
    """Reject impossible allocations before JPEG decoding."""
    if not 16 <= len(raw) <= MAX_JPEG or raw[:2] != b'\xff\xd8' or raw[-2:] != b'\xff\xd9':
        raise ValueError('invalid JPEG')
    pos, dimensions = 2, None
    while pos < len(raw) - 2:
        if raw[pos] != 255:
            raise ValueError('invalid JPEG marker')
        while pos < len(raw) and raw[pos] == 255:
            pos += 1
        if pos >= len(raw):
            break
        marker = raw[pos]
        pos += 1
        if marker in (0, 0xd8, 0xd9) or 0xd0 <= marker <= 0xd7 or pos + 2 > len(raw):
            raise ValueError('invalid JPEG segment')
        length = int.from_bytes(raw[pos:pos + 2], 'big')
        if length < 2 or pos + length > len(raw):
            raise ValueError('invalid JPEG length')
        if marker in (0xc0, 0xc1, 0xc2):
            if dimensions is not None or length < 11:
                raise ValueError('invalid JPEG dimensions')
            height = int.from_bytes(raw[pos + 3:pos + 5], 'big')
            width = int.from_bytes(raw[pos + 5:pos + 7], 'big')
            if (raw[pos + 2] != 8 or raw[pos + 7] not in (1, 3)
                    or length != 8 + 3 * raw[pos + 7]
                    or not 1 <= min(width, height) or max(width, height) > 4096
                    or width * height > 8_000_000):
                raise ValueError('unsupported JPEG dimensions')
            dimensions = width, height
        if marker == 0xda:
            if dimensions is None or pos + length >= len(raw) - 2:
                raise ValueError('invalid JPEG scan')
            return dimensions
        pos += length
    raise ValueError('missing JPEG scan')


def native_packet(headers, raw, requested, received):
    width, height = jpeg_dimensions(raw)
    session = headers.get('X-Video-Session', '')
    sequence = headers.get('X-Frame-Sequence', '')
    if not HEX32.fullmatch(session) or not sequence.isascii() or not sequence.isdigit() or len(sequence) > 16:
        raise ValueError('invalid native identity')
    media_time = float(headers.get('X-Video-Media-Time', 'nan'))
    age_ms = float(headers.get('X-Frame-Age-Ms', 'nan'))
    if (not math.isfinite(media_time) or not 0 <= media_time < 1e9
            or not math.isfinite(age_ms) or not 0 <= age_ms <= MAX_FRAME_AGE * 1000
            or not 0 <= received - requested <= 1.0):
        raise ValueError('stale native frame')
    # Starting the age at request time is conservative about transfer duration.
    captured = requested - age_ms / 1000
    if received - captured > MAX_FRAME_AGE:
        raise ValueError('expired native frame')
    return dict(jpeg=raw, width=width, height=height, captured=captured,
                token=(session, sequence), streamSession=session,
                mediaTime=media_time, imageId=None)


def color_packet(raw, captured, source_sequence=None):
    width, height = jpeg_dimensions(raw)
    image_id = hashlib.sha256(raw).hexdigest()
    return dict(jpeg=raw, width=width, height=height, captured=captured,
                token=(image_id, captured if source_sequence is None else source_sequence),
                streamSession=None, mediaTime=None, imageId=image_id)


class LatestFrames:
    """One slot per source, duplicate suppression and bounded fair scheduling."""
    def __init__(self):
        self.lock = threading.Lock()
        self.latest, self.used = {}, {}
        self.states = {key: 'WAITING_UPSTREAM' for key in KEYS}
        self.native_order = deque(NATIVE)
        self.last_was_color = False

    def put(self, key, packet):
        with self.lock:
            self.latest[key] = packet
            self.states[key] = 'AVAILABLE'

    def unavailable(self, key, *, error=False):
        with self.lock:
            self.latest.pop(key, None)
            self.states[key] = 'ERROR' if error else 'WAITING_UPSTREAM'

    def take(self, now):
        with self.lock:
            # Color may run at 5 Hz to match every uploaded JPEG. Alternate it
            # with native work under saturation so no source can starve.
            order = list(self.native_order)
            order = [*order, 'color'] if self.last_was_color else ['color', *order]
            for key in order:
                frame = self.latest.get(key)
                if frame is None or self.used.get(key) == frame['token']:
                    continue
                if not 0 <= now - frame['captured'] <= MAX_FRAME_AGE:
                    self.states[key] = 'WAITING_UPSTREAM'
                    continue
                self.used[key] = frame['token']
                self.last_was_color = key == 'color'
                if key != 'color':
                    self.native_order.rotate(-1)
                    while self.native_order[-1] != key:
                        self.native_order.rotate(-1)
                return key, frame
        return None

    def status(self, key, now):
        with self.lock:
            frame = self.latest.get(key)
            state = self.states[key]
            if state == 'AVAILABLE' and (frame is None or now - frame['captured'] > MAX_FRAME_AGE):
                return 'WAITING_UPSTREAM'
            return state


def bounded_boxes(rows, width, height):
    boxes = []
    for row in rows[:12]:
        if len(row) < 6:
            continue
        x1, y1, x2, y2, confidence, class_id = map(float, row[:6])
        if (not all(math.isfinite(v) for v in (x1, y1, x2, y2, confidence, class_id))
                or not 0 <= confidence <= 1 or not class_id.is_integer()
                or not 0 <= class_id < 80):
            continue
        coords = [max(0., min(1., x1 / width)), max(0., min(1., y1 / height)),
                  max(0., min(1., x2 / width)), max(0., min(1., y2 / height))]
        if coords[2] <= coords[0] or coords[3] <= coords[1]:
            continue
        boxes.append(dict(classId=int(class_id), confidence=round(confidence, 5),
                          xyxy=[round(v, 6) for v in coords]))
    return boxes


class Results:
    def __init__(self, frames):
        self.frames = frames
        self.session = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.counter = 0
        self.history = {key: deque(maxlen=10) for key in KEYS}

    def add(self, key, packet, boxes, inference_ms, markers=None):
        with self.lock:
            self.counter += 1
            result = {field: packet[field] for field in
                      ('width', 'height', 'streamSession', 'mediaTime', 'imageId')}
            result.update(id=f'{self.session}:{self.counter}', boxes=boxes,
                          markers=[] if markers is None else markers,
                          inferenceMs=round(inference_ms, 3), captured=packet['captured'])
            # A stream restart must not mix associations from the prior encoder.
            history = self.history[key]
            if history and history[-1]['streamSession'] != result['streamSession']:
                history.clear()
            history.append(result)

    def snapshot(self, now):
        rows = {}
        with self.lock:
            for key, history in self.history.items():
                while history and now - history[0]['captured'] > 5:
                    history.popleft()
                source_state = self.frames.status(key, now)
                active = bool(history) and now - history[-1]['captured'] <= 2
                state = source_state if source_state != 'AVAILABLE' else ('AVAILABLE' if active else 'WAITING_UPSTREAM')
                results = []
                if state == 'AVAILABLE':
                    for result in history:
                        row = {k: v for k, v in result.items() if k != 'captured'}
                        row['ageMs'] = round(max(0., now - result['captured']) * 1000, 3)
                        results.append(row)
                rows[key] = dict(status=state, results=results)
        payload = dict(version=1, session=self.session, model='YOLO26n',
                       generatedMonotonic=now, cameras=rows)
        return bound_history(payload)

    def marker_snapshot(self, now, wall):
        """Small latest-only ROS message; never publish pose or docking inputs."""
        rows = {}
        with self.lock:
            for key, history in self.history.items():
                source_state = self.frames.status(key, now)
                latest = history[-1] if history else None
                active = (latest is not None and source_state == 'AVAILABLE'
                          and 0 <= now - latest['captured'] <= 2)
                state = ('AVAILABLE' if active else
                         source_state if source_state != 'AVAILABLE' else 'WAITING_UPSTREAM')
                rows[key] = dict(status=state,
                    ageMs=round((now - latest['captured']) * 1000, 3) if active else None,
                    dictionary='DICT_5X5_100',
                    ids=sorted({marker['id'] for marker in latest.get('markers', [])}) if active else [])
        return dict(version=1, timestamp=wall, cameras=rows)


def bound_history(payload):
    """Keep complete latest results while bounding worst-case dense histories.

    Twelve object boxes plus twelve marker quadrilaterals can exceed the
    private metadata limit when all five histories are full. Drop complete
    oldest frames first, never individual detections or a source's last frame.
    """
    while len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                         allow_nan=False).encode()) > 124_000:
        candidates = [row['results'] for row in payload['cameras'].values()
                      if len(row['results']) > 1]
        if not candidates:
            raise ValueError('latest detection metadata exceeds bound')
        oldest = max(candidates, key=lambda history: history[0]['ageMs'])
        oldest.pop(0)
    return payload


def write_atomic(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 128_000:
        raise ValueError('detection metadata exceeds bound')
    fd, temporary = tempfile.mkstemp(prefix='.yolo-detections-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MarkerWorkers:
    """Three full-resolution CPU detectors, five bounded latest source slots.

    YOLO remains confined to the caller's single GPU thread. A source can
    have at most one marker job queued or running, and each result publishes
    its object boxes and marker corners together with the original frame ID.
    """
    def __init__(self, frames, results, stop, detector_factory, *, clock=time.monotonic):
        self.frames, self.results, self.stop = frames, results, stop
        self.detector_factory, self.clock = detector_factory, clock
        self.lock = threading.Lock()
        self.inflight = set()
        self.tasks = queue.Queue(maxsize=len(KEYS))
        self.threads = []

    def start(self):
        self.threads = [threading.Thread(target=self._run, name=f'observer_aruco_{index}', daemon=True)
                        for index in range(3)]
        for thread in self.threads:
            thread.start()

    def busy(self, key):
        with self.lock:
            return key in self.inflight

    def submit(self, key, packet, boxes, started):
        if key not in KEYS:
            raise ValueError('unsupported marker source')
        with self.lock:
            if self.stop.is_set() or key in self.inflight:
                return False
            self.inflight.add(key)
            try:
                self.tasks.put_nowait((key, packet, boxes, started))
            except queue.Full:
                self.inflight.remove(key)
                return False
            return True

    def _run(self):
        try:
            detector = self.detector_factory()
        except Exception:
            print('ArUco detector initialization failed.', flush=True)
            self.stop.set()
            return
        while not self.stop.is_set():
            try:
                key, packet, boxes, started = self.tasks.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if self.clock() - packet['captured'] > MAX_FRAME_AGE:
                    continue
                markers = detector.detect(packet['jpeg'], packet['width'], packet['height'])
                finished = self.clock()
                if (not self.stop.is_set()
                        and 0 <= finished - packet['captured'] <= MAX_FRAME_AGE):
                    self.results.add(key, packet, boxes, (finished - started) * 1000, markers)
            except Exception:
                self.frames.unavailable(key, error=True)
                print(f'{key}: marker inference unavailable.', flush=True)
            finally:
                with self.lock:
                    self.inflight.discard(key)
                self.tasks.task_done()

    def join(self):
        for thread in self.threads:
            thread.join(timeout=2)
        while True:
            try:
                key, *_ = self.tasks.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                self.inflight.discard(key)
            self.tasks.task_done()


def native_reader(key, camera_id, frames, stop):
    while not stop.is_set():
        started = time.monotonic()
        connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=.7)
        try:
            connection.request('GET', f'/camera/{camera_id}/inference.jpg')
            with connection.getresponse() as response:
                if response.status != 200 or response.getheader('Content-Type') != 'image/jpeg':
                    raise OSError('frame unavailable')
                length = response.getheader('Content-Length', '')
                if not length.isascii() or not length.isdigit() or not 16 <= int(length) <= MAX_JPEG:
                    raise ValueError('invalid image size')
                raw = response.read(int(length) + 1)
                if len(raw) != int(length):
                    raise ValueError('truncated image')
                packet = native_packet(response.headers, raw, started, time.monotonic())
                frames.put(key, packet)
        except (OSError, http.client.HTTPException, ValueError, OverflowError):
            frames.unavailable(key)
        finally:
            connection.close()
        stop.wait(max(0, .5 - (time.monotonic() - started)))


def color_subscriber(frames, stop, results):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import String
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = Node('observer_yolo_color_receiver', enable_rosout=False, start_parameter_services=False)

    def receive(message):
        try:
            if message.format.lower() not in ('jpeg', 'jpg') or len(message.data) > MAX_JPEG:
                raise ValueError('unsupported color image')
            now, wall = time.monotonic(), time.time()
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
            age = wall - stamp
            if not math.isfinite(age) or not -.25 <= age <= MAX_FRAME_AGE:
                raise ValueError('stale color image')
            source_sequence = (message.header.stamp.sec, message.header.stamp.nanosec)
            frames.put('color', color_packet(bytes(message.data), now - max(0., age), source_sequence))
        except (ValueError, TypeError, OverflowError):
            frames.unavailable('color')

    node.create_subscription(CompressedImage, COLOR_TOPIC, receive,
                             QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
    publisher = node.create_publisher(String, '/observer_camera/aruco_detections', 1)

    def publish_markers():
        message = String()
        message.data = json.dumps(results.marker_snapshot(time.monotonic(), time.time()),
                                  separators=(',', ':'), allow_nan=False)
        if len(message.data.encode()) > 8192:
            raise ValueError('ArUco ROS summary exceeds bound')
        publisher.publish(message)

    node.create_timer(.5, publish_markers)
    try:
        while not stop.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=.1)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def validate_engine(path):
    if path.suffix != '.engine' or not path.is_file():
        raise ValueError('an existing local TensorRT engine is required')
    with path.open('rb') as handle:
        size_raw = handle.read(4)
        if len(size_raw) != 4:
            raise ValueError('missing engine metadata')
        size = struct.unpack('<I', size_raw)[0]
        if not 1 <= size <= 65536:
            raise ValueError('invalid engine metadata size')
        metadata = json.loads(handle.read(size))
    if (metadata.get('task') != 'detect' or metadata.get('batch') != 1
            or metadata.get('imgsz') != [640, 640] or len(metadata.get('names', {})) != 80):
        raise ValueError('expected the installed COCO 80-class batch-1 640 engine')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, default=Path.home() / 'yolo26_test/tensorrt_fp16/yolo26n.engine')
    parser.add_argument('--output', type=Path, default=Path.home() / '.local/state/robot-cloud/yolo-detections.json')
    args = parser.parse_args(argv)
    validate_engine(args.model)
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    os.environ.setdefault('YOLO_CONFIG_DIR', str(Path.home() / 'yolo26_test/config'))
    import cv2
    import numpy as np
    import torch
    from ultralytics import YOLO
    from aruco_detector import ArucoDetector
    cv2.setNumThreads(1)
    torch.set_num_threads(1)
    if not torch.cuda.is_available():
        raise RuntimeError('local CUDA is unavailable')
    model = YOLO(str(args.model), task='detect')
    predict = dict(imgsz=640, rect=False, conf=.25, max_det=12, device='0', save=False, verbose=False)
    # A synthetic warmup initializes TensorRT before any source is sampled.
    model.predict(np.zeros((640, 640, 3), dtype=np.uint8), **predict)
    print('YOLO26n and DICT_5X5_100 ready: four cached native sources and Berxel color.', flush=True)
    stop, frames = threading.Event(), LatestFrames()
    results = Results(frames)
    markers = MarkerWorkers(frames, results, stop, ArucoDetector)
    markers.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def guarded_color():
        try:
            color_subscriber(frames, stop, results)
        except Exception:
            frames.unavailable('color', error=True)
            print('Depth color subscription unavailable.', flush=True)

    def writer():
        try:
            while not stop.is_set():
                write_atomic(args.output, results.snapshot(time.monotonic()))
                stop.wait(.2)
        except Exception:
            print('Detection metadata writer failed.', flush=True)
            stop.set()

    threads = [threading.Thread(target=native_reader, args=(key, camera, frames, stop), daemon=True)
               for key, camera in NATIVE.items()]
    threads += [threading.Thread(target=guarded_color, daemon=True),
                threading.Thread(target=writer, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        while not stop.is_set():
            work = frames.take(time.monotonic())
            if work is None:
                stop.wait(.01)
                continue
            key, packet = work
            if markers.busy(key):
                continue
            started = time.monotonic()
            try:
                # Decode large JPEGs at reduced resolution; normalized boxes
                # still refer to the full source image without a second encode.
                maximum = max(packet['width'], packet['height'])
                mode = (cv2.IMREAD_REDUCED_COLOR_4 if maximum >= 1920 else
                        cv2.IMREAD_REDUCED_COLOR_2 if maximum >= 1280 else cv2.IMREAD_COLOR)
                image = cv2.imdecode(np.frombuffer(packet['jpeg'], dtype=np.uint8), mode)
                if image is None:
                    raise ValueError('JPEG decode failed')
                result = model.predict(image, **predict)[0]
                rows = result.boxes.cpu().data.tolist() if result.boxes is not None else []
                finished = time.monotonic()
                if finished - packet['captured'] <= MAX_FRAME_AGE:
                    # CPU marker workers decode the original JPEG in full.
                    # GPU inference never waits for marker thresholding and
                    # each worker publishes both detections for the same frame.
                    markers.submit(key, packet,
                                   bounded_boxes(rows, image.shape[1], image.shape[0]), started)
            except Exception:
                frames.unavailable(key, error=True)
                print(f'{key}: inference unavailable.', flush=True)
                stop.wait(.1)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        markers.join()
        for key in KEYS:
            frames.unavailable(key)
        write_atomic(args.output, results.snapshot(time.monotonic()))


if __name__ == '__main__':
    main()
