"""Durable, garage-gated native ROS bags; raw media never enters the web encoder.

SQLite here is the ROS bag *file format*. Application records are still created
through DBProg's PostgreSQL-backed upload API, never a second application DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Exact integer representation across the warehouse JSON/JavaScript protocol.
# Individual messages/segments keep their separate bounded resource policies.
MAX_FILE_BYTES = (1 << 53) - 1
_BOOT_ID_PATTERN = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')


def _linux_boot_clock_domain(path='/proc/sys/kernel/random/boot_id'):
    """Read one boot identifier, shared by ROS and USB on this host only."""
    try:
        with Path(path).open('r', encoding='ascii') as stream:
            raw = stream.read(80)
        boot_id = raw.strip()
        if (len(raw) < 80 and _BOOT_ID_PATTERN.fullmatch(boot_id)
                and boot_id.replace('-', '') != '0' * 32):
            return 'linux-boot:' + boot_id
    except (OSError, UnicodeError):
        pass
    return None


def _ros_clock_metadata(session):
    """Do not apply the current boot's clock to older source messages."""
    value = {}
    if session.get('clockSource') == 'CLOCK_REALTIME':
        value['clockSource'] = 'CLOCK_REALTIME'
    if session.get('timestampSemantics') == 'host_subscription_receive':
        value['timestampSemantics'] = 'host_subscription_receive'
    domain = session.get('clockDomain')
    if isinstance(domain, str) and domain.startswith('linux-boot:'):
        boot_id = domain[len('linux-boot:'):]
        if (_BOOT_ID_PATTERN.fullmatch(boot_id)
                and boot_id.replace('-', '') != '0' * 32):
            value['clockDomain'] = domain
    return value


