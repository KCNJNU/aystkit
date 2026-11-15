"""Seal new ROS bag segments as byte-exact Zstandard file-compressed bags.

Only an explicit bag-pending.json is processed. The original bag and its YAML
remain unchanged until the independent, confirmed-copy retention path runs.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from .archive_core import MAX_FILE_BYTES
from .archive_policy import allowed_archive_manifest, allowed_manifest_path

MAX_MANIFEST = 256 * 1024
MAX_SOURCE = MAX_FILE_BYTES  # Exact integer protocol bound; no fixed2GiB file ceiling.
MAX_FILES = 4096
CHUNK = 128 * 1024
RAW_DATABASE = re.compile(r'bag/([A-Za-z0-9_-]+\.db3)')
OUTPUT_DATABASE = re.compile(r'compressed-bag/([A-Za-z0-9_-]+\.db3\.zst)')


class BagCompressionError(RuntimeError):
    """A compressed derivative is not ready; its sealed source remains intact."""


class BagCompressionBusy(BagCompressionError):
    pass


def _discard_verified_sources(segment, files):
    """Release raw db3/YAML only after the compressed manifest is durable."""
    if os.environ.get('ARCHIVE_DISCARD_RAW_AFTER_DERIVATIVE') != '1':
        return
    for name in files:
        path = segment / name
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            continue
    for parent in sorted({(segment / name).parent for name in files}, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise BagCompressionError('bag_compression_timeout')
    return left


def _segment(path):
    path = Path(path)
    if '..' in path.parts:
        raise BagCompressionError('bag_segment_path_invalid')
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise BagCompressionError('bag_segment_path_invalid')
    if path.stat().st_uid != os.geteuid():
        raise BagCompressionError('bag_segment_owner_invalid')
    return path


def _safe_file(segment, name, *, limit=MAX_SOURCE):
    if (not isinstance(name, str) or not name or '\\' in name or Path(name).is_absolute()
            or str(Path(name)) != name or any(part in ('.', '..') or any(ord(c) < 32 for c in part)
                                            for part in Path(name).parts)):
        raise BagCompressionError('bag_source_path_invalid')
    current = segment
    for part in Path(name).parts:
        current /= part
        if current.is_symlink():
            raise BagCompressionError('bag_source_link')
    info = current.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or not 0 < info.st_size <= limit):
        raise BagCompressionError('bag_source_file_invalid')
    return current


def _open_file(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise BagCompressionError('bag_source_file_invalid')
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _json(path, *, limit=MAX_MANIFEST):
    with _open_file(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise BagCompressionError('bag_manifest_too_large')
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise BagCompressionError('bag_manifest_invalid') from error
    if not isinstance(result, dict):
        raise BagCompressionError('bag_manifest_invalid')
    return result


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _source_record(path, deadline):
    digest, size = hashlib.sha256(), 0
    with _open_file(path) as stream:
        before = os.fstat(stream.fileno())
        for block in iter(lambda: stream.read(CHUNK), b''):
            _remaining(deadline)
            digest.update(block); size += len(block)
            if size > MAX_SOURCE:
                raise BagCompressionError('bag_source_too_large')
        after = os.fstat(stream.fileno())
    if _fingerprint(before) != _fingerprint(after) or size != after.st_size:
        raise BagCompressionError('bag_source_changed')
    return {'sha256': digest.hexdigest(), 'size': size, 'fingerprint': _fingerprint(after)}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write('\n'); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path); _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _segment_lock(segment):
    descriptor = os.open(segment / '.bag-compress.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise BagCompressionError('bag_compression_lock_invalid')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BagCompressionBusy('bag_compressor_busy') from error
        yield
    finally:
        os.close(descriptor)


def _clean_interrupted_work(segment):
    for directory in segment.glob('.bag-compress-working-*'):
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.geteuid():
            raise BagCompressionError('bag_compression_staging_unsafe')
        for path in directory.iterdir():
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or not (path.name == 'metadata.yaml' or re.fullmatch(r'[A-Za-z0-9_-]+\.db3\.zst', path.name))):
                raise BagCompressionError('bag_compression_staging_unsafe')
        shutil.rmtree(directory)


def _metadata(segment, databases):
    try:
        import yaml
    except ImportError as error:
        raise BagCompressionError('bag_yaml_missing') from error
    path = _safe_file(segment, 'bag/metadata.yaml', limit=MAX_MANIFEST)
    with _open_file(path) as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    try:
        metadata = yaml.safe_load(raw)
        value = metadata['rosbag2_bagfile_information']
        names = [Path(name).name for name in databases]
        if (not isinstance(metadata, dict) or not isinstance(value, dict)
                or value.get('storage_identifier') != 'sqlite3'
                or value.get('compression_format', '') not in ('', None)
                or value.get('compression_mode', '') not in ('', None)
                or not isinstance(value.get('relative_file_paths'), list)
                or len(value['relative_file_paths']) != len(names)
                or set(value['relative_file_paths']) != set(names)
                or type(value.get('message_count')) is not int or value['message_count'] <= 0):
            raise ValueError()
        actual_count = 0
        for name in databases:
            path = _safe_file(segment, name)
            with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as database:
                count = database.execute('SELECT count(*) FROM messages').fetchone()[0]
                if type(count) is not int or count < 0:
                    raise ValueError()
                actual_count += count
        if actual_count != value['message_count']:
            raise ValueError()
        rewritten = copy.deepcopy(metadata)
        output = rewritten['rosbag2_bagfile_information']
        output.update(compression_format='zstd', compression_mode='file',
                      relative_file_paths=[name + '.zst' for name in value['relative_file_paths']])
        if 'files' in output:
            if (not isinstance(output['files'], list) or len(output['files']) != len(names)
                    or any(not isinstance(row, dict) or row.get('path') not in names for row in output['files'])
                    or len({row['path'] for row in output['files']}) != len(names)):
                raise ValueError()
            for row in output['files']:
                row['path'] += '.zst'
        encoded = yaml.safe_dump(rewritten, sort_keys=False, allow_unicode=True).encode()
        if len(encoded) > MAX_MANIFEST:
            raise ValueError()
        return rewritten, encoded, actual_count
    except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, yaml.YAMLError) as error:
        raise BagCompressionError('bag_metadata_or_message_count_invalid') from error


def _compress(source, destination, expected, executable, level, deadline):
    with _open_file(source) as raw, destination.open('xb') as output:
        os.chmod(destination, 0o600)
        try:
            result = subprocess.run([executable, '-q', '-T1', '-' + str(level), '--check',
                '--stream-size=' + str(expected['size']), '-c'], stdin=raw,
                stdout=output, stderr=subprocess.PIPE, timeout=_remaining(deadline), check=False)
        except subprocess.TimeoutExpired as error:
            raise BagCompressionError('bag_compression_timeout') from error
        if result.returncode:
            raise BagCompressionError('bag_zstd_compression_failed')
        output.flush(); os.fsync(output.fileno())


def _verify_compressed(path, expected, executable, deadline):
    """Stream the entire decompressed DB through SHA256 without writing a copy."""
    digest, size = hashlib.sha256(), 0
    left = _remaining(deadline)
    process = subprocess.Popen([executable, '-q', '-d', '-c', '-M128MB', '--', str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(left, process.kill); timer.daemon = True; timer.start()
    try:
        for block in iter(lambda: process.stdout.read(CHUNK), b''):
            _remaining(deadline)
            size += len(block)
            if size > expected['size']:
                raise BagCompressionError('bag_decompression_mismatch')
            digest.update(block)
        result = process.wait(timeout=_remaining(deadline))
        if result or (size, digest.hexdigest()) != (expected['size'], expected['sha256']):
            raise BagCompressionError('bag_decompression_mismatch')
    except subprocess.TimeoutExpired as error:
        raise BagCompressionError('bag_compression_timeout') from error
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()


def _existing(segment, ready, deadline):
    published = _json(ready)
    metadata = published.get('metadata', {})
    rows = metadata.get('compressedFiles') if isinstance(metadata, dict) else None
    if (not isinstance(metadata, dict) or metadata.get('bagCompressionReady') is not True
            or not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows)
            or published.get('files') != [row.get('relativePath') for row in rows]):
        raise BagCompressionError('bag_existing_archive_conflict')
    for row in rows:
        name = row.get('relativePath')
        if name != 'compressed-bag/metadata.yaml' and not (isinstance(name, str) and OUTPUT_DATABASE.fullmatch(name)):
            raise BagCompressionError('bag_existing_archive_conflict')
        if (segment / name).is_symlink():
            raise BagCompressionError('bag_source_link')
    missing = [row for row in rows if not (segment / row['relativePath']).exists()]
    if missing:
        ack = _json(_safe_file(segment, 'uploaded.json', limit=MAX_MANIFEST))
        pruned = _json(_safe_file(segment, 'local-copy-pruned.json', limit=MAX_MANIFEST))
        acknowledged = {row.get('relativePath'): row for row in ack.get('files', [])}
        manifest_digest = _source_record(ready, deadline)['sha256']
        if (ack.get('state') != 'complete' or pruned.get('state') not in ('pruning', 'pruned')
                or ack.get('manifestSha256') != manifest_digest
                or set(acknowledged) != set(published['files'])
                or any((acknowledged[row['relativePath']].get('sha256'), acknowledged[row['relativePath']].get('size'))
                       != (row.get('sha256'), row.get('size')) for row in rows)):
            raise BagCompressionError('bag_existing_archive_missing')
    for row in rows:
        if not (segment / row['relativePath']).exists():
            continue
        path = _safe_file(segment, row['relativePath'])
        actual = _source_record(path, deadline)
        if (actual['sha256'], actual['size']) != (row.get('sha256'), row.get('size')):
            raise BagCompressionError('bag_existing_archive_checksum')
    return published


def finalize_pending(segment, *, zstd='zstd', timeout_s=120, reserve_bytes=10 * 1024**3,
                     compression_level=3):
    """Publish a verified derivative of an explicitly sealed bag, never active data."""
    if (not math.isfinite(timeout_s) or timeout_s <= 0 or reserve_bytes < 0
            or type(compression_level) is not int or not 1 <= compression_level <= 9):
        raise ValueError('invalid bag compression limits')
    segment = _segment(segment)
    deadline = time.monotonic() + timeout_s
    with _segment_lock(segment):
        ready = segment / 'archive.json'
        if ready.exists() or ready.is_symlink():
            return _existing(segment, _safe_file(segment, 'archive.json', limit=MAX_MANIFEST), deadline)
        manifest = _json(_safe_file(segment, 'bag-pending.json', limit=MAX_MANIFEST))
        metadata, files = manifest.get('metadata', {}), manifest.get('files')
        if (manifest.get('schemaVersion') != 1 or not isinstance(metadata, dict)
                or metadata.get('scope') != 'garage' or metadata.get('source') != 'native-ros-cdr'
                or metadata.get('bagCompressionRequired') is not True
                or not isinstance(files, list) or not 2 <= len(files) <= MAX_FILES
                or any(not isinstance(name, str) for name in files) or len(set(files)) != len(files)
                or files.count('bag/metadata.yaml') != 1
                or any(name != 'bag/metadata.yaml' and not RAW_DATABASE.fullmatch(name) for name in files)):
            raise BagCompressionError('bag_pending_manifest_invalid')
        executable = shutil.which(zstd)
        if executable is None:
            raise BagCompressionError('bag_zstd_missing')
        sources = {name: _safe_file(segment, name) for name in files}
        if sum(path.stat().st_size for path in sources.values()) > MAX_SOURCE:
            raise BagCompressionError('bag_source_too_large')
        before = {name: _source_record(path, deadline) for name, path in sources.items()}
        expected = metadata.get('sourceFiles')
        if expected is not None:
            if (not isinstance(expected, list) or len(expected) != len(before)
                    or any(not isinstance(row, dict) for row in expected)):
                raise BagCompressionError('bag_source_manifest_checksum')
            recorded = {row.get('relativePath'): row for row in expected}
            if (set(recorded) != set(before) or any(
                (recorded[name].get('size'), recorded[name].get('sha256')) != (row['size'], row['sha256'])
                for name, row in before.items())):
                raise BagCompressionError('bag_source_manifest_checksum')
        databases = [name for name in files if name != 'bag/metadata.yaml']
        rewritten, encoded_metadata, count = _metadata(segment, databases)
        _clean_interrupted_work(segment)
        source_bytes = sum(row['size'] for row in before.values())
        if shutil.disk_usage(segment).free - source_bytes * 2 <= reserve_bytes:
            raise BagCompressionError('bag_compression_insufficient_working_space')
        target, work = segment / 'compressed-bag', None
        output_names = [Path(name).name + '.zst' for name in databases] + ['metadata.yaml']
        try:
            if target.exists() or target.is_symlink():
                if (target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.geteuid()
                        or {p.name for p in target.iterdir()} != set(output_names)):
                    raise BagCompressionError('bag_existing_derivative_conflict')
                import yaml
                with _open_file(_safe_file(segment, 'compressed-bag/metadata.yaml', limit=MAX_MANIFEST)) as stream:
                    if yaml.safe_load(stream.read(MAX_MANIFEST + 1)) != rewritten:
                        raise BagCompressionError('bag_existing_derivative_metadata')
                destination = target
            else:
                work = Path(tempfile.mkdtemp(prefix='.bag-compress-working-', dir=segment))
                destination = work
                for name in databases:
                    output = work / (Path(name).name + '.zst')
                    _compress(sources[name], output, before[name], executable, compression_level, deadline)
                with (work / 'metadata.yaml').open('xb') as output:
                    os.chmod(work / 'metadata.yaml', 0o600)
                    output.write(encoded_metadata); output.flush(); os.fsync(output.fileno())
                _sync_directory(work)
            for name in databases:
                path = destination / (Path(name).name + '.zst')
                _safe_file(destination, path.name)
                _verify_compressed(path, before[name], executable, deadline)
            for name, path in sources.items():
                if _source_record(path, deadline) != before[name]:
                    raise BagCompressionError('bag_source_changed')
            compressed_files = []
            for name in output_names:
                path = _safe_file(destination, name)
                row = _source_record(path, deadline)
                compressed_files.append({'relativePath': 'compressed-bag/' + name,
                                         'sha256': row['sha256'], 'size': row['size']})
            compressed_bytes = sum(row['size'] for row in compressed_files)
            published = {**manifest, 'files': [row['relativePath'] for row in compressed_files],
                'metadata': {**metadata, 'format': 'rosbag2/sqlite3+zstd', 'bagCompressionReady': True,
                    'compressionFormat': 'zstd', 'compressionMode': 'file', 'compressionLevel': compression_level,
                    'compressionThreads': 1, 'losslessSourceBytes': True, 'receivedMessages': count,
                    'compressedBytes': compressed_bytes, 'compressionOriginalBytes': source_bytes,
                    'compressedFiles': compressed_files,
                    'sourceFiles': [{'relativePath': name, 'size': row['size'], 'sha256': row['sha256']}
                                    for name, row in before.items()]}}
            if work is not None:
                os.rename(work, target); work = None; _sync_directory(segment)
            _atomic_json(ready, published)
            _discard_verified_sources(segment, files)
            return published
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError, ImportError) as error:
            raise BagCompressionError('bag_compression_failed:' + type(error).__name__) from error
        finally:
            if work is not None:
                shutil.rmtree(work)


class BagCompressionFinalizer:
    """Bounded scheduling over explicit new sealed bags; archive.json is immutable."""
    def __init__(self, root, robot_id, *, camera_only=False, **options):
        if type(camera_only) is not bool:
            raise ValueError('camera_only must be a boolean')
        self.root, self.robot_id, self.options = Path(root), robot_id, options
        self.camera_only = camera_only
        self.counters, self.errors = Counter(), {}
        self.lock = threading.Lock()
        self.cursor = 0
        self.deferred = set()
        self.discovery_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.pending_cache = None
        self.pending_scanned_at = float('-inf')
        self.selected_error_keys = frozenset()

    def pending_candidates(self, *, force=False):
        with self.discovery_lock:
            now = time.monotonic()
            if force or self.pending_cache is None or now - self.pending_scanned_at >= 5:
                self.pending_cache = tuple(self._candidates())
                self.pending_scanned_at = time.monotonic()
            else:
                self.pending_cache = tuple(path for path in self.pending_cache
                    if path.is_file() and not (path.parent/'archive.json').exists())
            return self.pending_cache

    def refresh_pending(self):
        """Bypass discovery throttling for a newly closed capture at stop."""
        return self.pending_candidates(force=True)

    def _completed(self, pending):
        with self.discovery_lock:
            if self.pending_cache is not None:
                self.pending_cache = tuple(path for path in self.pending_cache if path != pending)

    def _candidates(self):
        days = self.root / 'days'
        if self.root.is_symlink() or days.is_symlink() or not days.is_dir():
            return []
        return [p for p in sorted(days.rglob('bag-pending.json'))
                if not (p.parent / 'archive.json').exists()
                and allowed_manifest_path(p, camera_only=self.camera_only)]

    def _selected_errors(self):
        errors = self.errors.copy()
        if not self.camera_only:
            return errors
        days = self.root/'days'
        selected = {key: error for key, error in errors.items()
                    if any(allowed_manifest_path(days/key/name, camera_only=True)
                           for name in ('bag-pending.json', 'archive.json'))}
        self.selected_error_keys = frozenset(selected)
        return selected

    def run_once(self, max_segments=4):
        if type(max_segments) is not int or not 1 <= max_segments <= 4:
            raise ValueError('invalid bag compression batch size')
        if not self.lock.acquire(blocking=False):
            return 0
        done = 0
        try:
            days = self.root / 'days'
            pending = self.pending_candidates()
            with self.state_lock:
                candidates = [path for path in pending
                              if str(path.parent.relative_to(days)) not in self.deferred]
                selected_errors = self._selected_errors()
                error_keys = tuple(self.errors)
            for key in error_keys:
                if key not in selected_errors:
                    with self.state_lock:
                        self.errors.pop(key, None)
                    continue
                ready = days / key / 'archive.json'
                try:
                    if ready.is_symlink() or not ready.is_file():
                        continue
                    published = _json(ready)
                    if (published.get('robotId') == self.robot_id and published.get('id') == ready.parent.name
                            and published.get('metadata', {}).get('bagCompressionReady') is True):
                        with self.state_lock:
                            self.errors.pop(key, None)
                            self.deferred.discard(key)
                except (BagCompressionError, OSError, ValueError, TypeError):
                    pass
            count = len(candidates)
            for step in range(min(max_segments, count)):
                pending = candidates[(self.cursor + step) % count]
                key = str(pending.parent.relative_to(days))
                try:
                    manifest = _json(_safe_file(pending.parent, 'bag-pending.json', limit=MAX_MANIFEST))
                    if not allowed_archive_manifest(manifest, camera_only=self.camera_only):
                        continue
                    if manifest.get('robotId') != self.robot_id or manifest.get('id') != pending.parent.name:
                        raise BagCompressionError('bag_robot_or_segment_identity_mismatch')
                    finalize_pending(pending.parent, **self.options)
                    self._completed(pending)
                    done += 1
                    with self.state_lock:
                        self.counters['finalizedSegments'] += 1
                        self.errors.pop(key, None)
                except BagCompressionBusy:
                    with self.state_lock:
                        self.counters['busySegments'] += 1
                except (BagCompressionError, OSError, ValueError, TypeError) as error:
                    with self.state_lock:
                        self.counters['failedAttempts'] += 1
                        self.errors[key] = str(error)
                        if any(code in str(error) for code in (
                                'bag_source_file_invalid', 'bag_source_path_invalid',
                                'bag_source_link', 'bag_manifest_invalid',
                                'bag_pending_manifest_invalid',
                                'bag_metadata_or_message_count_invalid',
                                'bag_yaml_missing')):
                            self.deferred.add(key)
            if count:
                self.cursor = (self.cursor + min(max_segments, count)) % count
            return done
        finally:
            self.lock.release()

    def status(self, *, cached=False):
        if cached:
            # Only immutable discovery references and in-memory copies: neither
            # the discovery lock nor the policy/state lock may delay telemetry.
            pending = self.pending_cache
            counters, errors, deferred = dict(self.counters), self.errors.copy(), self.deferred.copy()
            keys = frozenset(str(path.parent.relative_to(self.root/'days'))
                             for path in pending or ())
            if self.camera_only:
                selected = keys | self.selected_error_keys
                errors = {key: error for key, error in errors.items() if key in selected}
            return {**counters, 'pendingSegments': None if pending is None else len(pending),
                    'terminalPendingSegments': len(keys & deferred),
                    'errors': dict(list(errors.items())[-16:]), 'running': self.lock.locked(),
                    'discoveryReady': pending is not None,
                    'discoveryAgeS': None if pending is None else
                        max(0, time.monotonic()-self.pending_scanned_at)}
        pending = self.pending_candidates()
        with self.state_lock:
            terminal = sum(str(path.parent.relative_to(self.root/'days')) in self.deferred
                           for path in pending)
            return {**dict(self.counters), 'pendingSegments': len(pending),
                    'terminalPendingSegments': terminal,
                    'errors': dict(list(self._selected_errors().items())[-16:]), 'running': self.lock.locked()}
