"""Opt-in native JPEG archival tap. No camera devices, codecs, ROS or network.

submit() only retains bounded immutable bytes in memory. The writer receives
the exact JPEG supplied by the existing source, before any web resize/encode.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_JPEG_BYTES = 16 * 1024**2
MAX_CAMERAS = 16
MAX_INDEX_LINE_BYTES = 4096
MAX_CAMERA_CALIBRATION_BYTES = 16 * 1024
MAX_CAMERA_SESSION_BYTES = 32 * 1024
_BOOT_ID_PATTERN = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')


def _linux_boot_clock_domain(path='/proc/sys/kernel/random/boot_id'):
    """Identify one Linux boot; failure means an explicitly unknown domain."""
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


def _camera_clock_metadata(session):
    """Retain only the original source session's proven clock annotations."""
    value = {}
    if session.get('clockSource') == 'CLOCK_REALTIME':
        value['clockSource'] = 'CLOCK_REALTIME'
    if session.get('timestampSemantics') == 'host_read_complete':
        value['timestampSemantics'] = 'host_read_complete'
    domain = session.get('clockDomain')
    if isinstance(domain, str) and domain.startswith('linux-boot:'):
        boot_id = domain[len('linux-boot:'):]
        if (_BOOT_ID_PATTERN.fullmatch(boot_id)
                and boot_id.replace('-', '') != '0' * 32):
            value['clockDomain'] = domain
    return value


def _calibration_copy(value, camera_id):
    """Keep bounded JSON evidence, independently of mutable loader state."""
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('cameraId') != camera_id
            or not isinstance(value.get('state'), str)):
        raise ValueError('invalid camera calibration snapshot')
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    if len(encoded.encode('utf-8')) > MAX_CAMERA_CALIBRATION_BYTES:
        raise ValueError('oversized camera calibration snapshot')
    return json.loads(encoded)


def _calibration_mismatch(snapshot, dimensions):
    """A changing raw image size must never retain usable current-image K/D."""
    value = _calibration_copy(snapshot, snapshot['cameraId'])
    value.update(state='resolution_mismatch', size=dict(width=dimensions[0], height=dimensions[1]))
    value.pop('currentImageInfo', None)
    value['transform'] = dict(kind='none', reason='resolution_mismatch')
    return value


def _atomic_json(path, value):
    temporary = path.with_name(path.name+'.tmp-'+uuid4().hex)
    with temporary.open('x', encoding='utf-8') as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _name(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or value in ('.', '..') or len(value.encode()) > 80
            or re.search(r'[/\\\x00-\x1f\x7f]', value)):
        raise ValueError('invalid camera or sensor name')
    return value


def _jpeg_shape(raw):
    """Check bounded JPEG framing/headers without decoding or changing pixels."""
    if not 4 < len(raw) <= MAX_JPEG_BYTES or not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise ValueError('invalid JPEG framing')
    offset, shape = 2, None
    while offset < len(raw)-2:
        if raw[offset] != 0xff:
            raise ValueError('invalid JPEG marker')
        while offset < len(raw) and raw[offset] == 0xff:
            offset += 1
        if offset >= len(raw):
            raise ValueError('truncated JPEG marker')
        marker = raw[offset]
        offset += 1
        if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
            continue
        if offset+2 > len(raw):
            raise ValueError('truncated JPEG length')
        length = int.from_bytes(raw[offset:offset+2], 'big')
        if length < 2 or offset+length > len(raw)-2:
            raise ValueError('invalid JPEG segment length')
        if marker in (0xc0, 0xc1, 0xc2):
            if length < 11 or raw[offset+2] != 8:
                raise ValueError('invalid JPEG frame header')
            height = int.from_bytes(raw[offset+3:offset+5], 'big')
            width = int.from_bytes(raw[offset+5:offset+7], 'big')
            if not width or not height:
                raise ValueError('invalid JPEG dimensions')
            shape = width, height
        if marker == 0xda:
            if shape is None or offset+length >= len(raw)-2:
                raise ValueError('missing JPEG scan')
            return shape
        offset += length
    raise ValueError('missing JPEG scan')


def _directory(root, *parts, create=False):
    """Reject links at every recovery directory boundary, including days/."""
    path = root
    for part in parts:
        if (not isinstance(part, str) or part in ('', '.', '..')
                or re.search(r'[/\\\x00-\x1f\x7f]', part)):
            raise ValueError('unsafe recovery directory')
        path = path / part
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise ValueError('foreign recovery directory owner')
        finally:
            os.close(descriptor)
    return path


