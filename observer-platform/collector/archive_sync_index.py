"""Build optional software timestamp pairs from a selected sealed-video batch.

Only frame indexes are read. Videos are neither decoded nor rewritten, and an
absent clock identity is never inferred from a robot/session or filename.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .archive_time_sync import iter_pairs

MAX_INDEX_BYTES = 64 * 1024**2
MAX_OUTPUT_BYTES = 256 * 1024**2
MAX_MANIFEST_BYTES = 256 * 1024
# One hour at 10 FPS across six streams is 216,000 input rows. Each capture
# group has its own budget; an earlier session cannot exhaust a later one.
MAX_ROWS = 500_000
# Limit one indivisible session/clock group, never a prefix of the full batch.
MAX_SELECTED = 1024
MAX_LINE = 64 * 1024


class SyncIndexError(ValueError):
    pass


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def _safe_path(root, path):
    path = Path(os.path.abspath(path))
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SyncIndexError('SYNC_SOURCE_LINK')
    return path


def _read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or not 0 < before.st_size <= limit):
            raise SyncIndexError('SYNC_SOURCE_INVALID')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > limit or len(raw) != before.st_size or
            (before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise SyncIndexError('SYNC_SOURCE_CHANGED')
    return raw


def _mkdir(root, *parts):
    current = root
    for part in parts:
        if (not isinstance(part, str) or not part or part in ('.', '..')
                or re.search(r'[/\\\x00-\x1f\x7f]', part)):
            raise SyncIndexError('SYNC_DESTINATION_INVALID')
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise SyncIndexError('SYNC_DESTINATION_INVALID')
    return current


def _stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _extract_attachment(video, index, *, ffmpeg='ffmpeg', timeout=20):
    """Read a fixed attachment through a bounded pipe; process no video frames."""
    deadline = time.monotonic() + min(20, timeout)
    command = [ffmpeg, '-nostdin', '-v', 'error', '-dump_attachment:t:' + str(index),
        'pipe:1', '-i', str(video), '-map', '0:v:0', '-c:v', 'copy',
        '-frames:v', '0', '-f', 'null', os.devnull]
    with tempfile.TemporaryDirectory(prefix='robot-sync-index-') as temporary:
        target = Path(temporary) / 'index.jsonl'
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, True)
        selector.register(process.stderr, selectors.EVENT_READ, False)
        count = 0
        try:
            with target.open('xb') as output:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SyncIndexError('SYNC_ATTACHMENT_TIMEOUT')
                    for key, _events in selector.select(min(.25, remaining)):
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data:
                            count += len(chunk)
                            if count > MAX_INDEX_BYTES:
                                raise SyncIndexError('SYNC_ATTACHMENT_TOO_LARGE')
                            output.write(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SyncIndexError('SYNC_ATTACHMENT_TIMEOUT')
                if process.wait(timeout=remaining) != 0:
                    raise SyncIndexError('SYNC_ATTACHMENT_EXTRACTION_FAILED')
            return _read(target, MAX_INDEX_BYTES)
        except subprocess.TimeoutExpired:
            raise SyncIndexError('SYNC_ATTACHMENT_TIMEOUT') from None
        finally:
            selector.close()
            _stop(process)
            process.stdout.close()
            process.stderr.close()


def _index_bytes(root, path, manifest, name, extractor):
    attachments = manifest['metadata'].get('videoAttachments')
    expected, position = None, None
    if isinstance(attachments, list):
        for index, row in enumerate(attachments):
            if isinstance(row, dict) and row.get('name') == name:
                if expected is not None:
                    raise SyncIndexError('SYNC_ATTACHMENT_AMBIGUOUS')
                expected, position = row, index
    cache = None
    if expected is not None:
        digest = expected.get('sha256')
        if (not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                or type(expected.get('bytes')) is not int or not 0 < expected['bytes'] <= MAX_INDEX_BYTES):
            raise SyncIndexError('SYNC_ATTACHMENT_METADATA_INVALID')
        # Retention may remove sourceFiles such as the original index.jsonl.
        # The upload/prune paths preserve these byte-identical small indexes
        # under their manifest-bound attachment hash instead of a raw filename.
        cache = _safe_path(root, path.parent / '.sync-frame-indexes' / digest / name)
    cached = cache is not None and cache.exists()
    sidecar = None if cached else _safe_path(root, path.parent / name)
    if cached:
        raw = _read(cache, MAX_INDEX_BYTES)
    elif sidecar.exists():
        raw = _read(sidecar, MAX_INDEX_BYTES)
    else:
        if (expected is None or type(expected.get('bytes')) is not int
                or not 0 < expected['bytes'] <= MAX_INDEX_BYTES or position > 8):
            raise SyncIndexError('SYNC_ATTACHMENT_METADATA_MISSING')
        video = _safe_path(root, path.parent / 'video.mkv')
        info = video.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SyncIndexError('SYNC_VIDEO_UNAVAILABLE')
        raw = extractor(video, position)
        after = video.stat()
        if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise SyncIndexError('SYNC_SOURCE_CHANGED')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INDEX_BYTES:
        raise SyncIndexError('SYNC_INDEX_INVALID')
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and (expected.get('sha256') != digest or expected.get('bytes') != len(raw)):
        raise SyncIndexError('SYNC_ATTACHMENT_CHECKSUM')
    return raw, digest


def _rows(raw, manifest, *, is_usb):
    metadata = manifest['metadata']
    camera = metadata.get('cameraId') if is_usb else (
        'berxel_depth' if manifest['sensor'] == 'Berxel深度' else 'berxel_color')
    if not isinstance(camera, str) or not camera:
        raise SyncIndexError('SYNC_CAMERA_ID_MISSING')
    rows, first, previous_pts, previous_timestamp = [], None, -1, None
    for index, line in enumerate(raw.splitlines()):
        if index >= MAX_ROWS or not 0 < len(line) <= MAX_LINE:
            raise SyncIndexError('SYNC_INDEX_CAPACITY')
        row = json.loads(line)
        if not isinstance(row, dict) or type(row.get('timestampNs')) is not int or row['timestampNs'] <= 0:
            raise SyncIndexError('SYNC_TIMESTAMP_INVALID')
        if previous_timestamp is not None and row['timestampNs'] < previous_timestamp:
            raise SyncIndexError('SYNC_CLOCK_NON_MONOTONIC')
        previous_timestamp = row['timestampNs']
        if first is None:
            first = row['timestampNs']
        # Native-video _timing converts its original absolute timestamps into
        # per-segment microseconds, retaining strictly increasing packet PTS.
        pts = max(previous_pts + 1, max(0, (row['timestampNs'] - first) // 1000)) if is_usb else row.get('videoPtsUs')
        if type(pts) is not int or pts < 0 or pts <= previous_pts:
            raise SyncIndexError('SYNC_VIDEO_PTS_INVALID')
        previous_pts = pts
        result = dict(robotId=manifest['robotId'], captureSessionId=metadata['captureSessionId'],
            clockDomain=metadata['clockDomain'], cameraId=camera, segmentId=manifest['id'],
            frameIndex=index, timestampNs=row['timestampNs'], videoPtsUs=pts,
            streamId='usb_host_read' if is_usb else 'ros_host_receive')
        for key in ('sequence', 'sourceHeaderStamp'):
            if key in row:
                result[key] = row[key]
        rows.append(result)
    if not rows:
        raise SyncIndexError('SYNC_INDEX_EMPTY')
    return rows


def _eligible(manifest, *, camera_only, training_only):
    from .archive_policy import allowed_archive_manifest, training_manifest_allowed
    return (allowed_archive_manifest(manifest, camera_only, training_only)
            and (not training_only or training_manifest_allowed(manifest)))


def _identity(key, sources, tolerance_ns):
    robot, session, clock = key
    provenance = [item['provenance'] for item in sorted(sources, key=lambda item: item['provenance']['manifestPath'])]
    identity = hashlib.sha256(_json_bytes(dict(schemaVersion=1, robotId=robot,
        captureSessionId=session, clockDomain=clock, toleranceNs=tolerance_ns, sources=provenance))).hexdigest()
    started = min(datetime.fromisoformat(item['manifest']['startedAt'].replace('Z', '+00:00')) for item in sources)
    ended = max(datetime.fromisoformat(item['manifest']['endAt'].replace('Z', '+00:00')) for item in sources)
    if started.tzinfo is None or ended.tzinfo is None or ended < started:
        raise SyncIndexError('SYNC_CAPTURE_TIME_INVALID')
    day = started.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    return identity, provenance, started, ended, day


def _existing(root, key, sources, tolerance_ns, *, camera_only, training_only):
    # New manifests already bind their exact index attachment checksums. Their
    # deterministic identity locates a durable prior index even when retention
    # has removed videos; no archive-tree scan or attachment extraction occurs.
    if any(source['provenance'].get('indexSha256') is None for source in sources):
        return None
    identity, provenance, _started, _ended, day = _identity(key, sources, tolerance_ns)
    target = _safe_path(root, root / 'sync-indexes' / day / key[1] / identity[:32])
    if not target.exists():
        return None
    manifest_path = _safe_path(root, target / 'archive.json')
    raw_manifest = _read(manifest_path, MAX_MANIFEST_BYTES)
    manifest = json.loads(raw_manifest)
    metadata = manifest.get('metadata') if isinstance(manifest, dict) else None
    if (not isinstance(metadata, dict) or manifest.get('files') != ['sync-index.jsonl', 'sync-summary.json']
            or manifest.get('schemaVersion') != 1 or manifest.get('id') != identity[:32]
            or manifest.get('sensor') != '同步配对' or manifest.get('robotId') != key[0]
            or metadata.get('scope') != 'garage' or metadata.get('source') != 'native-time-sync'
            or metadata.get('syncKind') != 'software_host_timestamp'
            or metadata.get('hardwareSynchronized') is not False
            or metadata.get('clockSource') != 'CLOCK_REALTIME'
            or metadata.get('syncScope') != 'selected_sealed_session_snapshot'
            or metadata.get('toleranceNs') != tolerance_ns or metadata.get('captureSessionId') != key[1]
            or metadata.get('clockDomain') != key[2] or metadata.get('sourceFingerprint') != identity
            or metadata.get('sources') != provenance):
        raise SyncIndexError('SYNC_EXISTING_INDEX_CONFLICT')
    ack_path = _safe_path(root, target / 'uploaded.json')
    if ack_path.exists():
        ack = json.loads(_read(ack_path, MAX_MANIFEST_BYTES))
        files = ack.get('files') if isinstance(ack, dict) else None
        if (isinstance(ack, dict) and ack.get('schemaVersion') == 1
                and ack.get('state') == 'complete' and ack.get('segmentId') == manifest['id']
                and ack.get('manifestSha256') == hashlib.sha256(raw_manifest).hexdigest()
                and isinstance(files, list) and len(files) == 2
                and all(isinstance(row, dict) and isinstance(row.get('robotFileId'), str)
                        and bool(row['robotFileId']) for row in files)
                and {row.get('relativePath') for row in files} == set(manifest['files'])):
            # This exact manifest and both files are already confirmed in the
            # warehouse. The upload hook checks the same ACK before uploading;
            # do not rehash every historical index or require pruned local data.
            return manifest_path, True, _eligible(manifest, camera_only=camera_only, training_only=training_only)
    for filename, hash_key, size_key in (('sync-index.jsonl', 'indexSha256', 'indexBytes'),
                                        ('sync-summary.json', 'summarySha256', 'summaryBytes')):
        raw = _read(_safe_path(root, target / filename), MAX_OUTPUT_BYTES if filename.endswith('jsonl') else MAX_MANIFEST_BYTES)
        if len(raw) != metadata.get(size_key) or hashlib.sha256(raw).hexdigest() != metadata.get(hash_key):
            raise SyncIndexError('SYNC_EXISTING_INDEX_CONFLICT')
    return manifest_path, True, _eligible(manifest, camera_only=camera_only, training_only=training_only)


def _group_order(root, groups):
    """Resume a fair round-robin after a bounded upload was interrupted."""
    keys = sorted(groups)
    path = _safe_path(root, root / '.sync-index-cursor.json')
    if not path.exists():
        return keys
    value = json.loads(_read(path, 2048))
    next_group = value.get('nextGroup') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1
            or not isinstance(next_group, list) or len(next_group) != 3
            or any(not isinstance(part, str) or not 0 < len(part) <= 160
                   or any(ord(char) < 32 for char in part) for part in next_group)):
        raise SyncIndexError('SYNC_CURSOR_INVALID')
    next_group = tuple(next_group)
    split = next((index for index, key in enumerate(keys) if key >= next_group), 0)
    return keys[split:] + keys[:split]


def _advance_cursor(root, next_group):
    from .archive_core import atomic_json
    _safe_path(root, root / '.sync-index-cursor.json')
    atomic_json(root / '.sync-index-cursor.json', dict(schemaVersion=1, nextGroup=list(next_group)))


@contextmanager
def _lock(root):
    fd = os.open(root / '.sync-index.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise SyncIndexError('SYNC_LOCK_INVALID')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncIndexError('SYNC_BUILD_BUSY') from None
        yield
    finally:
        os.close(fd)


def _publish(root, key, sources, rgb, depth, tolerance_ns, *, camera_only, training_only):
    robot, session, clock = key
    identity, provenance, started, ended, day = _identity(key, sources, tolerance_ns)
    parent = _mkdir(root, 'sync-indexes', day, session)
    target = parent / identity[:32]
    stage = parent / ('.sync-writing-' + uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        counts, digest, size = Counter(), hashlib.sha256(), 0
        output_path = stage / 'sync-index.jsonl'
        with output_path.open('xb') as output:
            os.chmod(output_path, 0o600)
            for pair in iter_pairs(rgb, depth, tolerance_ns=tolerance_ns):
                raw = _json_bytes(pair)
                size += len(raw)
                if size > MAX_OUTPUT_BYTES:
                    raise SyncIndexError('SYNC_OUTPUT_CAPACITY')
                output.write(raw)
                digest.update(raw)
                counts[pair['state']] += 1
                counts['depthReused'] += int(pair.get('depthReusedWithinCamera') is True)
            output.flush()
            os.fsync(output.fileno())
        summary = dict(schemaVersion=1, robotId=robot, captureSessionId=session,
            scope='selected_sealed_session_snapshot',
            clockDomain=clock, clockSource='CLOCK_REALTIME', syncKind='software_host_timestamp',
            hardwareSynchronized=False, toleranceNs=tolerance_ns,
            depthReusePolicy='nearest_reuse_allowed', depthReusedFrames=counts['depthReused'],
            sourceManifestCount=len(sources), rgbFrames=len(rgb), depthFrames=len(depth),
            matchedFrames=counts['matched'], unmatchedFrames=counts['unmatched'],
            timestampSemantics=dict(usb='host_read_complete', ros='host_subscription_receive'),
            limitations=['Software host timestamps do not prove simultaneous exposure.',
                'Depth sourceHeaderStamp is preserved without assuming a shared epoch.',
                'No image registration or depth-value reconstruction is performed.'],
            indexSha256=digest.hexdigest(), indexBytes=size, sourceFingerprint=identity,
            sources=provenance)
        raw_summary = _json_bytes(summary)
        if len(raw_summary) > MAX_MANIFEST_BYTES:
            raise SyncIndexError('SYNC_SUMMARY_CAPACITY')
        with (stage / 'sync-summary.json').open('xb') as stream:
            os.chmod(stage / 'sync-summary.json', 0o600)
            stream.write(raw_summary)
            stream.flush()
            os.fsync(stream.fileno())
        manifest = dict(schemaVersion=1, id=identity[:32], day=day, robotId=robot,
            sensor='同步配对', label='同步配对_' + session[:12],
            startedAt=started.isoformat(), endAt=ended.isoformat(), files=['sync-index.jsonl', 'sync-summary.json'],
            metadata=dict(scope='garage', source='native-time-sync', captureSessionId=session,
                clockDomain=clock, clockSource='CLOCK_REALTIME',
                syncScope='selected_sealed_session_snapshot',
                syncKind='software_host_timestamp', hardwareSynchronized=False,
                toleranceNs=tolerance_ns, sourceManifestCount=len(sources), sources=provenance,
                rgbFrames=len(rgb), depthFrames=len(depth), matchedFrames=counts['matched'],
                unmatchedFrames=counts['unmatched'], indexSha256=digest.hexdigest(), indexBytes=size,
                depthReusedFrames=counts['depthReused'], depthReusePolicy='nearest_reuse_allowed',
                summarySha256=hashlib.sha256(raw_summary).hexdigest(), summaryBytes=len(raw_summary),
                sourceFingerprint=identity))
        raw_manifest = _json_bytes(manifest)
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise SyncIndexError('SYNC_MANIFEST_CAPACITY')
        with (stage / 'archive.json').open('xb') as stream:
            os.chmod(stage / 'archive.json', 0o600)
            stream.write(raw_manifest)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        reused = target.exists() or target.is_symlink()
        if reused:
            _safe_path(root, target)
            if (_read(target / 'archive.json', MAX_MANIFEST_BYTES) != raw_manifest
                    or _read(target / 'sync-summary.json', MAX_MANIFEST_BYTES) != raw_summary
                    or hashlib.sha256(_read(target / 'sync-index.jsonl', MAX_OUTPUT_BYTES)).hexdigest() != digest.hexdigest()):
                raise SyncIndexError('SYNC_EXISTING_INDEX_CONFLICT')
        else:
            stage.rename(target)
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return target / 'archive.json', reused, _eligible(manifest, camera_only=camera_only, training_only=training_only)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def build_sync_indexes(manifest_paths, root, robot_id, *, tolerance_ns=30_000_000,
                       extractor=None, ffmpeg='ffmpeg', camera_only=False, training_only=None):
    """Return optional local/eligible manifests; every failure leaves videos alone.