def valid_file_size(value):
    return type(value) is int and 0 < value <= MAX_FILE_BYTES


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as out:
            os.chmod(temporary, 0o600)
            json.dump(value, out, ensure_ascii=False, allow_nan=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def spool_size(root):
    size = 0
    for path in Path(root).rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():
                size += path.stat().st_size
        except FileNotFoundError:
            # Finalized segments can move or be pruned by the upload worker.
            continue
    return size


class GarageGate:
    """Explicit site declaration: a map named garage alone proves no location.

    The declaration can span boots while the vehicle stays in that site. It
    must be cleared when leaving. Missing/malformed files always fail closed.
    """
    def __init__(self, path):
        self.path = Path(path)

    def _read(self, path):
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                return {}
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _enabled(value):
        try:
            expiry = value.get('expiresAt')
            return (value.get('schemaVersion') == 1 and value.get('scope') == 'garage'
                    and value.get('enabled') is True
                    and (expiry is None or (math.isfinite(float(expiry)) and float(expiry) > time.time())))
        except (ValueError, TypeError):
            return False

    def snapshot(self):
        value = self._read(self.path)
        if self._enabled(value):
            return value
        # Desktop sessions never overwrite the web/user choice. Their separate
        # lease fails closed if the terminal dies or its heartbeat stops.
        if self.path.name == 'garage.json':
            lease = self._read(self.path.with_name('desktop-lease.json'))
            expiry = lease.get('expiresAt')
            if (self._enabled(lease) and type(expiry) in (int, float)
                    and expiry <= time.time() + 60
                    and isinstance(lease.get('desktopSession'), str)
                    and re.fullmatch(r'[a-f0-9]{32}', lease['desktopSession'])):
                return lease
        return {**value, 'enabled': False}

    def enabled(self):
        return self._enabled(self.snapshot())

    def set(self, enabled, *, expires_at=None):
        atomic_json(self.path, dict(schemaVersion=1, scope='garage', enabled=bool(enabled),
                                   updatedAt=datetime.now(timezone.utc).isoformat(), expiresAt=expires_at))


class RosBagWriter:
    def __init__(self, path):
        import rosbag2_py
        self.module = rosbag2_py
        self.writer = rosbag2_py.SequentialWriter()
        options = rosbag2_py.StorageOptions(uri=str(path), storage_id='sqlite3',
                                           max_bagfile_size=256 * 1024 * 1024,
                                           storage_preset_profile='resilient')
        self.writer.open(options, rosbag2_py.ConverterOptions('', ''))
        self.topics = {}

    def write(self, topic, type_name, data, timestamp_ns):
        if topic in self.topics and self.topics[topic] != type_name:
            raise ValueError('topic type changed within a segment')
        if topic not in self.topics:
            self.writer.create_topic(self.module.TopicMetadata(name=topic, type=type_name,
                                                               serialization_format='cdr'))
            self.topics[topic] = type_name
        self.writer.write(topic, data, timestamp_ns)

    def close(self):
        self.writer.close()


class ArchiveStore:
    def __init__(self, root, robot_id, *, gate=None, writer_factory=RosBagWriter,
                 segment_seconds=3600, segment_bytes=128 * 1024 * 1024,
                 camera_segment_bytes=512 * 1024 * 1024,
                 min_free_bytes=10 * 1024**3, max_spool_bytes=20 * 1024**3,
                 idle_seconds=15, clock=time.time, disk_usage=shutil.disk_usage,
                 video_format='mkv', compression=True, depth_video_mode='lossless', video_fps=0,
                 background_resources=False, resource_probe_interval=2.0,
                 resource_spool_interval=10.0, resource_probe_max_age=15.0,
                 resource_isolate_inventory=False):
        if not 1 <= segment_seconds <= 3600 or not 1 <= segment_bytes <= 512 * 1024**2:
            raise ValueError('archive segment limit invalid')
        if not 1 <= camera_segment_bytes <= 512 * 1024**2:
            raise ValueError('camera segment limit invalid')
        if video_format not in ('mkv', 'rosbag'):
            raise ValueError('archive video format invalid')
        if type(compression) is not bool:
            raise ValueError('archive compression switch invalid')
        if depth_video_mode not in ('lossless', 'pseudocolor'):
            raise ValueError('invalid depth video mode')
        if depth_video_mode == 'pseudocolor' and not compression:
            raise ValueError('pseudocolor depth video requires compression')
        self.depth_video_mode = depth_video_mode
        if type(video_fps) not in (int, float) or not math.isfinite(video_fps) or not 0 <= video_fps <= 120:
            raise ValueError('invalid video FPS')
        self.video_fps = float(video_fps)
        self.capture_session_id = None
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.robot_id, self.gate = robot_id, gate
        self.video_format = video_format
        self.compression = compression
        self.clock_domain = _linux_boot_clock_domain()
        self.writer_factory, self.clock, self.disk_usage = writer_factory, clock, disk_usage
        self.segment_seconds, self.segment_bytes = segment_seconds, segment_bytes
        self.camera_segment_bytes = camera_segment_bytes
        self.min_free_bytes, self.max_spool_bytes, self.idle_seconds = min_free_bytes, max_spool_bytes, idle_seconds
        self.lock = threading.RLock()
        self.active, self.errors = {}, {}
        self.closed, self.finalized, self.written_bytes = False, 0, 0
        self._usage_checked, self._spool_size = float('-inf'), 0
        self._unscanned_bytes = 0
        if type(background_resources) is not bool:
            raise ValueError('background_resources must be a boolean')
        if type(resource_isolate_inventory) is not bool:
            raise ValueError('resource_isolate_inventory must be a boolean')
        self.resource_monitor = None
        self._recover()
        if background_resources:
            from .archive_resources import StorageMonitor
            self.resource_monitor = StorageMonitor(self.root, disk_usage=disk_usage,
                interval=resource_probe_interval, spool_interval=resource_spool_interval,
                max_age=resource_probe_max_age, isolate_inventory=resource_isolate_inventory)
            self.resource_monitor.start()

    def _recover(self):
        """Validate and reindex raw bags after power loss; quarantine bad remnants."""
        active = self.root / '.active'
        if not active.exists():
            self._report_recovery_remnants()
            return
        for entry in active.iterdir():
            if not entry.is_dir() or entry.is_symlink():
                continue
            try:
                if self._recover_bag(entry):
                    continue
            except Exception:
                pass
            target = self.root / 'recovery' / entry.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists():
                target = target.with_name(target.name + '-' + uuid4().hex[:8])
            entry.rename(target)
            atomic_json(target / 'recovery-required.json', dict(state='interrupted',
                reason='上次录制未正常收尾，原文件已保留；须校验并 reindex 后再入仓',
                detectedAt=datetime.now(timezone.utc).isoformat()))
        self._report_recovery_remnants()

    def _report_recovery_remnants(self):
        recovery = self.root / 'recovery'
        try:
            # Inspect the actual directory without following a replacement link.
            # A retained but empty directory is not an interrupted recording.
            descriptor = os.open(recovery, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        except OSError:
            pending = True  # An unreadable or abnormal path is not verified empty.
        else:
            try:
                with os.scandir(descriptor) as entries:
                    pending = next(entries, None) is not None
            except OSError:
                pending = True
            finally:
                os.close(descriptor)
        if pending:
            self.errors['recovery'] = '存在意外断电片段，原件已保留在 recovery，尚未标记为成功'

    def _recover_bag(self, entry):
        session = json.loads((entry / 'session.json').read_text())
        if session.get('scope') != 'garage' or session.get('source') != 'native-ros-cdr':
            return False
        bags = sorted((entry / 'bag').glob('*.db3'))
        if not bags:
            return False
        topics, count, timestamps, size = {}, 0, [], 0
        for bag in bags:
            if bag.is_symlink() or not valid_file_size(bag.stat().st_size):
                return False
            # Opening read-write enables SQLite's committed WAL recovery. This
            # is a native bag file, not the warehouse application database.
            connection = sqlite3.connect(str(bag))
            try:
                if connection.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    return False
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                for name, kind, messages, first, last, byte_count in connection.execute(
                    'SELECT t.name,t.type,COUNT(m.id),MIN(m.timestamp),MAX(m.timestamp),'
                    'SUM(LENGTH(m.data)) FROM topics t JOIN messages m ON m.topic_id=t.id GROUP BY t.id'):
                    if not messages:
                        continue
                    topics[name] = dict(type=kind, count=topics.get(name, {}).get('count', 0) + messages)
                    count += messages
                    size += byte_count
                    timestamps.extend((first / 1e9, last / 1e9))
            finally:
                connection.close()
        if not count:
            return False
        import rosbag2_py
        rosbag2_py.Reindexer().reindex(rosbag2_py.StorageOptions(uri=str(entry / 'bag'), storage_id='sqlite3'))
        first, last = min(timestamps), max(timestamps)
        start, end = datetime.fromtimestamp(first, SHANGHAI), datetime.fromtimestamp(last, SHANGHAI)
        sensor = session['sensor']
        if not isinstance(sensor, str) or sensor in ('.', '..') or re.search(r'[/\\\x00-\x1f]', sensor):
            return False
        label = f'{sensor}_{start:%H:%M}-{end:%H:%M}'
        day = start.strftime('%Y-%m-%d')
        files = [str(p.relative_to(entry)) for p in sorted((entry / 'bag').rglob('*'))
                 if p.is_file() and not p.name.endswith(('-wal', '-shm'))]
        profile = {'archiveProfile': 'compact-v1'} if session.get('archiveProfile') == 'compact-v1' else {}
        token = session.get('captureSessionId')
        if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token):
            profile['captureSessionId'] = token
        if profile and session.get('videoArchivePreset') in ('720p-economy-v1', '720p-balanced-v1'):
            profile['videoArchivePreset'] = session['videoArchivePreset']
        if sensor == 'Berxel深度' and 'depthVideoMode' in session:
            if session['depthVideoMode'] not in ('lossless', 'pseudocolor'):
                return False
            profile['depthVideoMode'] = session['depthVideoMode']
        self._publish_capture_manifest(entry, dict(schemaVersion=1, id=entry.name, day=day,
            sensor=sensor, label=label, startedAt=self._iso(first), endAt=self._iso(last),
            robotId=session['robotId'], metadata=dict(scope='garage', source='native-ros-cdr',
                timezone='Asia/Shanghai', format='rosbag2/sqlite3', closeReason='power_loss_recovered',
                captureInterrupted=True, receivedMessages=count, rawBytes=size, topics=topics,
                **profile, **_ros_clock_metadata(session)), files=files))
        target = self.root / 'days' / day / sensor / label / entry.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry.rename(target)
        self.finalized += 1
        return True

    def _publish_capture_manifest(self, path, manifest):
        """Only sealed, explicitly new captures receive compact derivatives."""
        metadata = manifest['metadata']
        topics = metadata['topics']
        images = {name: row for name, row in topics.items()
                  if row['type'] == 'sensor_msgs/msg/Image' and row['count'] > 0}
        extra_topics = any(row['type'] not in ('sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo')
                           and row['count'] > 0 for row in topics.values())
        if self.video_format == 'mkv' and images and not extra_topics:
            metadata.update(videoRequired=True, format='matroska/ffv1',
                            receivedFrames=sum(row['count'] for row in images.values()),
                            videoImageTopics=sorted(images))
            atomic_json(path / 'video-pending.json', manifest)
        else:
            # A session can start with CameraInfo before an image arrives. The
            # RGB preset is meaningful only for a published image/video capture.
            metadata.pop('videoArchivePreset', None)
            if self.video_format == 'mkv' and images and extra_topics:
                # Old interrupted depth bags can also contain PointCloud2.
                # Preserve those historical raw messages rather than hide or
                # discard them while publishing a video-only replacement.
                metadata['videoDeferredReason'] = 'mixed_non_image_topics'
            elif self.video_format == 'mkv' and not images and not extra_topics and any(
                    row['type'] == 'sensor_msgs/msg/CameraInfo' for row in topics.values()):
                metadata['cameraMetadataOnly'] = True
            if (metadata.get('archiveProfile') == 'compact-v1'
                    and (not images or (images and extra_topics
                                        and metadata.get('captureInterrupted') is not True))):
                metadata['bagCompressionRequired'] = True
                atomic_json(path / 'bag-pending.json', manifest)
            else:
                atomic_json(path / 'archive.json', manifest)

    def _capacity(self, now, incoming_bytes=0):
        if self.resource_monitor is not None:
            resources = self.resource_monitor.snapshot(incoming_bytes)
            if not resources['ready']:
                self.errors['storage'] = '存储资源探针未就绪、过期或失败，已暂停采集：' + resources['state']
                return False
            if resources['freeBytes'] <= self.min_free_bytes:
                self.errors['storage'] = '磁盘剩余空间低于预留值，已暂停采集，未删除未入仓原件'
                return False
            if resources['spoolBytes'] > self.max_spool_bytes or resources['spoolBytes']-incoming_bytes >= self.max_spool_bytes:
                self.errors['storage'] = '本地缓存达到上限，已暂停采集，等待入仓或释放已确认的副本'
                return False
            self.errors.pop('storage', None)
            return True
        free = self.disk_usage(self.root).free
        if free <= self.min_free_bytes:
            self.errors['storage'] = '磁盘剩余空间低于预留值，已暂停采集，未删除未入仓原件'
            return False
        if now - self._usage_checked >= 10 or now < self._usage_checked:
            self._spool_size = spool_size(self.root)
            self._usage_checked = now
            self._unscanned_bytes = 0
        if self._spool_size + self._unscanned_bytes >= self.max_spool_bytes:
            self.errors['storage'] = '本地缓存达到上限，已暂停采集，等待入仓或释放已确认的副本'
            return False
        self.errors.pop('storage', None)
        return True

    def _new(self, sensor, now):
        if (not isinstance(sensor, str) or not sensor or sensor in ('.', '..')
                or re.search(r'[/\\\x00-\x1f\x7f]', sensor) or len(sensor.encode()) > 80):
            raise ValueError('invalid sensor name')
        segment_id = uuid4().hex
        path = self.root / '.active' / segment_id
        path.mkdir(parents=True, mode=0o700)
        value = dict(id=segment_id, path=path, sensor=sensor, start=now, end=now,
                     day=datetime.fromtimestamp(now, SHANGHAI).strftime('%Y-%m-%d'),
                     bytes=0, messages=0, topics={}, writer=None)
        profile = {'archiveProfile': 'compact-v1', 'videoArchivePreset': '720p-balanced-v1'} if self.compression else {}
        snapshot = self.gate.snapshot() if self.gate is not None and hasattr(self.gate, 'snapshot') else {}
        token = snapshot.get('desktopSession')
        if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token):
            self.capture_session_id = token
        elif snapshot.get('enabled') is True:
            self.capture_session_id = None
        if self.capture_session_id:
            profile['captureSessionId'] = self.capture_session_id
        if sensor == 'Berxel深度':
            profile['depthVideoMode'] = self.depth_video_mode
        profile.update(clockSource='CLOCK_REALTIME', timestampSemantics='host_subscription_receive')
        if self.clock_domain is not None:
            profile['clockDomain'] = self.clock_domain
        value.update(profile)
        atomic_json(path / 'session.json', dict(schemaVersion=1, robotId=self.robot_id,
                    sensor=sensor, startedAt=self._iso(now), scope='garage', source='native-ros-cdr',
                    **profile))
        try:
            value['writer'] = self.writer_factory(path / 'bag')
        except Exception:
            self.errors[sensor] = '原生录制器启动失败，未生成成功片段'
            raise
        self.active[sensor] = value
        return value

    @staticmethod
    def _iso(timestamp):
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def record(self, sensor, topic, type_name, data, timestamp_ns, wall_time=None):
        return self._record(sensor, topic, type_name, data, timestamp_ns, wall_time, admitted=False)

    def record_accepted(self, sensor, topic, type_name, data, timestamp_ns, wall_time=None):
        """Persist a packet admitted by the bounded ROS tap while capture was on.

        Closing capture stops admission, then drains that tap. Re-reading the
        gate here would discard its final accepted packets. Storage limits and
        the closed-store guard still apply during draining.
        """
        return self._record(sensor, topic, type_name, data, timestamp_ns, wall_time, admitted=True)

    def _record(self, sensor, topic, type_name, data, timestamp_ns, wall_time, *, admitted):
        with self.lock:
            now = self.clock() if wall_time is None else wall_time
            if self.closed or (not admitted and self.gate is not None and not self.gate.enabled()):
                return False
            if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MESSAGE_BYTES:
                raise ValueError('raw message must be bounded bytes')
            if not self._capacity(now, len(data)):
                self._finish_all('storage_paused')
                return False
            if self.resource_monitor is not None:
                self.resource_monitor.begin_write(len(data))
            try:
                day = datetime.fromtimestamp(now, SHANGHAI).strftime('%Y-%m-%d')
                value = self.active.get(sensor)
                camera_types = ('sensor_msgs/msg/Image', 'sensor_msgs/msg/CameraInfo')
                existing_topics = value['topics'].values() if value else ()
                image_segment = (self.video_format == 'mkv' and type_name in camera_types
                    and (type_name == 'sensor_msgs/msg/Image'
                         or any(row['type'] == 'sensor_msgs/msg/Image' for row in existing_topics))
                    and all(row['type'] in camera_types for row in existing_topics))
                byte_limit = self.camera_segment_bytes if image_segment else self.segment_bytes
                if value and (value['day'] != day or now - value['start'] >= self.segment_seconds
                              or now < value['start'] or value['bytes'] + len(data) > byte_limit):
                    self._finish(sensor, 'rotation')
                    value = None
                if value is None:
                    value = self._new(sensor, now)
                try:
                    value['writer'].write(topic, type_name, data, timestamp_ns)
                except Exception:
                    self.errors[sensor] = '原生数据写入失败，当前片段待校验'
                    self._quarantine(sensor)
                    raise
                value['end'] = now
                value['bytes'] += len(data)
                value['messages'] += 1
                value['topics'][topic] = dict(type=type_name,
                    count=value['topics'].get(topic, {}).get('count', 0) + 1)
                self.written_bytes += len(data)
                self._unscanned_bytes += len(data)
                return True
            finally:
                if self.resource_monitor is not None:
                    self.resource_monitor.finish_write(len(data))

    def _quarantine(self, sensor):
        value = self.active.pop(sensor)
        try:
            value['writer'].close()
        except Exception:
            pass
        target = self.root / 'recovery' / value['id']
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        value['path'].rename(target)
        atomic_json(target / 'recovery-required.json', dict(state='write_failed', sensor=sensor))

    def _finish(self, sensor, reason):
        value = self.active[sensor]
        try:
            value['writer'].close()
            start = datetime.fromtimestamp(value['start'], SHANGHAI)
            end = datetime.fromtimestamp(value['end'], SHANGHAI)
            label = f'{sensor}_{start:%H:%M}-{end:%H:%M}'
            files = [p for p in sorted((value['path'] / 'bag').rglob('*')) if p.is_file()]
            if not files or any(not valid_file_size(p.stat().st_size) or p.is_symlink() for p in files):
                raise ValueError('empty or oversized native bag')
            # Content digests and server acknowledgements are persisted by the
            # upload worker; do not hash a large bag inside the ROS writer lock.
            manifest = dict(schemaVersion=1, id=value['id'], day=value['day'], sensor=sensor,
                label=label, startedAt=self._iso(value['start']), endAt=self._iso(value['end']),
                robotId=self.robot_id, metadata=dict(scope='garage', source='native-ros-cdr',
                    timezone='Asia/Shanghai', format='rosbag2/sqlite3',
                    rosDistribution='humble', closeReason=reason, rawBytes=value['bytes'],
                    receivedMessages=value['messages'], topics=value['topics'],
                    **({'captureSessionId': value['captureSessionId']} if value.get('captureSessionId') else {}),
                    **({'videoTargetFps': self.video_fps} if self.video_fps and any(
                        row['type'] == 'sensor_msgs/msg/Image' for row in value['topics'].values()) else {}),
                    timestampBasis='receive-wall-clock; original ROS header retained in CDR',
                    **_ros_clock_metadata(value),
                    **({'archiveProfile': 'compact-v1'} if self.compression else {}),
                    **({'videoArchivePreset': value['videoArchivePreset']}
                       if value.get('videoArchivePreset') in ('720p-economy-v1', '720p-balanced-v1') else {}),
                    **({'depthVideoMode': value['depthVideoMode']}
                       if value.get('depthVideoMode') in ('lossless', 'pseudocolor') else {})),
                files=[str(p.relative_to(value['path'])) for p in files])
            self._publish_capture_manifest(value['path'], manifest)
            destination = self.root / 'days' / value['day'] / sensor / label / value['id']
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            value['path'].rename(destination)
            self.active.pop(sensor)
            self.finalized += 1
        except Exception:
            self.errors[sensor] = '录制片段收尾失败，原件保留待恢复'
            if sensor in self.active:
                self._quarantine(sensor)
            raise

    def _finish_all(self, reason):
        for sensor in list(self.active):
            try:
                self._finish(sensor, reason)
            except Exception:
                pass

    def tick(self):
        with self.lock:
            now = self.clock()
            if self.gate is not None and not self.gate.enabled():
                self._finish_all('garage_disabled')
                return
            self._capacity(now)
            for sensor, value in list(self.active.items()):
                if now - value['end'] >= self.idle_seconds or now - value['start'] >= self.segment_seconds:
                    try:
                        self._finish(sensor, 'idle' if now - value['end'] >= self.idle_seconds else 'rotation')
                    except Exception:
                        pass

    def snapshot(self):
        with self.lock:
            resources = self.resource_monitor.snapshot() if self.resource_monitor is not None else None
            return dict(enabled=self.gate.enabled() if self.gate else True,
                finalizedSegments=self.finalized, writtenBytes=self.written_bytes,
                freeBytes=resources['freeBytes'] if resources is not None else self.disk_usage(self.root).free,
                **({'resourceMonitor': resources} if resources is not None else {}), errors=dict(self.errors),
                active={key: dict(startedAt=self._iso(value['start']), messages=value['messages'],
                                  bytes=value['bytes'], topics=value['topics'])
                        for key, value in self.active.items()})

    def close(self):
        with self.lock:
            self.closed = True
            self._finish_all('shutdown')
        # The probe never takes the writer lock. Join outside it so a blocked
        # filesystem cannot lock up recorder state or hide unfinished cleanup.
        if self.resource_monitor is not None:
            try:
                stopped = self.resource_monitor.close()
            except Exception as error:
                with self.lock:
                    self.errors['resourceMonitor'] = 'ResourceMonitorClose:' + type(error).__name__
                raise
            with self.lock:
                if stopped:
                    self.errors.pop('resourceMonitor', None)
                else:
                    self.errors['resourceMonitor'] = 'ResourceMonitorShutdownTimeout'