def _reader(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError('unsafe recovery file')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate recovery JSON field')
        result[key] = value
    return result


def _bounded_json(path, limit=MAX_INDEX_LINE_BYTES):
    with _reader(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('oversized recovery JSON')
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json)
    except RecursionError as error:
        raise ValueError('excessive recovery JSON nesting') from error
    if not isinstance(value, dict):
        raise ValueError('invalid recovery JSON object')
    return value


def _frame_prefix(frames, index, frames_out=None, index_out=None):
    """Validate/copy one frame at a time; never retain a segment in RAM."""
    original_frames, original_index = os.fstat(frames.fileno()), os.fstat(index.fileno())
    count = size = index_size = gaps = resets = 0
    first = last = previous_sequence = None
    dimensions = set()
    raw_digest, index_digest = hashlib.sha256(), hashlib.sha256()
    reason = None
    while True:
        line = index.readline(MAX_INDEX_LINE_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_INDEX_LINE_BYTES:
            reason = 'oversized_index_line'
            break
        if not line.endswith(b'\n'):
            reason = 'incomplete_index_line'
            break
        try:
            row = json.loads(line, object_pairs_hook=_unique_json)
            if not isinstance(row, dict):
                raise ValueError()
            for key in ('sequence', 'timestampNs', 'offset', 'length', 'width', 'height', 'gapBefore'):
                if type(row.get(key)) is not int:
                    raise ValueError()
            wall = row.get('wallTime')
            sampled = row.get('sampledOutBefore', 0)
            if (row['sequence'] < 0 or row['timestampNs'] <= 0 or row['offset'] != size
                    or not 4 < row['length'] <= MAX_JPEG_BYTES
                    or row['offset'] + row['length'] > original_frames.st_size
                    or row['width'] <= 0 or row['height'] <= 0 or row['gapBefore'] < 0
                    or type(sampled) is not int or sampled < 0
                    or type(row.get('sequenceReset')) is not bool
                    or type(wall) not in (int, float) or not math.isfinite(wall) or wall <= 0
                    or not isinstance(row.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'])):
                raise ValueError()
            day = datetime.fromtimestamp(wall, SHANGHAI).date()
            if first is not None and (wall < first or wall-first >= 3600
                    or day != datetime.fromtimestamp(first, SHANGHAI).date()):
                raise ValueError()
            if previous_sequence is not None and (row['sequence'] == previous_sequence
                    or row['gapBefore'] != max(0, row['sequence']-previous_sequence-1-sampled)
                    or row['sequenceReset'] != (row['sequence'] < previous_sequence)):
                raise ValueError()
        except (ValueError, TypeError, OverflowError, OSError, RecursionError):
            reason = 'invalid_index_row'
            break
        raw = frames.read(row['length'])
        if len(raw) != row['length']:
            reason = 'incomplete_frame'
            break
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            reason = 'frame_checksum_mismatch'
            break
        try:
            shape = _jpeg_shape(raw)
            if shape != (row['width'], row['height']):
                raise ValueError()
            if shape not in dimensions and len(dimensions) >= 64:
                raise ValueError()
        except ValueError:
            reason = 'invalid_jpeg_or_dimensions'
            break
        if frames_out is not None and frames_out.write(raw) != len(raw):
            raise OSError('short recovered frame write')
        if index_out is not None and index_out.write(line) != len(line):
            raise OSError('short recovered index write')
        raw_digest.update(raw)
        index_digest.update(line)
        # Drop this buffer before the next read: at most one <=16 MiB JPEG is
        # retained, plus bounded JSON lines and small file buffers (<32 MiB).
        del raw
        first = wall if first is None else first
        # record() permits a wall-clock correction inside the current segment.
        # Preserve source row times and use the maximum as the segment end.
        last, previous_sequence = wall if last is None else max(last, wall), row['sequence']
        count += 1
        size += row['length']
        index_size += len(line)
        gaps += row['gapBefore']
        resets += int(row['sequenceReset'])
        dimensions.add(shape)
    for stream, before in ((frames, original_frames), (index, original_index)):
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError('recovery source changed')
    tail = original_frames.st_size-size
    if reason is None and tail:
        reason = 'unindexed_frame_tail'
    return dict(count=count, size=size, indexSize=index_size, first=first, last=last,
        gaps=gaps, resets=resets, dimensions=sorted(dimensions), tail=tail,
        indexTail=original_index.st_size-index_size, reason=reason,
        framesSha256=raw_digest.hexdigest(), indexSha256=index_digest.hexdigest())


def _inspect_prefix(entry, frames_out=None, index_out=None):
    with _reader(entry/'frames.mjpg') as frames, _reader(entry/'index.jsonl') as index:
        return _frame_prefix(frames, index, frames_out, index_out)


class NativeCameraStore:
    """Durable source writer; MKV captures are published by a separate finalizer."""
    def __init__(self, root, robot_id, *, gate=None, camera_names=None, capacity=None,
                 segment_seconds=3600, segment_bytes=512*1024**2, idle_seconds=15,
                 min_free_bytes=10*1024**3, max_spool_bytes=20*1024**3,
                 clock=time.time, disk_usage=shutil.disk_usage, sync_seconds=1,
                 video_format='mkv', video_fps=0, background_resources=False,
                 resource_probe_interval=2.0, resource_spool_interval=10.0,
                 resource_probe_max_age=15.0, calibration_path=None):
        if not 1 <= segment_seconds <= 3600 or not 1 <= segment_bytes <= 512*1024**2:
            raise ValueError('invalid camera segment limits')
        if min_free_bytes < 0 or max_spool_bytes < 1 or sync_seconds < 0:
            raise ValueError('invalid camera storage limits')
        if video_format not in ('mkv', 'mjpeg'):
            raise ValueError('invalid camera archive video format')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.robot_id, self.gate, self.capacity = robot_id, gate, capacity
        self.video_format = video_format
        self.calibration_path = calibration_path
        self.clock_domain = _linux_boot_clock_domain()
        from .archive_sampling import FrameSampler
        self.video_fps = FrameSampler(video_fps).fps
        self.camera_names = None if camera_names is None else {_name(key): _name(value) for key, value in camera_names.items()}
        if self.camera_names is not None and not 1 <= len(self.camera_names) <= MAX_CAMERAS:
            raise ValueError('invalid camera identity count')
        self.segment_seconds, self.segment_bytes, self.idle_seconds = segment_seconds, segment_bytes, idle_seconds
        self.min_free_bytes, self.max_spool_bytes = min_free_bytes, max_spool_bytes
        self.clock, self.disk_usage, self.sync_seconds = clock, disk_usage, sync_seconds
        self.lock = threading.RLock()
        self.active, self.counters, self.last_sequences, self.errors = {}, {}, {}, {}
        self.last_sampling_totals = {}
        self.closed = False
        self.finalized = self.written_bytes = 0
        self._checked, self._spool_size, self._new_bytes = float('-inf'), 0, 0
        if type(background_resources) is not bool:
            raise ValueError('background_resources must be a boolean')
        self.resource_monitor = None
        self._recover_interrupted()
        if background_resources:
            from .archive_resources import StorageMonitor
            self.resource_monitor = StorageMonitor(self.root, disk_usage=disk_usage,
                interval=resource_probe_interval, spool_interval=resource_spool_interval,
                max_age=resource_probe_max_age)
            self.resource_monitor.start()

    def _recover_interrupted(self):
        """Keep originals; publish only verified prefixes as independent copies."""
        stale = self.root/'.camera-active'
        if stale.is_symlink():
            self.errors['recovery'] = '相机恢复目录为链接，未读取目录外数据'
            return
        if stale.exists():
            try:
                _directory(self.root, '.camera-active')
                recovery = _directory(self.root, 'recovery', create=True)
                with os.scandir(stale) as entries:
                    for entry in entries:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            self.errors['recovery'] = '相机恢复目录含异常路径，原件未改动'
                            continue
                        # Quarantine before opening any payload, and never
                        # replace another original with the same old ID.
                        target = recovery/('camera-'+uuid4().hex)
                        Path(entry.path).rename(target)
            except (OSError, ValueError):
                self.errors['recovery'] = '相机原件已保留，恢复目录暂不可用'
                return
        recovery = self.root/'recovery'
        if not recovery.exists() and not recovery.is_symlink():
            return
        try:
            _directory(self.root, 'recovery')
            with os.scandir(recovery) as entries:
                for entry in entries:
                    if not entry.name.startswith('camera-'):
                        continue
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        self.errors['recovery'] = '相机原件含异常路径，未读取链接目标'
                        continue
                    try:
                        if not self._recover_camera(Path(entry.path)):
                            self.errors['recovery'] = '部分相机原件无法验证，已保留在 recovery'
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        self.errors['recovery'] = '相机恢复未完成，原件已保留在 recovery，重启后重试'
        except (OSError, ValueError):
            self.errors['recovery'] = '相机恢复目录不安全或暂不可用，原件未改动'

    def _recovery_session(self, entry):
        # Native segments have a flat set of regular files. Reject unexpected
        # directories, links, hardlinks and special files rather than following
        # any path supplied by an interrupted/malformed source.
        with os.scandir(entry) as children:
            for index, child in enumerate(children):
                info = child.stat(follow_symlinks=False)
                if (index >= 64 or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid() or info.st_nlink != 1):
                    raise ValueError('unsafe interrupted camera entry')
        session = _bounded_json(entry/'session.json', MAX_CAMERA_SESSION_BYTES)
        if (type(session.get('schemaVersion')) is not int or session['schemaVersion'] != 1
                or session.get('scope') != 'garage' or session.get('source') != 'native-camera-jpeg'
                or session.get('robotId') != self.robot_id):
            raise ValueError('untrusted interrupted camera session')
        camera, sensor = _name(session.get('cameraId')), _name(session.get('sensor'))
        if self.camera_names is not None and self.camera_names.get(camera) != sensor:
            raise ValueError('camera identity does not match the configured source')
        return session

    def _published_camera_matches(self, target, session, identity, plan):
        _directory(self.root, *target.relative_to(self.root).parts)
        # A video is not uploadable until the independent finalizer publishes
        # archive.json. Its durable pending manifest is a valid recovery
        # checkpoint too; revisiting it must not create another recovered copy.
        manifest_path = target/'archive.json'
        pending = not (manifest_path.exists() or manifest_path.is_symlink())
        if pending:
            manifest_path = target/'video-pending.json'
        manifest = _bounded_json(manifest_path, 64*1024)
        metadata = manifest.get('metadata', {})
        if (manifest.get('id') != identity or manifest.get('robotId') != self.robot_id
                or manifest.get('sensor') != session['sensor']
                or not isinstance(metadata, dict) or metadata.get('scope') != 'garage'
                or metadata.get('source') != 'native-camera-jpeg'
                or metadata.get('captureInterrupted') is not True
                or metadata.get('cameraId') != session['cameraId']):
            return False
        files = manifest.get('files')
        source_files = [
            dict(relativePath='frames.mjpg', sha256=plan['framesSha256'], size=plan['size']),
            dict(relativePath='index.jsonl', sha256=plan['indexSha256'], size=plan['indexSize']),
        ]
        if files == ['video.mkv'] and not pending:
            if (metadata.get('videoRequired') is not True
                    or metadata.get('videoReady') is not True
                    or metadata.get('framesSha256') != plan['framesSha256']
                    or metadata.get('indexSha256') != plan['indexSha256']
                    or metadata.get('sourceFiles') != source_files
                    or not isinstance(metadata.get('videoSha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', metadata['videoSha256'])
                    or type(metadata.get('videoBytes')) is not int or metadata['videoBytes'] <= 0):
                return False
            expected = {'video.mkv': (metadata['videoSha256'], metadata['videoBytes'])}
            video = target/'video.mkv'
            if not video.exists() and not video.is_symlink():
                return self._published_pruned_matches(target, manifest_path, expected)
            digest, size = hashlib.sha256(), 0
            with _reader(video) as stream:
                while chunk := stream.read(1024*1024):
                    digest.update(chunk)
                    size += len(chunk)
            return (digest.hexdigest(), size) == expected['video.mkv']
        if files != ['frames.mjpg', 'index.jsonl']:
            return False
        if pending and (metadata.get('videoRequired') is not True
                        or metadata.get('format') != 'matroska/mjpeg'
                        or metadata.get('framesSha256') != plan['framesSha256']
                        or metadata.get('indexSha256') != plan['indexSha256']
                        or metadata.get('sourceFiles') != source_files):
            return False
        if not all((target/name).exists() for name in ('frames.mjpg', 'index.jsonl')):
            # The upload worker may have legitimately pruned these transfer
            # copies after full server confirmation. Do not flag those as
            # broken or regenerate an already uploaded recovery on reboot.
            if pending:
                return False
            expected = {'frames.mjpg': (plan['framesSha256'], plan['size']),
                        'index.jsonl': (plan['indexSha256'], plan['indexSize'])}
            return self._published_pruned_matches(target, manifest_path, expected)
        actual = _inspect_prefix(target)
        return (actual['count'] == plan['count'] and actual['size'] == plan['size']
                and actual['framesSha256'] == plan['framesSha256']
                and actual['indexSha256'] == plan['indexSha256']
                and not actual['tail'] and not actual['indexTail'] and actual['reason'] is None)

    @staticmethod
    def _published_pruned_matches(target, manifest_path, expected):
        pruned = _bounded_json(target/'local-copy-pruned.json', 64*1024)
        ack = _bounded_json(target/'uploaded.json', 64*1024)
        with _reader(manifest_path) as stream:
            raw_manifest = stream.read(64*1024 + 1)
        rows = ack.get('files')
        if (pruned.get('state') not in ('pruning', 'pruned') or ack.get('state') != 'complete'
                or ack.get('manifestSha256') != hashlib.sha256(raw_manifest).hexdigest()
                or not isinstance(rows, list) or len(rows) != len(expected)):
            return False
        if {row.get('relativePath') for row in rows if isinstance(row, dict)} != set(expected):
            return False
        return all(isinstance(row, dict) and isinstance(row.get('robotFileId'), str)
            and bool(row['robotFileId'])
            and (row.get('sha256'), row.get('size')) == expected[row['relativePath']] for row in rows)

    def _publish_capture_manifest(self, path, manifest):
        """Only raw legacy captures can bypass the video finalizer."""
        if self.video_format == 'mkv':
            metadata = manifest['metadata']
            metadata.update(videoRequired=True, format='matroska/mjpeg')
            metadata['sourceFiles'] = [
                dict(relativePath='frames.mjpg', sha256=metadata['framesSha256'], size=metadata['rawBytes']),
                dict(relativePath='index.jsonl', sha256=metadata['indexSha256'], size=(path/'index.jsonl').stat().st_size),
            ]
            _atomic_json(path/'video-pending.json', manifest)
        else:
            _atomic_json(path/'archive.json', manifest)

    def _recover_camera(self, entry):
        session = self._recovery_session(entry)
        plan = _inspect_prefix(entry)
        if not plan['count']:
            return False
        marker_path = entry/'camera-recovery.json'
        try:
            marker = _bounded_json(marker_path)
        except FileNotFoundError:
            marker = None
        if marker is not None and (type(marker.get('schemaVersion')) is not int or marker['schemaVersion'] != 1
                or marker.get('robotId') != self.robot_id
                or marker.get('cameraId') != session['cameraId']
                or not isinstance(marker.get('segmentId'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', marker['segmentId'])):
            raise ValueError('invalid camera recovery checkpoint')
        identity = marker['segmentId'] if marker else uuid4().hex
        start, end = (datetime.fromtimestamp(plan[key], SHANGHAI) for key in ('first', 'last'))
        day, sensor = start.strftime('%Y-%m-%d'), session['sensor']
        label = f'{sensor}_{start:%H:%M}-{end:%H:%M}'
        parent = _directory(self.root, 'days', day, sensor, label, create=True)
        target = parent/identity
        checkpoint = dict(schemaVersion=1, robotId=self.robot_id, cameraId=session['cameraId'],
            segmentId=identity, recoveredFrames=plan['count'], unrecoveredTailBytes=plan['tail'],
            unrecoveredIndexBytes=plan['indexTail'], stoppedReason=plan['reason'])
        if target.exists() or target.is_symlink():
            if not self._published_camera_matches(target, session, identity, plan):
                raise ValueError('published recovery copy does not match original')
            _atomic_json(marker_path, dict(checkpoint, state='recovered'))
        else:
            copied_size = plan['size']+plan['indexSize']
            # Reuse the reserved destination ID for staging too. A failed copy
            # must not consume the quota again on every restart. Only our
            # well-known derivative files are removed; originals stay intact.
            staging_root = _directory(self.root, '.camera-recovering', create=True)
            stage = staging_root/identity
            if stage.exists() or stage.is_symlink():
                _directory(self.root, '.camera-recovering', identity)
                with os.scandir(stage) as children:
                    for child in children:
                        info = child.stat(follow_symlinks=False)
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                                or info.st_nlink != 1 or not (child.name in ('frames.mjpg', 'index.jsonl', 'archive.json', 'video-pending.json')
                                or re.fullmatch(r'(?:archive|video-pending)\.json\.tmp-[a-f0-9]{32}', child.name))):
                            raise ValueError('unsafe interrupted recovery staging copy')
                        Path(child.path).unlink()
                stage.rmdir()
                self._checked = float('-inf')
            now = self.clock()
            if (not self._capacity(now)
                    or self.disk_usage(self.root).free-copied_size <= self.min_free_bytes
                    or self._spool_size+self._new_bytes+copied_size > self.max_spool_bytes):
                raise OSError('not enough capacity to preserve original and recovery copy')
            # Reserve a stable destination ID before copying, so a second power
            # loss after the final rename cannot publish the same prefix twice.
            _atomic_json(marker_path, dict(checkpoint, state='recovering'))
            stage.mkdir(mode=0o700)
            with (stage/'frames.mjpg').open('xb') as frames, (stage/'index.jsonl').open('xb') as index:
                os.chmod(stage/'frames.mjpg', 0o600)
                os.chmod(stage/'index.jsonl', 0o600)
                copied = _inspect_prefix(entry, frames, index)
                if copied != plan:
                    raise OSError('original camera prefix changed during recovery')
                for output in (frames, index):
                    output.flush()
                    os.fsync(output.fileno())
            metadata = dict(scope='garage', source='native-camera-jpeg', format='mjpeg+jsonl',
                timezone='Asia/Shanghai', closeReason='power_loss_recovered', captureInterrupted=True,
                cameraId=session['cameraId'], rawBytes=plan['size'], receivedMessages=plan['count'],
                receivedFrames=plan['count'], recoveredFrames=plan['count'], sequenceGaps=plan['gaps'],
                sequenceResets=plan['resets'], unrecoveredTailBytes=plan['tail'],
                unrecoveredIndexBytes=plan['indexTail'], recoveryStoppedReason=plan['reason'],
                recoveryOriginal=entry.name, framesSha256=plan['framesSha256'], indexSha256=plan['indexSha256'],
                dimensions=[{'width': width, 'height': height} for width, height in plan['dimensions']],
                resized=False, reencoded=False, timestampBasis='source timestampNs and original wallTime in index.jsonl')
            metadata.update(_camera_clock_metadata(session))
            # Recovery belongs to its durable source session, even when a new
            # desktop capture is active. Never borrow the current gate's token.
            session_id = session.get('captureSessionId')
            if isinstance(session_id, str) and re.fullmatch(r'[a-f0-9]{32}', session_id):
                metadata['captureSessionId'] = session_id
            if session.get('archiveProfile') == 'compact-v1':
                metadata['archiveProfile'] = 'compact-v1'
                if session.get('videoArchivePreset') in ('720p-economy-v1', '720p-balanced-v1'):
                    metadata['videoArchivePreset'] = session['videoArchivePreset']
            if 'cameraCalibration' in session:
                # The source session is the only calibration authority after
                # an interruption. A newly installed result belongs to future
                # segments and cannot relabel frames captured earlier.
                try:
                    calibration = _calibration_copy(session['cameraCalibration'], session['cameraId'])
                    source_size = calibration.get('size') if calibration is not None else None
                    if (calibration is not None and calibration.get('currentImageInfo') is not None
                            and any(dict(width=size[0], height=size[1]) != source_size
                                    for size in plan['dimensions'])):
                        calibration = _calibration_mismatch(calibration, plan['dimensions'][0])
                    if calibration is not None:
                        metadata['cameraCalibration'] = calibration
                except Exception:
                    metadata['cameraCalibration'] = dict(state='invalid', cameraId=session['cameraId'],
                        errorCode='invalid_saved_calibration')
            self._publish_capture_manifest(stage, dict(schemaVersion=1, id=identity, day=day,
                sensor=sensor, label=label, robotId=self.robot_id,
                startedAt=datetime.fromtimestamp(plan['first'], timezone.utc).isoformat(),
                endAt=datetime.fromtimestamp(plan['last'], timezone.utc).isoformat(),
                metadata=metadata, files=['frames.mjpg', 'index.jsonl']))
            stage.rename(target)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _atomic_json(marker_path, dict(checkpoint, state='recovered'))
            self.finalized += 1
            self.written_bytes += plan['size']
            self._new_bytes += copied_size
            self.note_loss(session['cameraId'], 'recoveredFrames', plan['count'])
        if plan['tail'] or plan['indexTail'] or plan['reason']:
            self.errors['recovery'] = '已恢复完整相机帧；未验证尾部及原版保留在 recovery'
        return True

    def enabled(self):
        try:
            function = self.gate.enabled if hasattr(self.gate, 'enabled') else self.gate
            return callable(function) and function() is True
        except Exception:
            return False

    def note_loss(self, camera_id, reason, count=1):
        with self.lock:
            _name(camera_id)
            if camera_id not in self.counters and len(self.counters) >= MAX_CAMERAS:
                return
            self.counters.setdefault(camera_id, Counter())[reason] += count

    def _capacity(self, now, incoming_bytes=0):
        if self.resource_monitor is not None:
            resources = self.resource_monitor.snapshot(incoming_bytes)
            if not resources['ready']:
                self.errors['storage'] = '原相机存储资源探针未就绪、过期或失败，已暂停采集：' + resources['state']
                return False
            if (resources['freeBytes'] <= self.min_free_bytes
                    or self.capacity is not None and self.capacity() is not True):
                self.errors['storage'] = '原相机归档已因空间保护暂停；未删除原件'
                return False
            if (resources['spoolBytes'] > self.max_spool_bytes
                    or resources['spoolBytes']-incoming_bytes >= self.max_spool_bytes):
                self.errors['storage'] = '原相机缓存达到上限；未删除未入仓原件'
                return False
            self.errors.pop('storage', None)
            return True
        if self.disk_usage(self.root).free <= self.min_free_bytes or (self.capacity is not None and self.capacity() is not True):
            self.errors['storage'] = '原相机归档已因空间保护暂停；未删除原件'
            return False
        if now-self._checked >= 10 or now < self._checked:
            self._spool_size = 0
            for path in self.root.rglob('*'):
                try:
                    if path.is_file() and not path.is_symlink():
                        self._spool_size += path.stat().st_size
                except FileNotFoundError:
                    # Upload retention and finalization run concurrently.
                    continue
            self._new_bytes, self._checked = 0, now
        if self._spool_size+self._new_bytes >= self.max_spool_bytes:
            self.errors['storage'] = '原相机缓存达到上限；未删除未入仓原件'
            return False
        self.errors.pop('storage', None)
        return True

    def _new(self, camera_id, now, dimensions):
        sensor = _name(camera_id) if self.camera_names is None else self.camera_names[camera_id]
        identity = uuid4().hex
        path = self.root/'.camera-active'/identity
        path.mkdir(parents=True, mode=0o700)
        value = dict(id=identity, path=path, sensor=sensor, cameraId=camera_id, start=now, end=now,
                     day=datetime.fromtimestamp(now, SHANGHAI).strftime('%Y-%m-%d'), bytes=0,
                     frames=0, gaps=0, sync=now, dimensions=set(), videoArchivePreset='720p-balanced-v1',
                     framesDigest=hashlib.sha256(), indexDigest=hashlib.sha256())
        snapshot = self.gate.snapshot() if hasattr(self.gate, 'snapshot') else {}
        session_id = snapshot.get('desktopSession') if isinstance(snapshot, dict) else None
        if isinstance(session_id, str) and re.fullmatch(r'[a-f0-9]{32}', session_id):
            value['captureSessionId'] = session_id
        # _new runs in the spool writer after JPEG headers were validated,
        # never in NativeCameraSink.submit() or a sensor callback. One read per
        # segment freezes the result while retaining exact source JPEG bytes.
        try:
            from .camera_calibration import get_camera_calibration
            calibration = _calibration_copy(get_camera_calibration(camera_id, *dimensions,
                robot_id=self.robot_id, path=self.calibration_path, resize=False), camera_id)
        except Exception:
            calibration = dict(state='invalid', cameraId=camera_id, errorCode='calibration_load_failed')
        if calibration is not None:
            value['cameraCalibration'] = calibration
            value['calibrationDimensions'] = dimensions
        session = {'schemaVersion': 1, 'scope': 'garage',
            'source': 'native-camera-jpeg', 'robotId': self.robot_id, 'sensor': sensor, 'cameraId': camera_id,
            'archiveProfile': 'compact-v1', 'videoArchivePreset': value['videoArchivePreset'],
            **({'captureSessionId': value['captureSessionId']} if 'captureSessionId' in value else {}),
            **({'videoTargetFps': self.video_fps} if self.video_fps else {})}
        session.update(clockSource='CLOCK_REALTIME', timestampSemantics='host_read_complete')
        if self.clock_domain is not None:
            session['clockDomain'] = self.clock_domain
        if calibration is not None:
            session['cameraCalibration'] = _calibration_copy(calibration, camera_id)
        value['session'] = session
        _atomic_json(path/'session.json', session)
        value['framesFile'] = (path/'frames.mjpg').open('xb')
        value['indexFile'] = (path/'index.jsonl').open('x', encoding='utf-8')
        os.chmod(path/'frames.mjpg', 0o600)
        os.chmod(path/'index.jsonl', 0o600)
        self.active[camera_id] = value
        return value

    def record(self, camera_id, jpeg, *, sequence, timestamp_ns, wall_time=None, width=None, height=None,
               sampled_before=0, sampled_total=None):
        return self._record(camera_id, jpeg, sequence=sequence, timestamp_ns=timestamp_ns,
            wall_time=wall_time, width=width, height=height, admitted=False,
            sampled_before=sampled_before, sampled_total=sampled_total)

    def record_accepted(self, camera_id, jpeg, *, sequence, timestamp_ns, wall_time=None, width=None, height=None,
                        sampled_before=0, sampled_total=None):
        """Drain a frame accepted by NativeCameraSink before its gate closed."""
        return self._record(camera_id, jpeg, sequence=sequence, timestamp_ns=timestamp_ns,
            wall_time=wall_time, width=width, height=height, admitted=True,
            sampled_before=sampled_before, sampled_total=sampled_total)

    def _record(self, camera_id, jpeg, *, sequence, timestamp_ns, wall_time, width, height, admitted,
                sampled_before=0, sampled_total=None):
        with self.lock:
            now = self.clock() if wall_time is None else wall_time
            _name(camera_id)
            if self.camera_names is not None and camera_id not in self.camera_names:
                raise ValueError('unknown camera identity')
            if camera_id not in self.counters and len(self.counters) >= MAX_CAMERAS:
                raise ValueError('camera identity limit reached')
            if self.closed:
                return False
            if not admitted and not self.enabled():
                # Sequence numbers continue while garage capture is off. They
                # are not missing recorded frames, so the next enabled period
                # starts a fresh baseline. Lifetime loss counters stay intact.
                self._finish_all('garage_disabled')
                self.last_sequences.clear()
                self.last_sampling_totals.clear()
                return False
            if (not isinstance(jpeg, bytes) or type(sequence) is not int or sequence < 0
                    or type(timestamp_ns) is not int or timestamp_ns <= 0
                    or type(now) not in (int, float) or not math.isfinite(now) or now <= 0):
                self.note_loss(camera_id, 'invalidPackets')
                return False
            try:
                dimensions = _jpeg_shape(jpeg)
                if (width is not None and (type(width) is not int or width != dimensions[0])) or (height is not None and (type(height) is not int or height != dimensions[1])):
                    raise ValueError('source dimensions do not match JPEG header')
            except ValueError:
                self.note_loss(camera_id, 'corruptPackets')
                return False
            if len(jpeg) > self.segment_bytes:
                self.note_loss(camera_id, 'oversizedPackets')
                return False
            # Reserve room for the incoming JPEG and its bounded index line.
            # The monitor accounts the exact bytes below once the line exists.
            if not self._capacity(now, len(jpeg)+MAX_INDEX_LINE_BYTES):
                self._finish_all('storage_paused')
                self.note_loss(camera_id, 'storageDropped')
                return False
            previous = self.last_sequences.get(camera_id)
            if sequence == previous:
                self.note_loss(camera_id, 'duplicatePackets')
                return False
            if sampled_total is not None:
                if type(sampled_total) is not int or sampled_total < self.last_sampling_totals.get(camera_id, 0):
                    raise ValueError('invalid cumulative sampled frame count')
                sampled_before = sampled_total-self.last_sampling_totals.get(camera_id, 0) if previous is not None else 0
            if type(sampled_before) is not int or sampled_before < 0:
                raise ValueError('invalid sampled frame count')
            gap = max(0, sequence-previous-1-sampled_before) if previous is not None else 0
            reset = previous is not None and sequence < previous
            day = datetime.fromtimestamp(now, SHANGHAI).strftime('%Y-%m-%d')
            value = self.active.get(camera_id)
            if value and (day != value['day'] or now-value['start'] >= self.segment_seconds
                    or now < value['start'] or value['bytes']+len(jpeg) > self.segment_bytes
                    or (self.video_format == 'mkv' and dimensions not in value['dimensions'])):
                self._finish(camera_id, 'rotation')
                value = None
            value = value or self._new(camera_id, now, dimensions)
            calibration = value.get('cameraCalibration')
            if (calibration is not None and dimensions != value.get('calibrationDimensions')
                    and calibration.get('currentImageInfo') is not None):
                # Legacy MJPEG segments can contain multiple resolutions.
                # Preserve the evidence but remove a matrix for a single size.
                value['cameraCalibration'] = _calibration_mismatch(calibration, dimensions)
                value['session']['cameraCalibration'] = value['cameraCalibration']
                try:
                    _atomic_json(value['path']/'session.json', value['session'])
                except Exception:
                    # Calibration metadata failure cannot stop raw recording.
                    # Recovery also checks sizes against the frozen snapshot.
                    self.errors['calibration:'+camera_id] = '标定快照更新失败；原图继续保存'
            row = dict(sequence=sequence, timestampNs=timestamp_ns, wallTime=now,
                       offset=value['bytes'], length=len(jpeg), width=dimensions[0], height=dimensions[1],
                       sha256=hashlib.sha256(jpeg).hexdigest(), gapBefore=gap, sequenceReset=reset)
            if self.video_fps or sampled_total is not None:
                row['sampledOutBefore'] = sampled_before
            encoded = json.dumps(row, separators=(',', ':'), allow_nan=False)+'\n'
            reserved = len(jpeg)+len(encoded.encode())
            if self.resource_monitor is not None:
                self.resource_monitor.begin_write(reserved)
            try:
                if value['framesFile'].write(jpeg) != len(jpeg):
                    raise OSError('short native JPEG write')
                value['framesFile'].flush()
                if value['indexFile'].write(encoded) != len(encoded):
                    raise OSError('short native JPEG index write')
                value['indexFile'].flush()
                if now-value['sync'] >= self.sync_seconds:
                    os.fsync(value['framesFile'].fileno())
                    os.fsync(value['indexFile'].fileno())
                    value['sync'] = now
            except Exception:
                self.errors[camera_id] = '原相机JPEG写入失败；片段保留待校验'
                self.note_loss(camera_id, 'writeErrors')
                self._quarantine(camera_id)
                raise
            finally:
                if self.resource_monitor is not None:
                    self.resource_monitor.finish_write(reserved)
            value.update(end=now, bytes=value['bytes']+len(jpeg), frames=value['frames']+1, gaps=value['gaps']+gap)
            value['framesDigest'].update(jpeg)
            value['indexDigest'].update(encoded.encode())
            value['dimensions'].add(dimensions)
            self.last_sequences[camera_id] = sequence
            if sampled_total is not None:
                self.last_sampling_totals[camera_id] = sampled_total
            self.note_loss(camera_id, 'writtenFrames')
            self.note_loss(camera_id, 'sequenceGaps', gap)
            self.note_loss(camera_id, 'sequenceResets', int(reset))
            self.written_bytes += len(jpeg)
            self._new_bytes += len(jpeg)+len(encoded.encode())
            return True

    def _quarantine(self, camera_id):
        value = self.active.pop(camera_id)
        for key in ('framesFile', 'indexFile'):
            try:
                value[key].close()
            except Exception:
                pass
        target = self.root/'recovery'/('camera-'+value['id'])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        value['path'].rename(target)
        _atomic_json(target/'recovery-required.json', {'state': 'native_jpeg_write_failed', 'cameraId': camera_id})

    def _finish(self, camera_id, reason):
        value = self.active[camera_id]
        try:
            for key in ('framesFile', 'indexFile'):
                value[key].flush()
                os.fsync(value[key].fileno())
                value[key].close()
            start, end = (datetime.fromtimestamp(value[key], SHANGHAI) for key in ('start', 'end'))
            label = f"{value['sensor']}_{start:%H:%M}-{end:%H:%M}"
            manifest = dict(schemaVersion=1, id=value['id'], day=value['day'], sensor=value['sensor'], label=label,
                startedAt=datetime.fromtimestamp(value['start'], timezone.utc).isoformat(),
                endAt=datetime.fromtimestamp(value['end'], timezone.utc).isoformat(), robotId=self.robot_id,
                metadata=dict(scope='garage', source='native-camera-jpeg', format='mjpeg+jsonl',
                    archiveProfile='compact-v1', videoArchivePreset=value['videoArchivePreset'],
                    timezone='Asia/Shanghai', closeReason=reason, cameraId=camera_id, rawBytes=value['bytes'],
                    receivedMessages=value['frames'], receivedFrames=value['frames'], sequenceGaps=value['gaps'],
                    cameraCountersAtClose=dict(self.counters.get(camera_id, {})),
                    framesSha256=value['framesDigest'].hexdigest(), indexSha256=value['indexDigest'].hexdigest(),
                    dimensions=[{'width': width, 'height': height} for width, height in sorted(value['dimensions'])],
                    resized=False, reencoded=False, timestampBasis='source timestampNs and original wallTime in index.jsonl'),
                files=['frames.mjpg', 'index.jsonl'])
            if self.video_fps:
                manifest['metadata'].update(videoTargetFps=self.video_fps, videoSampling='recording-tap-clock-slots')
            if 'captureSessionId' in value:
                manifest['metadata']['captureSessionId'] = value['captureSessionId']
            if 'cameraCalibration' in value:
                manifest['metadata']['cameraCalibration'] = _calibration_copy(value['cameraCalibration'], camera_id)
            manifest['metadata'].update(_camera_clock_metadata(value['session']))
            self._publish_capture_manifest(value['path'], manifest)
            target = self.root/'days'/value['day']/value['sensor']/label/value['id']
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            value['path'].rename(target)
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.active.pop(camera_id)
            self.finalized += 1
        except Exception:
            self.errors[camera_id] = '原相机片段收尾失败；原件保留待校验'
            if camera_id in self.active:
                self._quarantine(camera_id)
            raise

    def _finish_all(self, reason):
        for camera_id in list(self.active):
            try:
                self._finish(camera_id, reason)
            except Exception:
                pass

    def tick(self):
        with self.lock:
            now = self.clock()
            if not self.enabled():
                self._finish_all('garage_disabled')
                self.last_sequences.clear()
                self.last_sampling_totals.clear()
                return
            if not self._capacity(now):
                self._finish_all('storage_paused')
                return
            for camera_id, value in list(self.active.items()):
                if now-value['end'] >= self.idle_seconds or now-value['start'] >= self.segment_seconds:
                    self._finish(camera_id, 'idle' if now-value['end'] >= self.idle_seconds else 'rotation')

    def close(self):
        try:
            with self.lock:
                self.closed = True
                self._finish_all('shutdown')
        finally:
            if self.resource_monitor is not None:
                stopped = self.resource_monitor.close()
                with self.lock:
                    if stopped:
                        self.errors.pop('resourceMonitor', None)
                    else:
                        self.errors['resourceMonitor'] = '原相机空间检查线程尚未退出'

    def snapshot(self):
        with self.lock:
            return dict(enabled=self.enabled(), finalizedSegments=self.finalized, writtenBytes=self.written_bytes,
                        **({'resourceMonitor': self.resource_monitor.snapshot()}
                           if self.resource_monitor is not None else {}),
                        errors=dict(self.errors), cameras={key: dict(value) for key, value in self.counters.items()},
                        active={key: {'frames': row['frames'], 'bytes': row['bytes']} for key, row in self.active.items()})


class NativeCameraSink:
    """Explicitly started, bounded in-memory tap; all disk/gate calls are off capture threads."""
    def __init__(self, store, *, gate=None, queue_capacity=64, queue_bytes=64*1024**2, target_fps=0):
        if type(queue_capacity) is not int or queue_capacity < 1 or type(queue_bytes) is not int or queue_bytes < 1:
            raise ValueError('invalid native camera queue bounds')
        self.store, self.gate = store, gate or store.enabled
        from .archive_sampling import FrameSampler
        self.sampler = FrameSampler(target_fps)
        self.sampled_totals = Counter()
        self.queue_capacity, self.queue_bytes = queue_capacity, queue_bytes
        self.condition = threading.Condition(threading.RLock())
        self.queue, self.pending_losses = deque(), Counter()
        self.pending_bytes = self.epoch = 0
        self.active = self.stopping = self.inflight = False
        self.worker = None
        self.counters, self.errors = Counter(), {}
        self.known_cameras = set(store.camera_names or ()) if hasattr(store, 'camera_names') else set()
        self.allowed_cameras = frozenset(self.known_cameras) if self.known_cameras else None

    def reconcile_gate(self):
        try:
            enabled = self.gate() is True
        except Exception:
            enabled = False
        with self.condition:
            if enabled != self.active:
                self.active = enabled
                self.epoch += 1
                self.sampler.clear()
                # The gate controls admission. Already accepted frames remain
                # bounded in this queue and must reach disk on normal stop.
            self.condition.notify_all()

    def start(self):
        self.reconcile_gate()
        with self.condition:
            if self.stopping:
                raise RuntimeError('native camera sink is closed')
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name='native-camera-archive', daemon=True)
                self.worker.start()

    def submit(self, camera_id, jpeg, *, sequence, timestamp_ns, wall_time, width=None, height=None):
        # None means intentional sampling; False means an inactive/rejected tap.
        # No disk, network, gate-file read, hashing, decode, resize, or encode.
        with self.condition:
            self.counters['received'] += 1
            if self.stopping or not self.active:
                self.counters['inactiveDropped'] += 1
                return False
            try:
                _name(camera_id)
            except ValueError:
                self.counters['invalidPackets'] += 1
                return False
            if (not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= MAX_JPEG_BYTES
                    or (self.allowed_cameras is not None and camera_id not in self.allowed_cameras)
                    or (camera_id not in self.known_cameras and len(self.known_cameras) >= MAX_CAMERAS)):
                self.counters['invalidPackets'] += 1
                return False
            self.known_cameras.add(camera_id)
            if self.sampler.interval_ns:
                if type(timestamp_ns) is not int or timestamp_ns <= 0:
                    self.counters['invalidPackets'] += 1
                    return False
                if not self.sampler.accept(camera_id, timestamp_ns):
                    self.counters['sampledFrames'] += 1
                    self.sampled_totals[camera_id] += 1
                    self.pending_losses[(camera_id, 'sampledOutFrames')] += 1
                    return None
            if len(self.queue) >= self.queue_capacity or self.pending_bytes+len(jpeg) > self.queue_bytes:
                self.counters['queueDropped'] += 1
                self.pending_losses[(camera_id, 'queueDropped')] += 1
                return False
            sampled = self.sampled_totals[camera_id]
            self.queue.append((camera_id, jpeg, sequence, timestamp_ns, wall_time, width, height, self.epoch, sampled))
            self.pending_bytes += len(jpeg)
            self.counters['enqueued'] += 1
            self.condition.notify_all()
            return True

    def _run(self):
        while True:
            self.reconcile_gate()
            with self.condition:
                losses, self.pending_losses = self.pending_losses, Counter()
                if self.stopping and not self.queue:
                    break
                if not self.queue:
                    packet = None
                    self.condition.wait(.2)
                else:
                    packet = self.queue.popleft()
                    self.pending_bytes -= len(packet[1])
                    self.inflight = True
            for (camera_id, reason), count in losses.items():
                self.store.note_loss(camera_id, reason, count)
            try:
                if packet is not None:
                    camera_id, jpeg, sequence, timestamp_ns, wall, width, height, epoch, sampled = packet
                    record = getattr(self.store, 'record_accepted', self.store.record)
                    options = {'sampled_total': sampled} if self.sampler.interval_ns else {}
                    ok = record(camera_id, jpeg, sequence=sequence,
                        timestamp_ns=timestamp_ns, wall_time=wall, width=width, height=height, **options)
                    with self.condition:
                        self.counters['written' if ok else 'writerDropped'] += 1
                with self.condition:
                    drained = not self.queue
                # Sealing on a closed gate must follow the accepted frames,
                # otherwise every draining frame would reopen another file.
                if drained:
                    self.store.tick()
            except Exception as error:
                with self.condition:
                    self.counters['writeErrors'] += 1
                    self.errors['writer'] = type(error).__name__
            finally:
                with self.condition:
                    self.inflight = False
                    self.condition.notify_all()
        for (camera_id, reason), count in losses.items():
            self.store.note_loss(camera_id, reason, count)
        self.store.close()

    def flush(self, timeout=5):
        deadline = time.monotonic()+timeout
        with self.condition:
            while self.queue or self.inflight:
                left = deadline-time.monotonic()
                if left <= 0:
                    return False
                self.condition.wait(min(.2, left))
        return True

    def close(self, timeout=5):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=timeout)
            return not self.worker.is_alive()
        self.store.close()
        return True

    def snapshot(self):
        with self.condition:
            return dict(enabled=self.active, queueFrames=len(self.queue), queueBytes=self.pending_bytes,
                        inFlightFrames=int(self.inflight),
                        counters=dict(self.counters), errors=dict(self.errors),
                        videoTargetFps=self.sampler.fps,
                        writerRunning=self.worker is not None and self.worker.is_alive())