The caller supplies its current upload selection; this function never walks the
archive tree. ``readyManifestPaths`` contains only storage-policy-eligible
indexes. Excluded indexes stay outside ``days`` so normal video retry scans do
not mistake them for failed media uploads.
"""
    report = dict(readyManifestPaths=[], localManifestPaths=[], generated=0, reused=0,
                  skipped={}, errors=[])
    skipped = Counter()
    groups = defaultdict(list)
    try:
        root = Path(os.path.abspath(root))
        if root.is_symlink() or not root.is_dir():
            raise SyncIndexError('SYNC_ROOT_INVALID')
        if not isinstance(robot_id, str) or not robot_id:
            raise SyncIndexError('SYNC_ROBOT_ID_MISSING')
        if type(tolerance_ns) is not int or not 0 <= tolerance_ns <= 1_000_000_000:
            raise SyncIndexError('SYNC_TOLERANCE_INVALID')
        if training_only is None:
            from .archive_policy import training_only_enabled
            training_only = training_only_enabled()
        extractor = extractor or (lambda video, position: _extract_attachment(video, position, ffmpeg=ffmpeg))
        seen = set()
        with _lock(root):
            for selected in manifest_paths:
                path = None
                try:
                    path = _safe_path(root, selected)
                    if path in seen:
                        continue
                    seen.add(path)
                    raw = _read(path, MAX_MANIFEST_BYTES)
                    manifest = json.loads(raw)
                    metadata = manifest.get('metadata') if isinstance(manifest, dict) else None
                    if (not isinstance(metadata, dict) or manifest.get('schemaVersion') != 1
                            or manifest.get('files') != ['video.mkv'] or metadata.get('videoReady') is not True):
                        skipped['not_sealed_video'] += 1
                        continue
                    if manifest.get('robotId') != robot_id:
                        skipped['robot_mismatch'] += 1
                        continue
                    session, clock = metadata.get('captureSessionId'), metadata.get('clockDomain')
                    if not isinstance(session, str) or re.fullmatch('[a-f0-9]{32}', session) is None:
                        skipped['missing_session'] += 1
                        continue
                    if (not isinstance(clock, str) or re.fullmatch(
                            r'linux-boot:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', clock) is None):
                        skipped['missing_clock_domain'] += 1
                        continue
                    is_usb = metadata.get('source') == 'native-camera-jpeg'
                    is_ros = metadata.get('source') == 'native-ros-cdr' and manifest.get('sensor') in ('Berxel深度', 'Berxel彩色')
                    if not is_usb and not is_ros:
                        skipped['not_pair_source'] += 1
                        continue
                    semantics = 'host_read_complete' if is_usb else 'host_subscription_receive'
                    if (metadata.get('clockSource') != 'CLOCK_REALTIME'
                            or metadata.get('timestampSemantics') != semantics):
                        skipped['unverified_timestamp_semantics'] += 1
                        continue
                    if not isinstance(manifest.get('id'), str) or not manifest['id']:
                        raise SyncIndexError('SYNC_SEGMENT_ID_MISSING')
                    name = 'index.jsonl' if is_usb else 'frames.index.jsonl'
                    attachments = metadata.get('videoAttachments')
                    known = [row for row in attachments if isinstance(row, dict) and row.get('name') == name] if isinstance(attachments, list) else []
                    if len(known) > 1:
                        raise SyncIndexError('SYNC_ATTACHMENT_AMBIGUOUS')
                    expected = known[0] if known else {}
                    if expected and (not isinstance(expected.get('sha256'), str)
                            or re.fullmatch('[0-9a-f]{64}', expected['sha256']) is None
                            or type(expected.get('bytes')) is not int or not 0 < expected['bytes'] <= MAX_INDEX_BYTES):
                        raise SyncIndexError('SYNC_ATTACHMENT_METADATA_INVALID')
                    groups[(robot_id, session, clock)].append(dict(manifest=manifest, path=path,
                        is_usb=is_usb, index_name=name,
                        depth=manifest.get('sensor') == 'Berxel深度', provenance=dict(
                            manifestPath=path.relative_to(root).as_posix(),
                            manifestSha256=hashlib.sha256(raw).hexdigest(), indexName=name,
                            indexSha256=expected.get('sha256'), indexBytes=expected.get('bytes'),
                            segmentId=manifest['id'], videoSha256=metadata.get('videoSha256'))))
                except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    report['errors'].append(dict(code=str(error) if isinstance(error, SyncIndexError)
                        else 'SYNC_SOURCE_UNAVAILABLE', manifestPath=str(path.relative_to(root)) if path else None))
            ordered_groups = _group_order(root, groups)
            for group_index, key in enumerate(ordered_groups):
                sources = groups[key]
                failed_source = None
                rgb, depth = [], []
                try:
                    # Save the next complete group before any codec read. A
                    # deadline in one damaged/large session must not starve
                    # every later session on subsequent upload invocations.
                    _advance_cursor(root, ordered_groups[(group_index + 1) % len(ordered_groups)])
                    if len(sources) > MAX_SELECTED:
                        raise SyncIndexError('SYNC_SESSION_SOURCE_CAPACITY')
                    if not any(source['depth'] for source in sources) or not any(not source['depth'] for source in sources):
                        skipped['missing_rgb_or_depth'] += 1
                        continue
                    existing = _existing(root, key, sources, tolerance_ns,
                        camera_only=camera_only, training_only=training_only)
                    if existing is not None:
                        path, reused, eligible = existing
                    else:
                        group_rows = 0
                        for source in sources:
                            failed_source = source['path']
                            content, index_digest = _index_bytes(root, source['path'], source['manifest'], source['index_name'], extractor)
                            source['rows'] = _rows(content, source['manifest'], is_usb=source['is_usb'])
                            if len(source['rows']) + group_rows > MAX_ROWS:
                                raise SyncIndexError('SYNC_BATCH_FRAME_CAPACITY')
                            group_rows += len(source['rows'])
                            source['provenance'].update(indexSha256=index_digest, indexBytes=len(content))
                        rgb = [row for source in sources if not source['depth'] for row in source['rows']]
                        depth = [row for source in sources if source['depth'] for row in source['rows']]
                        rgb.sort(key=lambda row: (row['cameraId'], row['timestampNs'], row['segmentId'], row['frameIndex']))
                        depth.sort(key=lambda row: (row['timestampNs'], row['segmentId'], row['frameIndex']))
                        path, reused, eligible = _publish(root, key, sources, rgb, depth, tolerance_ns,
                            camera_only=camera_only, training_only=training_only)
                    report['localManifestPaths'].append(str(path))
                    report['reused' if reused else 'generated'] += 1
                    if eligible:
                        report['readyManifestPaths'].append(str(path))
                    else:
                        skipped['storage_policy_excluded'] += 1
                except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
                    report['errors'].append(dict(code=str(error) if isinstance(error, SyncIndexError)
                        else 'SYNC_BUILD_FAILED', captureSessionId=key[1],
                        manifestPath=str(failed_source.relative_to(root)) if failed_source else None))
                finally:
                    # ``groups`` retains source provenance until this batch
                    # returns, but completed/failed sessions must not retain
                    # their potentially large decoded frame-index lists.
                    for source in sources:
                        source.pop('rows', None)
                    rgb, depth = [], []
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        report['errors'].append(dict(code=str(error) if isinstance(error, SyncIndexError) else 'SYNC_BUILD_FAILED'))
    descriptions = dict(not_sealed_video='尚未封存为视频', robot_mismatch='机器人身份不匹配',
        missing_session='缺少本轮采集身份', missing_clock_domain='缺少可验证的同主机时钟身份',
        not_pair_source='不是相机同步来源', unverified_timestamp_semantics='时间戳来源未经确认',
        missing_rgb_or_depth='同一采集及主机时钟中缺少 RGB 或深度帧',
        storage_policy_excluded='同步索引已本地生成，当前存储策略尚未允许上传',
        selected_capacity='所选来源超过本次同步处理上限')
    report['skipped'] = dict(skipped)
    report['skippedReasons'] = [dict(reason=key, description=descriptions[key], count=count)
                                for key, count in skipped.items()]
    error_descriptions = dict(SYNC_ATTACHMENT_TIMEOUT='读取视频帧时间索引超时',
        SYNC_ATTACHMENT_TOO_LARGE='视频附件时间索引超过 64 MiB 上限',
        SYNC_ATTACHMENT_METADATA_MISSING='缺少可验证的帧时间附件信息',
        SYNC_ATTACHMENT_EXTRACTION_FAILED='无法读取视频内的帧时间附件',
        SYNC_ATTACHMENT_CHECKSUM='帧时间索引校验不一致', SYNC_VIDEO_UNAVAILABLE='本地视频不可读取',
        SYNC_INDEX_EMPTY='帧时间索引为空', SYNC_INDEX_CAPACITY='帧时间索引行数或行长度超过处理上限',
        SYNC_BATCH_FRAME_CAPACITY='本次同步帧数超过处理上限',
        SYNC_SESSION_SOURCE_CAPACITY='同一采集会话的来源片段超过处理上限，未截断生成同步结果',
        SYNC_CURSOR_INVALID='同步处理进度文件无效，未覆盖已有进度',
        SYNC_OUTPUT_CAPACITY='本次同步结果超过 256 MiB 上限',
        SYNC_EXISTING_INDEX_CONFLICT='已有同步索引与原记录不一致，已保留原件',
        SYNC_CLOCK_NON_MONOTONIC='采集主机时间出现回退，未自动拼接为同步数据',
        SYNC_TIMESTAMP_INVALID='帧时间戳无效', SYNC_VIDEO_PTS_INVALID='帧播放时间索引无效',
        SYNC_SOURCE_LINK='来源路径含软链接', SYNC_SOURCE_CHANGED='读取期间来源文件发生变化')
    for error in report['errors']:
        error['description'] = error_descriptions.get(error['code'], '同步索引生成失败；原视频保持可上传')
    return report
