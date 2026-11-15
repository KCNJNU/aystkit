"""Bounded GET-only preview receiver behind the operator's loopback SSH tunnel.

This adapter never opens a camera, imports ROS, or sends a camera command. HTTP
previews provide no sampling timestamp/sequence: only a changed source JPEG
identity with bounded source age can count as a newly observed preview.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import threading
import time
from collections import deque

import aiohttp
import cv2
import numpy as np

from shared.protocol import HTTP_CAMERA_KEYS, decode_media
from .media import jpeg_dimensions

HTTP_CAMERA_URL = 'http://127.0.0.1:18080'
CAMERA_SOURCES = {
    'usbRear': ('camera1', '后视相机'),
    'usbFront': ('camera2', '前视相机'),
    'usbLeft': ('camera3', '左视相机'),
}
# Endpoint numbers are profile-local aliases, not hardware identities. This
# mapping must be re-audited if the upstream camera_map changes. usb* keys stay
# bound to the same physical cameras; their names are historical aliases. Only
# the current four-camera hub profile follows the corrected vehicle directions.
CAMERA_SOURCE_PROFILES = {
    'legacy3': CAMERA_SOURCES,
    'jetson-hub4': {
        'usbFront': ('camera2', '后视相机'),
        'usbLeft': ('camera1', '前视相机'),
        'usbHub2': ('camera4', '左视相机'),
        'usbHub3': ('camera3', '右视相机'),
    },
    'jetson-direct3': {
        'usbFront': ('camera2', '前视相机'),
        'usbLeft': ('camera1', '左视相机'),
        'usbPort21': ('camera3', '扩展相机 · 接口 2.1'),
    },
}
MAX_HEADER_BYTES = 4096
MAX_JPEG_BYTES = 500_000
FRESH_SECONDS = 2.0
REQUEST_TIMEOUT = 1.5
METADATA_RESEND_SECONDS = 30.0
DIAGNOSTIC_SECONDS = 60.0
NOTE = 'Jetson HTTP 只读预览；非 ROS2 图像 Topic'
LOG = logging.getLogger(__name__)
SOURCE_DIGEST = re.compile(r'^[0-9a-f]{64}$')
SOURCE_AGE = re.compile(r'^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$')


class PreviewError(ValueError):
    """A fixed safe error category, never an upstream response body."""


async def read_mjpeg_frame(response, *, finite=False):
    """Read one part; a private finite relay response must be fully consumed."""
    outer_length = None
    if finite:
        text = response.headers.get('Content-Length', '')
        if (not text.isascii() or not text.isdigit() or len(text) > 7
                or response.headers.get('Transfer-Encoding')
                or not 16 <= int(text) <= MAX_JPEG_BYTES + MAX_HEADER_BYTES + 22):
            raise PreviewError('Invalid finite preview length')
        outer_length = int(text)
    if response.content_type != 'multipart/x-mixed-replace':
        raise PreviewError('Invalid stream content type')
    # The audited upstream uses this fixed boundary, not user-provided paths.
    parameters = response.headers.get('Content-Type', '').split(';')
    boundaries = [part.strip().split('=', 1)[1].strip().strip('"')
                  for part in parameters[1:] if part.strip().lower().startswith('boundary=')]
    if boundaries != ['frame']:
        raise PreviewError('Invalid stream boundary')
    if await response.content.readline() != b'--frame\r\n':
        raise PreviewError('Invalid initial stream boundary')
    size, headers = 0, {}
    while True:
        line = await response.content.readline()
        size += len(line)
        if not line or size > MAX_HEADER_BYTES or not line.endswith(b'\r\n'):
            raise PreviewError('Invalid stream part headers')
        if line == b'\r\n':
            break
        try:
            name, value = line[:-2].decode('ascii').split(':', 1)
        except (UnicodeError, ValueError) as exc:
            raise PreviewError('Invalid stream part header') from exc
        name, value = name.strip().lower(), value.strip()
        if name in headers:
            raise PreviewError('Duplicate stream part header')
        headers[name] = value
    length = headers.get('content-length', '')
    if not length.isascii() or not length.isdigit() or len(length) > 7:
        raise PreviewError('Invalid stream part length')
    length = int(length)
    if headers.get('content-type', '').lower() != 'image/jpeg' or not 16 <= length <= MAX_JPEG_BYTES:
        raise PreviewError('Invalid JPEG part size or type')
    tail = b'\r\n--frame--\r\n'
    if finite and outer_length != 9 + size + length + len(tail):
        raise PreviewError('Invalid finite preview length')
    raw = await response.content.readexactly(length)
    if finite:
        # Drain only the fixed finite part terminator, never a continuous
        # camera stream. Complete Content-Length lets aiohttp reuse the SSH
        # channel; a pending tail would otherwise force the connection closed.
        if await response.content.readexactly(len(tail)) != tail or await response.content.read(1):
            raise PreviewError('Invalid finite preview terminator')
    return raw


def source_metadata(headers):
    """Strict private relay contract, never a physical capture sequence/UTC stamp."""
    values = []
    for name in ('X-Preview-Contract', 'X-Source-SHA256', 'X-Source-Age-S'):
        entries = headers.getall(name, [])
        if len(entries) != 1 or not isinstance(entries[0], str):
            raise PreviewError('Missing or duplicate preview contract metadata')
        values.append(entries[0])
    version, digest, text_age = values
    if version != '1' or not SOURCE_DIGEST.fullmatch(digest):
        raise PreviewError('Invalid preview contract identity')
    if not text_age or len(text_age) > 32 or not SOURCE_AGE.fullmatch(text_age):
        raise PreviewError('Invalid preview contract age')
    try:
        age = float(text_age)
    except ValueError as exc:
        raise PreviewError('Invalid preview contract age') from exc
    if not math.isfinite(age) or age < 0:
        raise PreviewError('Invalid preview contract age')
    return digest, age


def preview_jpeg(raw):
    """Always validate decoded pixels; preserve an already bounded relay JPEG."""
    width, height = jpeg_dimensions(raw)
    decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None or decoded.shape[:2] != (height, width):
        raise PreviewError('JPEG decode failed')
    scale = min(1.0, 960 / width, 720 / height)
    if scale == 1:
        decode_media({'kind': 'image', 'data': base64.b64encode(raw).decode('ascii'), 'mime_type': 'image/jpeg'})
        return raw, width, height, width, height
    if scale < 1:
        decoded = cv2.resize(decoded, (max(1, round(width * scale)), max(1, round(height * scale))),
                             interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', decoded, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok or len(encoded) > MAX_JPEG_BYTES:
        raise PreviewError('JPEG preview encoding failed')
    result = encoded.tobytes()
    decode_media({'kind': 'image', 'data': base64.b64encode(result).decode('ascii'), 'mime_type': 'image/jpeg'})
    return result, width, height, decoded.shape[1], decoded.shape[0]


class HttpCameraCollector:
    def __init__(self, config, uploader, clock=time.monotonic, wall_clock=time.time):
        if config.http_camera_url != HTTP_CAMERA_URL:
            raise ValueError('HTTP camera source must be the fixed loopback SSH tunnel')
        self.config, self.uploader = config, uploader
        self.clock, self.wall_clock = clock, wall_clock
        profile = getattr(config, 'http_camera_profile', 'legacy3')
        if profile not in CAMERA_SOURCE_PROFILES:
            raise ValueError('HTTP camera profile is not audited')
        self.sources = CAMERA_SOURCE_PROFILES[profile]
        self.keys = tuple(key for key in config.media_keys if key in HTTP_CAMERA_KEYS)
        if any(key not in self.sources for key in self.keys):
            raise ValueError('HTTP camera key lacks an audited source in this profile')
        preview_fps = getattr(config, 'http_camera_fps', None)
        if preview_fps is None:
            preview_fps = min(1.0, config.image_fps)  # Preserve the legacy default cap.
        if (isinstance(preview_fps, bool) or not isinstance(preview_fps, (int, float))
                or not math.isfinite(preview_fps) or not .1 <= preview_fps <= 2):
            raise ValueError('HTTP preview FPS must be 0.1..2')
        self.interval = 1.0 / preview_fps
        self.frames, self.sent, self.rates = {}, {}, {key: deque(maxlen=16) for key in self.keys}
        self.stopping = threading.Event()
        self.thread = self.loop = self.task = None
        self.lock = threading.Lock()
        self.hops = {key: deque(maxlen=120) for key in self.keys}
        self.hops_logged_at = self.clock()

    def _record_hop(self, key, duration, source_age, outcome):
        """Bounded passive timings; never include images, headers or credentials."""
        self.hops[key].append((max(0.0, duration) * 1000, source_age, outcome))
        now = self.clock()
        if now - self.hops_logged_at < DIAGNOSTIC_SECONDS:
            return
        report = {}
        for camera_key, samples in self.hops.items():
            if not samples:
                continue
            durations = sorted(item[0] for item in samples)
            ages = sorted(item[1] * 1000 for item in samples if item[1] is not None)
            outcomes = {}
            for _, _, state in samples:
                outcomes[state] = outcomes.get(state, 0) + 1
            report[camera_key] = {
                'samples': len(samples), 'outcomes': outcomes,
                'fetchP95Ms': round(durations[math.ceil(.95 * len(durations)) - 1], 1),
                'fetchMaxMs': round(durations[-1], 1),
                'sourceAgeP95Ms': round(ages[math.ceil(.95 * len(ages)) - 1], 1) if ages else None,
            }
            samples.clear()
        LOG.info('Camera fetch diagnostics %s', json.dumps(report, sort_keys=True))
        self.hops_logged_at = now

    def _metadata(self, key):
        camera, label = self.sources[key]
        frame = self.frames.get(key)
        value = dict(frame['metadata']) if frame else {
            'key': key, 'kind': 'image', 'label': label, 'frameSeq': 0,
            'topic': '/http_camera/' + camera, 'source': self.config.source_id,
            'message_timestamp': None, 'frameId': None, 'note': NOTE,
            'sourceHz': None, 'sourcePublisherCount': None,
        }
        value['ageS'] = None if frame is None else frame['source_age'] + max(0, self.clock() - frame['observed_at'])
        samples = self.rates[key]
        span = samples[-1] - samples[0] if len(samples) > 1 else 0
        value['hz'] = (len(samples) - 1) / span if span > 0 else None
        value['previewHz'] = value['hz']
        value['receivedFrames'] = value['frameSeq']
        return value

    def publish(self, key, status=None, reason=None, *, new_frame=False):
        value = self._metadata(key)
        if status is None:
            status = ('WAITING_UPSTREAM' if key not in self.frames else
                      'AVAILABLE' if value['ageS'] <= FRESH_SECONDS else 'STALE')
            if status == 'STALE':
                reason = '相机帧未更新'
        value['status'] = status
        if status != 'AVAILABLE':
            value.pop('data', None)
            value.pop('mime_type', None)
            # A historical measured preview rate is not a current rate.
            value['hz'] = value['previewHz'] = None
        if reason:
            value['reason'] = reason
        generation = self.uploader.connection_generation
        previous = self.sent.get(key)
        now = self.clock()
        if previous and not new_frame and previous[0] == status and previous[2] == generation:
            if status == 'AVAILABLE' or (now - previous[1] < METADATA_RESEND_SECONDS
                                          and previous[3] == reason):
                return
        self.uploader.enqueue('media', value['topic'], value, None, self.wall_clock())
        self.sent[key] = status, now, generation, reason

    def receive(self, key, raw, age, source_hash):
        """Only a new original JPEG hash advances observed-preview identity/time."""
        if key not in self.keys:
            raise ValueError('HTTP camera key is not enabled')
        if not isinstance(raw, bytes) or not 16 <= len(raw) <= MAX_JPEG_BYTES:
            raise PreviewError('Invalid JPEG input size')
        if not isinstance(source_hash, str) or not SOURCE_DIGEST.fullmatch(source_hash):
            raise PreviewError('Invalid source JPEG identity')
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
            raise PreviewError('Invalid camera freshness')
        if age > FRESH_SECONDS:
            self.publish(key, 'STALE', '上游相机数据已过期')
            return False
        previous = self.frames.get(key)
        encode_started = self.clock()
        encoded, width, height, preview_width, preview_height = preview_jpeg(raw)
        age += max(0, self.clock() - encode_started)
        if age > FRESH_SECONDS:
            self.publish(key, 'STALE', '上游相机数据已过期')
            return False
        if previous and previous['source_hash'] == source_hash:
            self.publish(key)
            return False
        value = self._metadata(key)
        value.update(data=base64.b64encode(encoded).decode('ascii'), mime_type='image/jpeg',
                     width=width, height=height, previewWidth=preview_width, previewHeight=preview_height,
                     frameSeq=1 if previous is None else previous['metadata']['frameSeq'] + 1,
                     received_timestamp=self.wall_clock())
        observed_at = self.clock()
        self.frames[key] = {'metadata': value, 'source_hash': source_hash, 'source_age': age, 'observed_at': observed_at}
        self.rates[key].append(observed_at)
        self.publish(key, new_frame=True)
        return True

    def transient_failure(self, key):
        """A transport failure cannot invalidate or refresh a still-fresh frame."""
        if key in self.frames:
            if self._metadata(key)['ageS'] <= FRESH_SECONDS:
                # Preserve the last published state: AVAILABLE stays available;
                # a prior explicit ERROR/STALE is never revived by a retry error.
                return
            self.publish(key, 'STALE', '相机预览连接中断')
        else:
            self.publish(key, 'WAITING_UPSTREAM', '相机预览连接中断')

    async def _camera(self, session, key):
        camera = self.sources[key][0]
        requested_at = self.clock()
        source_age = None
        outcome = 'transport_error'
        try:
            async with session.get(HTTP_CAMERA_URL + '/stream/' + camera, allow_redirects=False,
                                   headers={'Accept': 'multipart/x-mixed-replace'}) as response:
                if response.status in (502, 503, 504):
                    self.transient_failure(key)
                    return
                if response.status != 200:
                    raise PreviewError('Camera stream HTTP rejected')
                source_hash, age = source_metadata(response.headers)
                source_age = age
                raw = await read_mjpeg_frame(response, finite=True)
            # Add the whole single GET latency as a conservative upper bound;
            # this never claims that the relay's relative age is a UTC timestamp.
            age += max(0, self.clock() - requested_at)
            if age > FRESH_SECONDS:
                outcome = 'stale'
                self.publish(key, 'STALE', '上游相机数据已过期')
                return
            outcome = 'new_frame' if self.receive(key, raw, age, source_hash) else 'unchanged_or_stale'
        except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            self.transient_failure(key)
        except (PreviewError, ValueError, TypeError, cv2.error):
            outcome = 'source_error'
            self.publish(key, 'ERROR', '上游相机预览异常')
        finally:
            self._record_hop(key, self.clock() - requested_at, source_age, outcome)

    async def poll(self, session):
        """One isolated test/diagnostic sweep; continuous operation uses independent loops."""
        await asyncio.gather(*(self._camera(session, key) for key in self.keys))

    async def _camera_loop(self, session, key, phase):
        await asyncio.sleep(phase)
        while not self.stopping.is_set():
            started = self.clock()
            await self._camera(session, key)
            await asyncio.sleep(max(.05, self.interval - (self.clock() - started)))

    async def _run(self):
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=0.5, sock_read=1.0)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False,
                                         connector=aiohttp.TCPConnector(limit=max(1, len(self.keys))),
                                         auto_decompress=False, read_bufsize=16_384,
                                         max_line_size=MAX_HEADER_BYTES, max_field_size=MAX_HEADER_BYTES) as session:
            tasks = [asyncio.create_task(self._camera_loop(session, key, self.interval * index / len(self.keys)))
                     for index, key in enumerate(self.keys)]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def start(self):
        def worker():
            async def run():
                with self.lock:
                    self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
                try:
                    await self._run()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    LOG.error('HTTP camera preview stopped: %s', type(exc).__name__)
                finally:
                    for key in self.keys:
                        self.publish(key, 'STALE' if key in self.frames else 'WAITING_UPSTREAM', '相机预览采集已停止')
            asyncio.run(run())
        with self.lock:
            if self.thread is not None or self.stopping.is_set() or not self.keys:
                return
            self.thread = threading.Thread(target=worker, name='collector-http-camera', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.lock:
            loop, task, thread = self.loop, self.task, self.thread
        if loop and not loop.is_closed() and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if thread:
            thread.join(timeout=3)
