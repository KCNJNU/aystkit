"""Fresh, bounded image/map/cloud previews from received ROS messages only."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import time

import cv2
import numpy as np
from shared.protocol import decode_media

from .legacy.camera_codec import image_to_jpeg
from .legacy.point_codec import cloud_points

CAMERAS = {'fisheye': '鱼眼相机', 'color': '深度相机 · 彩色', 'depth': '深度相机 · 深度伪彩'}
SCANS = {'bluesea': '蓝海二维雷达', 'scan': '二维雷达扫描'}
CLOUDS = {'lidar': 'MID360 雷达点云', 'registered': 'FAST-LIO 配准点云', 'depthPoints': '深度相机点云', **SCANS}
MAPS = {'map': '静态地图', 'localCostmap': '局部代价地图', 'globalCostmap': '全局代价地图'}
LABELS = {**CAMERAS, **CLOUDS, **MAPS}
MAX_SCAN_RAYS = 100_000
METADATA_RESEND_SECONDS = 30.0


def laser_scan_points(message, limit: int) -> list[list[float]]:
    """Bounded sensor-frame XY projection, never a TF transform or 3-D measurement."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4000:
        raise ValueError('LaserScan point limit must be in 1..4000')
    count = len(message.ranges)
    if count > MAX_SCAN_RAYS:
        raise ValueError('LaserScan ray count exceeds limit')
    values = [message.angle_min, message.angle_increment, message.range_min, message.range_max]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in values):
        raise ValueError('LaserScan angle and range metadata must be finite numbers')
    angle_min, increment, minimum, maximum = values
    if increment == 0 or abs(increment) > math.tau:
        raise ValueError('Invalid LaserScan angle increment')
    # Align the distance bound with the shared XYZ protocol coordinate bound.
    if not 0 <= minimum < maximum <= 1_000_000:
        raise ValueError('Invalid LaserScan distance bounds')
    final_angle = angle_min + max(0, count - 1) * increment
    if not math.isfinite(final_angle):
        raise ValueError('LaserScan final angle is not finite')
    ranges = np.asarray(message.ranges, dtype=np.float64)
    if ranges.ndim != 1 or ranges.size != count:
        raise ValueError('Invalid LaserScan range array')
    valid = np.flatnonzero(np.isfinite(ranges) & (ranges > 0) & (ranges >= minimum) & (ranges <= maximum))
    # Filter before decimation, so sparse valid rays are not lost between stride slots.
    stride = max(1, math.ceil(len(valid) / limit))
    indices = valid[::stride]
    distances = ranges[indices]
    angles = angle_min + indices * increment
    return np.column_stack((distances * np.cos(angles), distances * np.sin(angles),
                            np.zeros(len(indices)))).tolist()


def header_timestamp(message) -> float | None:
    header = getattr(message, 'header', None)
    stamp = getattr(header, 'stamp', None)
    if stamp is None:
        return None
    seconds, nanoseconds = getattr(stamp, 'sec', 0), getattr(stamp, 'nanosec', 0)
    if isinstance(seconds, bool) or isinstance(nanoseconds, bool):
        return None
    value = float(seconds) + float(nanoseconds) / 1_000_000_000
    # Epoch time only; a zero or simulation-relative stamp is not a UTC sample time.
    return value if math.isfinite(value) and 946_684_800 <= value <= time.time() + 300 else None


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not (4 <= len(data) <= 500_000 and data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9')):
        raise ValueError('Invalid or oversized JPEG')
    offset = 2
    while offset + 3 < len(data):
        if data[offset] != 0xff:
            raise ValueError('Invalid JPEG marker')
        while offset < len(data) and data[offset] == 0xff:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack('>H', data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data):
            raise ValueError('Invalid JPEG segment')
        if marker in (0xc0, 0xc1, 0xc2):
            if length < 8:
                raise ValueError('Invalid JPEG dimensions')
            height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
            if not 0 < width <= 4096 or not 0 < height <= 4096 or width * height > 8_000_000:
                raise ValueError('JPEG dimensions out of bounds')
            return width, height
        offset += length
    raise ValueError('JPEG dimensions not found')


def grid_png(message) -> bytes:
    """Adapted independently from audited Observer visuals.py:grid_png."""
    width, height = message.info.width, message.info.height
    if not 0 < width * height <= 16_000_000 or len(message.data) != width * height:
        raise ValueError('Invalid grid dimensions')
    cells = np.asarray(message.data, dtype=np.int16).reshape(height, width)
    pixels = np.full((height, width), 110, dtype=np.uint8)
    valid = (cells >= 0) & (cells <= 100)
    pixels[valid] = (245 - cells[valid] * 2.25).astype(np.uint8)
    pixels = np.flipud(pixels)
    scale = min(1.0, 800 / max(width, height))
    if scale < 1:
        pixels = cv2.resize(pixels, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_NEAREST)
    ok, encoded = cv2.imencode('.png', pixels)
    if not ok or len(encoded) > 500_000:
        raise ValueError('Map encoding failed or exceeds upload limit')
    return encoded.tobytes()


class MediaStore:
    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock = config, clock
        # HTTP previews have a separate receiver and must not be described as ROS.
        self.keys = tuple(key for key in config.media_keys if key in LABELS)
        self.items, self.errors, self.last_encode = {}, {}, {}
        self.next_image_encode = {}
        self.dirty = set()
        self.map_sent = {}
        self.metadata_sent = {}
        self.connection_generation = -1
        self.bridge_times = {}
        self.frame_sequences = {key: 0 for key in self.keys}

    def receive(self, key, message, topic: str, *, compressed: bool = False, bridge_cloud: bool = False):
        if key not in self.keys:
            return
        now = self.clock()
        received_time = time.time()
        if compressed or bridge_cloud:
            self.bridge_times[key] = now
        elif now - self.bridge_times.get(key, -100) < 2:
            return  # A current compressed bridge is the single authority for this key.
        interval = 1 / (self.config.image_fps if key in CAMERAS else self.config.cloud_fps)
        if key in MAPS:
            interval = 1.0
        if key in CAMERAS:
            # Keep the sampling clock separate from the last accepted arrival.
            # Nearly equal source/preview clocks otherwise discard alternate
            # frames whenever a callback arrives a few microseconds early.
            due = self.next_image_encode.get(key, now)
            if now + 1e-6 < due:
                return
            skipped = max(1, math.floor((now - due + 1e-6) / interval) + 1)
            self.next_image_encode[key] = due + skipped * interval
        elif now - self.last_encode.get(key, -100) < interval:
            return
        self.last_encode[key] = now
        try:
            stamp = header_timestamp(message)
            frame = getattr(getattr(message, 'header', None), 'frame_id', None)
            metadata = {'key': key, 'label': LABELS[key], 'topic': topic, 'source': self.config.source_id,
                        'frameId': frame, 'message_timestamp': stamp, 'status': 'AVAILABLE'}
            if key in CAMERAS:
                if compressed:
                    if str(message.format).lower().split(';')[-1].strip() not in ('jpeg', 'jpg') and 'jpeg' not in str(message.format).lower():
                        raise ValueError('Only JPEG compressed preview is supported')
                    jpeg = bytes(message.data)
                    width, height = jpeg_dimensions(jpeg)
                    original_width, original_height = width, height
                    scale = min(1.0, 960 / width, 720 / height)
                    if scale < 1:
                        decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if decoded is None or decoded.shape[:2] != (height, width):
                            raise ValueError('Compressed JPEG cannot be decoded')
                        resized = cv2.resize(decoded, (max(1, round(width * scale)), max(1, round(height * scale))),
                                             interpolation=cv2.INTER_AREA)
                        ok, encoded = cv2.imencode('.jpg', resized, [cv2.IMWRITE_JPEG_QUALITY, 70])
                        if not ok or len(encoded) > 500_000:
                            raise ValueError('Compressed preview resize failed')
                        jpeg, width, height = encoded.tobytes(), resized.shape[1], resized.shape[0]
                    metadata.update(width=original_width, height=original_height)
                else:
                    jpeg, width, height = image_to_jpeg(message, depth=key == 'depth')
                    if len(jpeg) > 500_000:
                        raise ValueError('Encoded JPEG exceeds upload limit')
                    metadata.update(width=message.width, height=message.height, encoding=message.encoding)
                metadata.update(kind='image', data=base64.b64encode(jpeg).decode('ascii'), mime_type='image/jpeg',
                                previewWidth=width, previewHeight=height,
                                note='深度伪彩；黑色为无效深度；仅作观察，不作测距' if key == 'depth' else '限帧 JPEG 只读预览')
                # Check the same finite binary format and pixel bounds before claiming
                # AVAILABLE; an invalid small bridge JPEG must not fail only at upload.
                decode_media(metadata)
            elif key in CLOUDS:
                if key in SCANS:
                    points = laser_scan_points(message, self.config.cloud_points)
                    total = len(message.ranges)
                    metadata['note'] = '二维距离扫描 · 传感器本地 XY 平面 · Z=0 · 不融合 TF'
                elif bridge_cloud:
                    if len(message.data) > 500_000:
                        raise ValueError('Bridge cloud exceeds size bound')
                    decoded = json.loads(message.data)
                    points = decoded.get('points')
                    if not isinstance(points, list) or len(points) > 4000:
                        raise ValueError('Invalid bridge point cloud')
                    if not all(isinstance(point, list) and len(point) == 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in point) for point in points):
                        raise ValueError('Invalid bridge XYZ coordinate')
                    stride = max(1, math.ceil(len(points) / self.config.cloud_points))
                    points = points[::stride]
                    total = decoded.get('totalPoints')
                    metadata['frameId'] = decoded.get('frameId')
                    metadata['message_timestamp'] = None  # Bridge String contains no original ROS timestamp.
                else:
                    points = cloud_points(message, self.config.cloud_points)
                    total = message.width * message.height
                metadata.update(kind='cloud', points=points, totalPoints=total)
                if not points:
                    metadata.update(status='NO_DATA', reason='已收到扫描消息，但没有有效距离' if key in SCANS else '已收到点云消息，但有效采样坐标为空')
            else:
                png = grid_png(message)
                pose = message.info.origin
                origin = {'position': {'x': pose.position.x, 'y': pose.position.y, 'z': pose.position.z},
                          'orientation': {'x': pose.orientation.x, 'y': pose.orientation.y, 'z': pose.orientation.z, 'w': pose.orientation.w}}
                metadata.update(kind='map', data=base64.b64encode(png).decode('ascii'), mime_type='image/png',
                                width=message.info.width, height=message.info.height,
                                resolutionM=message.info.resolution, origin=origin)
                hashed = png + json.dumps({'origin': origin, 'resolution': message.info.resolution, 'frameId': frame}, sort_keys=True).encode()
                metadata['content_hash'] = hashlib.sha256(hashed).hexdigest()
            self.frame_sequences[key] += 1
            metadata['frameSeq'] = self.frame_sequences[key]
            metadata['received_timestamp'] = received_time
            self.items[key] = (metadata, now, received_time)
            self.errors.pop(key, None)
            self.dirty.add(key)
        except Exception as exc:
            self.errors[key] = type(exc).__name__ + ': ' + str(exc)[:200]
            self.dirty.add(key)

    def status(self, key, sensor_status: dict | None = None):
        item = self.items.get(key)
        now = self.clock()
        age = None if item is None else max(0, now - item[1])
        timeout = None if key == 'map' else 5 if key in MAPS else 2
        state = 'ERROR' if key in self.errors else 'WAITING_UPSTREAM' if item is None else 'STALE' if timeout is not None and age > timeout else item[0]['status']
        if key in MAPS and sensor_status and sensor_status.get('status') in ('STALE', 'ERROR', 'WAITING_UPSTREAM'):
            state = sensor_status['status']
        meta = dict(item[0] if item else {'key': key, 'label': LABELS[key], 'kind': 'image' if key in CAMERAS else 'map' if key in MAPS else 'cloud'})
        meta.update(status=state, ageS=age, hz=None if not sensor_status else sensor_status.get('hz'))
        if state not in ('AVAILABLE', 'NO_DATA'):
            for field in ('data', 'mime_type', 'points'):
                meta.pop(field, None)
        if key in self.errors:
            meta['reason'] = self.errors[key]
        return meta

    def publish(self, uploader, sensor_snapshots):
        force_maps = uploader.connection_generation != self.connection_generation
        self.connection_generation = uploader.connection_generation
        now = self.clock()
        for key in self.keys:
            meta = self.status(key, sensor_snapshots.get(key))
            item = self.items.get(key)
            if key in MAPS and meta['status'] == 'AVAILABLE':
                previous = self.map_sent.get(key)
                changed = not previous or previous[0] != meta['content_hash']
                if not (force_maps or changed or now - previous[1] >= self.config.map_resend_s):
                    self.dirty.discard(key)
                    continue
                self.map_sent[key] = (meta['content_hash'], now)
            elif key not in self.dirty and meta['status'] in ('AVAILABLE', 'NO_DATA'):
                continue
            elif meta['status'] not in ('AVAILABLE', 'NO_DATA'):
                previous = self.metadata_sent.get(key)
                # Age/rate updates do not change the meaning of an unavailable
                # stream. Send changes (including error reason) immediately,
                # otherwise avoid repeated POSTs competing with fresh cameras.
                signature = json.dumps({field: value for field, value in meta.items()
                                        if field not in ('ageS', 'hz')}, sort_keys=True)
                if (previous and previous[0] == signature
                        and now - previous[1] < METADATA_RESEND_SECONDS and not force_maps):
                    continue
                self.metadata_sent[key] = (signature, now)
            topic = meta.get('topic') or sensor_snapshots.get(key, {}).get('topic')
            if not topic:
                # Actual configured ROS source, even when no message is available yet.
                continue
            # Current metadata collection time is distinct from the cached frame's real
            # received_timestamp/header sampling stamp, especially during map resync.
            uploader.enqueue('media', topic, meta, meta.get('message_timestamp'), time.time())
            self.dirty.discard(key)

    def snapshots(self, sensors):
        cameras, visuals = {}, {}
        for key in self.keys:
            value = self.status(key, sensors.get(key))
            for field in ('data', 'mime_type', 'points'):
                value.pop(field, None)
            if key in CAMERAS:
                value['previewUrl'] = '/camera/' + key + '.jpg'
                value['receivedFrames'] = self.frame_sequences[key]
                cameras[key] = value
            else:
                visuals[key] = value
        return cameras, visuals
