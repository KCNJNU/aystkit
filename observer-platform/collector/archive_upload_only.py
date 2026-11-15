"""Bounded sealed-file sync/upload; no ROS, capture gates, services or source pruning."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager, nullcontext
from datetime import datetime
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from uuid import UUID
from zoneinfo import ZoneInfo

from .archive_upload import (ArchiveUploadClient, ArchiveUploadConfig,
                             ArchiveUploadError, MAX_RESPONSE_BYTES,
                             DEFAULT_UPLOAD_BYTES_PER_SECOND, upload_rate)


class UploadDeadlineExpired(BaseException):
    """Not caught by HTTP/OSError recovery: the foreground time budget expired."""


def load_environment(path, environ=None):
    """Parse literal KEY=value data, never source or expand a shell command."""
    values = {}
    path = Path(path)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ArchiveUploadError('ARCHIVE_ENV_INVALID')
        for line in path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            key, sep, raw = stripped.partition('=')
            if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                raise ArchiveUploadError('ARCHIVE_ENV_INVALID')
            try:
                fields = shlex.split(raw, comments=True, posix=True)
            except ValueError:
                raise ArchiveUploadError('ARCHIVE_ENV_INVALID') from None
            if len(fields) > 1:
                raise ArchiveUploadError('ARCHIVE_ENV_INVALID')
            if key.startswith('ARCHIVE_'):
                values[key] = fields[0] if fields else ''
    values.update({key: value for key, value in
                   (os.environ if environ is None else environ).items()
                   if key.startswith('ARCHIVE_')})
    return values


def upload_config(values):
    data = {}
    if values.get('ARCHIVE_UPLOAD_CONFIG'):
        path = Path(values['ARCHIVE_UPLOAD_CONFIG'])
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_size > 16384):
            raise ArchiveUploadError('ARCHIVE_CONFIG_MUST_BE_PRIVATE')
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ArchiveUploadError('ARCHIVE_CONFIG_INVALID')
    return ArchiveUploadConfig(
        values.get('ARCHIVE_DBPROG_EMAIL', data.get('email', '')),
        values.get('ARCHIVE_DBPROG_PASSWORD', data.get('password', '')),
        values.get('ARCHIVE_DBPROG_API_URL', data.get('apiUrl', ArchiveUploadConfig.api_url)),
        values.get('ARCHIVE_DATASET_PREFIX', data.get('datasetPrefix', ArchiveUploadConfig.dataset_prefix)),
        values.get('ARCHIVE_ROBOT_ID', data.get('robotId', '')),
        upload_rate(values.get('ARCHIVE_UPLOAD_BYTES_PER_SECOND',
            data.get('uploadBytesPerSecond', DEFAULT_UPLOAD_BYTES_PER_SECOND))))


def sealed_manifests(root):
    """Walk only finalized days, excluding symlinked trees and staging files."""
    days = root / 'days'
    if days.is_symlink() or (days.exists() and not days.is_dir()):
        raise ArchiveUploadError('ARCHIVE_LOCAL_SCAN_FAILED')
    if not days.exists():
        return []
    result = []
    def scan_error(_error):
        raise ArchiveUploadError('ARCHIVE_LOCAL_SCAN_FAILED')
    for directory, children, files in os.walk(days, followlinks=False, onerror=scan_error):
        parent = Path(directory)
        children[:] = sorted(name for name in children if not (parent / name).is_symlink())
        if 'archive.json' in files:
            path = parent / 'archive.json'
            result.append(path)
    return sorted(result)


def unfinished_counts(root):
    """Report local material that an upload-only process cannot seal or recover."""
    counts = dict(preparingSegments=0, activeSegments=0, stagingSegments=0, recoverySegments=0)
    days = root / 'days'
    def scan_error(_error):
        raise ArchiveUploadError('ARCHIVE_LOCAL_SCAN_FAILED')
    if days.is_symlink() or (days.exists() and not days.is_dir()):
        raise ArchiveUploadError('ARCHIVE_LOCAL_SCAN_FAILED')
    if days.exists():
        for directory, children, files in os.walk(days, followlinks=False, onerror=scan_error):
            parent = Path(directory)
            children[:] = [name for name in children if not (parent / name).is_symlink()]
            if 'archive.json' not in files and any(
                    name in files for name in ('video-pending.json', 'bag-pending.json')):
                counts['preparingSegments'] += 1
    for name, key in (('.active', 'activeSegments'), ('.camera-active', 'activeSegments'),
                      ('.incoming', 'stagingSegments'), ('.camera-recovering', 'stagingSegments'),
                      ('.staging', 'stagingSegments'), ('recovery', 'recoverySegments')):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_dir():
            raise ArchiveUploadError('ARCHIVE_LOCAL_SCAN_FAILED')
        with os.scandir(path) as entries:
            counts[key] += sum(1 for _entry in entries)
    return counts


def _robot_identity(value):
    try:
        if not isinstance(value, str) or not value:
            raise ValueError()
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_INVALID') from None


def _expected_identity(values, expected_robot_id, *, dry_run):
    """Bind public identity without opening upload credentials during dry-run."""
    supplied = _robot_identity(expected_robot_id) if expected_robot_id is not None else None
    configured = values.get('ARCHIVE_ROBOT_ID')
    if configured:
        configured = _robot_identity(configured)
    elif not dry_run:
        configured = _robot_identity(upload_config(values).robot_id)
    if supplied and configured and supplied != configured:
        raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_MISMATCH')
    return supplied or configured or None


def _bound_upload_config(values, expected_robot_id):
    config = upload_config(values)
    if _robot_identity(config.robot_id) != expected_robot_id:
        raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_MISMATCH')
    return config


def acknowledged(manifest, expected_robot_id=None):
    if (manifest.is_symlink() or not manifest.is_file()
            or manifest.stat().st_size > MAX_RESPONSE_BYTES):
        raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
    raw = manifest.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID') from None
    if not isinstance(data, dict) or data.get('schemaVersion') != 1:
        raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
    if expected_robot_id is not None:
        if _robot_identity(data.get('robotId')) != _robot_identity(expected_robot_id):
            raise ArchiveUploadError('ARCHIVE_ROBOT_IDENTITY_MISMATCH')
    path = manifest.parent / 'uploaded.json'
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ArchiveUploadError('ARCHIVE_STATE_INVALID')
    try:
        ack = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, RecursionError):
        raise ArchiveUploadError('ARCHIVE_STATE_INVALID') from None
    if (not isinstance(ack, dict) or ack.get('state') != 'complete'
            or ack.get('manifestSha256') != hashlib.sha256(raw).hexdigest()):
        raise ArchiveUploadError('ARCHIVE_MANIFEST_CHANGED')
    return True


@contextmanager
def upload_lock(root):
    descriptor = os.open(root / '.upload-only.lock',
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ArchiveUploadError('ARCHIVE_UPLOAD_LOCK_INVALID')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveUploadError('ARCHIVE_FOREGROUND_UPLOAD_BUSY') from None
        yield
    finally:
        os.close(descriptor)


@contextmanager
def time_budget(seconds):
    """Bound hashing and HTTP calls, retaining each durable resume checkpoint."""
    def expire(_signal, _frame):
        raise UploadDeadlineExpired()
    previous = signal.signal(signal.SIGALRM, expire)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0] or previous_timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def run_upload(root, values, *, timeout=600, dry_run=False, client_factory=ArchiveUploadClient,
               sync_source=False, source_factory=None, progress=None, processed_folder=None,
               expected_robot_id=None):
    report = dict(state='incomplete', scope='local-sealed-snapshot', remoteSync='not_checked',
                  selectedSegments=0, completedSegments=0, pendingSegments=0, invalidSegments=0,
                  preparingSegments=0, activeSegments=0, stagingSegments=0, recoverySegments=0,
                  alreadyCompletedSegments=0, uploadedThisRun=0, lastError=None)
    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 3600):
            raise ArchiveUploadError('ARCHIVE_UPLOAD_TIMEOUT_INVALID')
        # Inventory and ACK hashing can dominate a large cache. The same
        # deadline covers those reads, credentials, locks and HTTP requests.
        with time_budget(timeout):
            deadline = time.monotonic() + timeout
            root = Path(root).expanduser()
            if root.is_symlink() or not root.is_dir():
                raise ArchiveUploadError('ARCHIVE_LOCAL_ROOT_UNAVAILABLE')
            root = root.resolve()
            expected_robot_id = _expected_identity(values, expected_robot_id, dry_run=True)
            if sync_source and not dry_run:
                expected_robot_id = _expected_identity(values, expected_robot_id, dry_run=False)
                return _source_upload(root, values, report, deadline, client_factory,
                                      source_factory, progress, processed_folder, expected_robot_id)
            report.update(unfinished_counts(root))
            manifests = sealed_manifests(root)
            if manifests and not dry_run:
                expected_robot_id = _expected_identity(values, expected_robot_id, dry_run=False)
            report['selectedSegments'] = report['pendingSegments'] = len(manifests)
            pending = []
            for manifest in manifests:
                try:
                    if acknowledged(manifest, expected_robot_id) and expected_robot_id is not None:
                        report['completedSegments'] += 1
                        report['alreadyCompletedSegments'] += 1
                        report['pendingSegments'] -= 1
                    else:
                        pending.append(manifest)
                except ArchiveUploadError as error:
                    report['invalidSegments'] += 1
                    report['lastError'] = error.code
                except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
                    report['invalidSegments'] += 1
                    report['lastError'] = 'ARCHIVE_LOCAL_UPLOAD_INVALID'
            if dry_run:
                # No credentials, API calls, lock files or other writes in dry-run.
                if report['invalidSegments']:
                    return report, 3
                report['state'] = 'dry_run'
                return report, 0
            if pending:
                pending.sort(key=lambda path: (-recording_priority(path), str(path)))
                config = _bound_upload_config(values, expected_robot_id)
                with upload_lock(root):
                    _upload_pending(config, pending, report, deadline, client_factory)
            _best_effort_session_ends(root, values, manifests, report, client_factory, deadline=deadline)
            _best_effort_time_sync(root, values, manifests, report, client_factory,
                                  expected_robot_id=expected_robot_id, deadline=deadline)
            _best_effort_processed_folder(root, values, manifests, report, client_factory,
                                          processed_folder, deadline=deadline)
            if (not report['pendingSegments'] and not report['invalidSegments'] and not any(report[key] for key in
                    ('preparingSegments', 'activeSegments', 'stagingSegments'))):
                report['state'] = 'complete'
                return report, 0
            if report['lastError'] is None:
                report['lastError'] = 'ARCHIVE_LOCAL_SEALING_PENDING'
            return report, 3
    except UploadDeadlineExpired:
        report['lastError'] = 'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED'
    except KeyboardInterrupt:
        report['lastError'] = 'ARCHIVE_UPLOAD_INTERRUPTED'
        return report, 130
    except ArchiveUploadError as error:
        report['lastError'] = error.code
    except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        # Never print exception text: config/HTTP data may contain credentials.
        report['lastError'] = 'ARCHIVE_LOCAL_UPLOAD_INVALID'
    return report, 3


def _session_control(values):
    return Path(values.get('ARCHIVE_CONTROL_DIR',
        str(Path.home()/'DBProg/RobotHygienism/.runtime/garage-archive-control')))


def _time_sync_candidates(root, values, manifests, robot_id, result):
    """Require durable local session closure; never infer it from a video ACK."""
    selected = []
    def skip(reason):
        result['skips'][reason] = result['skips'].get(reason, 0) + 1
    for path in manifests:
        try:
            path = Path(path)
            path.relative_to(root)
            if (any(parent.is_symlink() for parent in (path, *path.parents))
                    or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES):
                continue
            manifest = json.loads(path.read_bytes())
            metadata = manifest.get('metadata') if isinstance(manifest, dict) else None
            if not isinstance(metadata, dict):
                continue
            usb = metadata.get('source') == 'native-camera-jpeg'
            berxel = (metadata.get('source') == 'native-ros-cdr'
                and manifest.get('sensor') in ('Berxel彩色', 'Berxel深度'))
            if not usb and not berxel:
                continue
            if manifest.get('files') != ['video.mkv'] or metadata.get('videoReady') is not True:
                skip('notSealedVideo')
                continue
            if manifest.get('robotId') != robot_id:
                skip('robotMismatch')
                continue
            token = metadata.get('captureSessionId')
            if not isinstance(token, str) or re.fullmatch(r'[a-f0-9]{32}', token) is None:
                skip('missingSessionId')
                continue
            domain = metadata.get('clockDomain')
            if (not isinstance(domain, str) or re.fullmatch(
                    r'linux-boot:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', domain) is None
                    or domain[len('linux-boot:'):].replace('-', '') == '0' * 32):
                skip('missingClockDomain')
                continue
            if (metadata.get('clockSource') != 'CLOCK_REALTIME'
                    or metadata.get('timestampSemantics') != (
                        'host_read_complete' if usb else 'host_subscription_receive')):
                skip('unverifiedTimestampSemantics')
                continue
            marker = _session_control(values)/'session-ends'/(token+'.json')
            proof = {}
            if (not any(parent.is_symlink() for parent in (marker, *marker.parents))
                    and marker.is_file() and marker.stat().st_size <= MAX_RESPONSE_BYTES):
                try:
                    proof = json.loads(marker.read_bytes())
                except (OSError, ValueError, TypeError, RecursionError):
                    pass
            if (not isinstance(proof, dict) or proof.get('schemaVersion') != 1
                    or proof.get('state') != 'sealed' or proof.get('sessionId') != token
                    or proof.get('robotId') != robot_id):
                skip('waitingForSessionEnd')
                result['waitingForSessionEnd'] += 1
                continue
            selected.append(path)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
            skip('sourceUnavailable')
    return selected


def _best_effort_time_sync(root, values, manifests, report, client_factory,
                           client=None, deadline=None, expected_robot_id=None):
    """Optional indexes use a separate bounded budget and never change media results."""
    result = dict(state='not_required', generated=0, reused=0, selected=0,
        completed=0, pending=0, waitingForSessionEnd=0, skips={}, errors=[], lastError=None)
    report['timeSync'] = result
    if not manifests:
        return
    try:
        now = time.monotonic()
        sync_deadline = min(deadline, now + 60) if deadline is not None else now + 60
        with _optional_time_budget(sync_deadline):
            identity = expected_robot_id or values.get('ARCHIVE_ROBOT_ID')
            robot_id = _robot_identity(identity) if identity is not None else None
            candidates = _time_sync_candidates(root, values, manifests, robot_id, result)
            if not candidates:
                if result['skips']:
                    result['state'] = 'not_generated'
                return
            from .archive_sync_index import build_sync_indexes
            from .archive_policy import camera_only_enabled, training_only_enabled
            # Source uploading already owns this exact foreground lock. The
            # local path acquires it before generating or uploading indexes.
            with upload_lock(root) if client is None else nullcontext():
                built = build_sync_indexes(candidates, root, robot_id,
                    camera_only=camera_only_enabled(values), training_only=training_only_enabled(values))
                result['generated'], result['reused'] = built['generated'], built['reused']
                for reason, count in built.get('skipped', {}).items():
                    result['skips'][reason] = result['skips'].get(reason, 0) + count
                result['skippedReasons'] = built.get('skippedReasons', [])
                result['errors'].extend(built.get('errors', []))
                ready = list(dict.fromkeys(Path(path) for path in built['readyManifestPaths']))
                result['selected'] = result['pending'] = len(ready)
                pending = []
                for path in ready:
                    if acknowledged(path, robot_id):
                        result['completed'] += 1
                        result['pending'] -= 1
                    else:
                        pending.append(path)
                api_client = client
                while pending:
                    remaining = []
                    for path in pending:
                        if time.monotonic() >= sync_deadline:
                            raise UploadDeadlineExpired()
                        try:
                            if api_client is None:
                                api_client = client_factory(_bound_upload_config(values, robot_id))
                            response = api_client.upload_segment(path.parent)
                            if response.get('state') == 'complete':
                                result['completed'] += 1
                                result['pending'] -= 1
                            else:
                                remaining.append(path)
                        except (Exception, UploadDeadlineExpired) as error:
                            if isinstance(error, UploadDeadlineExpired):
                                raise
                            code = error.code if isinstance(error, ArchiveUploadError) else 'ARCHIVE_TIME_SYNC_UPLOAD_FAILED'
                            result['lastError'] = code
                            result['errors'].append(dict(code=code))
                    pending = remaining
                    if pending:
                        time.sleep(min(1, max(0, sync_deadline - time.monotonic())))
                result['state'] = ('incomplete' if result['pending'] or result['errors']
                    or result['waitingForSessionEnd'] else
                    'complete' if result['selected'] else 'not_generated')
                if result['errors'] and result['lastError'] is None:
                    result['lastError'] = 'ARCHIVE_TIME_SYNC_BUILD_FAILED'
    except (Exception, UploadDeadlineExpired) as error:
        result['state'] = 'incomplete'
        result['lastError'] = ('ARCHIVE_TIME_SYNC_TIME_BUDGET_EXPIRED'
            if isinstance(error, UploadDeadlineExpired) else
            error.code if isinstance(error, ArchiveUploadError) else 'ARCHIVE_TIME_SYNC_FAILED')


@contextmanager
def _optional_time_budget(deadline):
    """Re-arm a remaining budget after an optional step consumed SIGALRM."""
    if deadline is None:
        yield
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise UploadDeadlineExpired()
    with time_budget(remaining):
        yield


def _best_effort_processed_folder(root, values, manifests, report, client_factory,
                                  folder_name, client=None, deadline=None):
    """Add a manual processing destination without changing media upload ACKs."""
    if folder_name is None or not manifests:
        return
    try:
        from .archive_upload_folders import ensure_processed_folders
        def create(api_client):
            api = getattr(api_client, 'api', None)
            previous_timeout = getattr(api, 'timeout', None)
            try:
                if previous_timeout is not None:
                    api.timeout = min(previous_timeout, 2)
                report['processedFolder'] = ensure_processed_folders(
                    api_client, manifests, folder_name)
            finally:
                if previous_timeout is not None:
                    api.timeout = previous_timeout
        with _optional_time_budget(deadline):
            if client is not None:
                create(client)
            else:
                with upload_lock(root):
                    create(client_factory(upload_config(values)))
    except (Exception, UploadDeadlineExpired) as error:
        report['processedFolder'] = dict(name=folder_name, createdDatasets=0,
            pendingDatasets=None, lastError=('ARCHIVE_PROCESSED_FOLDER_TIME_BUDGET_EXPIRED'
                if isinstance(error, UploadDeadlineExpired) else
                error.code if isinstance(error, ArchiveUploadError) else
                'ARCHIVE_PROCESSED_FOLDER_FAILED'))


def _session_ends_required(values, manifests):
    """Inspect only the frozen metadata snapshot; do not scan media trees again."""
    if not manifests:
        return False
    directory = _session_control(values)/'session-ends'
    if directory.exists() or directory.is_symlink():
        return True
    for path in manifests:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
                continue
            token = (json.loads(path.read_bytes()).get('metadata') or {}).get('captureSessionId')
            if isinstance(token, str) and re.fullmatch(r'[a-f0-9]{32}', token):
                return True
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            continue  # The normal segment validator reports malformed manifests.
    return False


def _best_effort_session_ends(root, values, manifests, report, client_factory, client=None,
                             deadline=None):
    """Optional merge metadata must never change video success or its error."""
    try:
        def maintain(api_client):
            api = getattr(api_client, 'api', None)
            previous_timeout = getattr(api, 'timeout', None)
            try:
                if previous_timeout is not None:
                    api.timeout = min(previous_timeout, 2)
                _upload_session_ends(root, values, manifests, report, api_client)
            finally:
                if previous_timeout is not None:
                    api.timeout = previous_timeout
        with _optional_time_budget(deadline):
            if not _session_ends_required(values, manifests):
                return
            if client is not None:
                # Source upload already owns the foreground lock and client.
                maintain(client)
            else:
                with upload_lock(root):
                    maintain(client_factory(upload_config(values)))
    except (Exception, UploadDeadlineExpired) as error:
        # Keep optional metadata diagnostics machine-readable. Never expose
        # exception text, alter media errors, or swallow user interruption.
        result = dict(report.get('sessionEnds') or {})
        result.setdefault('pending', None)
        result.setdefault('acknowledged', None)
        result.setdefault('missing', None)
        result.setdefault('invalid', None)
        result['lastError'] = ('ARCHIVE_SESSION_END_TIME_BUDGET_EXPIRED'
            if isinstance(error, UploadDeadlineExpired) else
            error.code if isinstance(error, ArchiveUploadError) else type(error).__name__)
        report['sessionEnds'] = result


def _upload_session_ends(root, values, manifests, report, client):
    from .archive_session_end import upload_session_ends
    while True:
        previous = (report.get('sessionEnds') or {}).get('acknowledged', -1)
        try:
            result = upload_session_ends(_session_control(values), root, client, manifests=manifests)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError) as error:
            result = dict(pending=1, acknowledged=0, missing=0, invalid=1,
                          lastError=type(error).__name__)
        report['sessionEnds'] = result
        if not result['pending'] and not result['lastError']:
            return
        if (result.get('missing') or result.get('invalid') or result['lastError']
                or result['acknowledged'] <= previous):
            return


def _upload_pending(config, pending, report, deadline, client_factory):
    client = client_factory(config)
    while pending:
        remaining = []
        for manifest in pending:
            if time.monotonic() >= deadline:
                raise UploadDeadlineExpired()
            try:
                # Only an API-confirmed complete result counts as stored.
                result = client.upload_segment(manifest.parent)
                if result.get('state') == 'complete':
                    report['completedSegments'] += 1
                    report['uploadedThisRun'] += 1
                    report['pendingSegments'] -= 1
                else:
                    remaining.append(manifest)
            except ArchiveUploadError as error:
                # A malformed segment must not prevent unrelated good files
                # uploading. Retry the failed segment on the next invocation.
                report['lastError'] = error.code
            except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
                report['lastError'] = 'ARCHIVE_LOCAL_UPLOAD_INVALID'
        pending = remaining
        if pending:
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def recording_priority(manifest_path, source_descriptor=None):
    """Compare capture instants across offsets, never transfer time or path text."""
    try:
        if source_descriptor is None:
            path = Path(manifest_path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESPONSE_BYTES:
                return 0
            raw = path.read_bytes()
        else:
            raw = base64.b64decode(source_descriptor['manifest'], validate=True)
            if len(raw) > MAX_RESPONSE_BYTES:
                return 0
        manifest = json.loads(raw)
        try:
            instant = datetime.fromisoformat(manifest['startedAt'].replace('Z', '+00:00'))
            if instant.tzinfo is not None:
                return instant.timestamp()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            pass
        # A date-only historical record has the documented archive timezone.
        day = datetime.strptime(manifest['day'], '%Y-%m-%d')
        return day.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 0  # The normal validator still reports malformed entries.


def _source_upload(root, values, report, deadline, client_factory, source_factory, progress,
                   processed_folder=None, expected_robot_id=None):
    """Upload one fixed local/source snapshot, without source ACK, prune or gate sync."""
    from .archive_source_link import ssh_options
    from .archive_pull import FilePuller, source_descriptor
    from .archive_policy import camera_only_enabled, training_only_enabled
    report.update(scope='local-and-source-sealed-snapshot', remoteSync='checking',
                  sourceSelectedSegments=0, sourcePendingSegments=0, sourceInvalidSegments=0)
    report.update(unfinished_counts(root))
    config = _bound_upload_config(values, expected_robot_id)
    client = client_factory(config)
    source = None
    remote = []
    source_failed = False
    with upload_lock(root):
        try:
            if source_factory is not None:
                source = source_factory(config)
            else:
                options, target = ssh_options(values)
                source = FilePuller(options, target, root, config,
                    camera_only=camera_only_enabled(values),
                    training_only=training_only_enabled(values),
                    min_free_bytes=int(values.get('ARCHIVE_MIN_FREE_BYTES', 10 * 1024**3)),
                    max_spool_bytes=int(values.get('ARCHIVE_MAX_SPOOL_BYTES', 20 * 1024**3)),
                    bytes_per_second=int(values.get('ARCHIVE_TRANSFER_BYTES_PER_SECOND', 2 * 1024**2)))
            remote = source.sealed_snapshot()
            report['sourceSelectedSegments'] = len(remote)
            invalid = getattr(source, 'snapshot_invalid_segments', 0)
            if type(invalid) is not int or not 0 <= invalid <= 2**31 - 1:
                raise ValueError('source snapshot diagnostics invalid')
            report['sourceInvalidSegments'] = invalid
            report['invalidSegments'] += invalid
            if invalid:
                report['lastError'] = 'ARCHIVE_SOURCE_MANIFEST_INVALID'
            report['remoteSync'] = 'syncing'
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            source_failed = True
            report.update(remoteSync='unavailable', sourcePendingSegments=None,
                          lastError='ARCHIVE_SOURCE_SYNC_UNAVAILABLE')
        # Freeze paths once: newly recorded files belong to the next invocation.
        work = {str(path.parent): None for path in sealed_manifests(root)}
        for descriptor in remote:
            try:
                source_descriptor(descriptor, expected_robot_id)
                path = root / 'days' / descriptor['path']
                previous = work.get(str(path))
                if previous is not None and previous['sha256'] != descriptor['sha256']:
                    raise ArchiveUploadError('ARCHIVE_SOURCE_SNAPSHOT_INVALID')
                work[str(path)] = descriptor
            except ArchiveUploadError as error:
                report['sourceInvalidSegments'] += 1
                report['invalidSegments'] += 1
                report['lastError'] = error.code
        report['selectedSegments'] = report['pendingSegments'] = len(work)
        source_missing = {path for path, descriptor in work.items()
                          if descriptor is not None and not (Path(path) / 'archive.json').is_file()}
        report['sourcePendingSegments'] = None if source_failed else len(source_missing)
        pending_paths = []
        # Count durable historical ACKs before a new video can spend seconds
        # transferring. Already stored files never consume the upload queue.
        for path in work:
            manifest = Path(path) / 'archive.json'
            try:
                if not manifest.is_file():
                    pending_paths.append(path)
                    continue
                if any(parent.is_symlink() for parent in manifest.parents):
                    raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                descriptor = work[path]
                if descriptor is not None:
                    if manifest.is_symlink() or manifest.stat().st_size > MAX_RESPONSE_BYTES:
                        raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                    if hashlib.sha256(manifest.read_bytes()).hexdigest() != descriptor['sha256']:
                        source_missing.add(path)
                        raise ArchiveUploadError('ARCHIVE_SOURCE_MANIFEST_CHANGED')
                source_missing.discard(path)
                if acknowledged(manifest, expected_robot_id):
                    report['completedSegments'] += 1
                    report['alreadyCompletedSegments'] += 1
                    report['pendingSegments'] -= 1
                else:
                    pending_paths.append(path)
            except ArchiveUploadError as error:
                report['invalidSegments'] += 1
                report['lastError'] = error.code
            except (OSError, ValueError, TypeError, AttributeError, RecursionError, OverflowError):
                report['invalidSegments'] += 1
                report['lastError'] = 'ARCHIVE_LOCAL_UPLOAD_INVALID'
        if not source_failed:
            report['sourcePendingSegments'] = len(source_missing)
        queue = deque(sorted(pending_paths,
            key=lambda path: (-recording_priority(Path(path) / 'archive.json', work[path]), path)))
        last_progress = -float('inf')
        first_pass = set(pending_paths)
        while queue:
            if time.monotonic() >= deadline:
                raise UploadDeadlineExpired()
            path = Path(queue.popleft())
            descriptor = work[str(path)]
            manifest = path / 'archive.json'
            retry = False
            try:
                if descriptor is not None and not manifest.is_file():
                    if not source._pull(descriptor):
                        retry = True
                if manifest.is_file() and str(path) in source_missing:
                    source_missing.remove(str(path))
                    report['sourcePendingSegments'] = len(source_missing)
                if not retry:
                    if any(parent.is_symlink() for parent in manifest.parents):
                        raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                    # A background pull may have published the path since the
                    # snapshot. Compare again before trusting its ACK or bytes.
                    if descriptor is not None:
                        if manifest.is_symlink() or manifest.stat().st_size > MAX_RESPONSE_BYTES:
                            raise ArchiveUploadError('ARCHIVE_MANIFEST_INVALID')
                        if hashlib.sha256(manifest.read_bytes()).hexdigest() != descriptor['sha256']:
                            source_missing.add(str(path))
                            report['sourcePendingSegments'] = len(source_missing)
                            raise ArchiveUploadError('ARCHIVE_SOURCE_MANIFEST_CHANGED')
                    existing = acknowledged(manifest, expected_robot_id)
                    if existing or client.upload_segment(path).get('state') == 'complete':
                        report['completedSegments'] += 1
                        report['pendingSegments'] -= 1
                        report['uploadedThisRun'] += 1
                    else:
                        retry = True
            except ArchiveUploadError as error:
                report['lastError'] = error.code
                report['invalidSegments'] += 1
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
                    OverflowError, subprocess.SubprocessError):
                report['lastError'] = 'ARCHIVE_SOURCE_SYNC_FAILED' if descriptor is not None else 'ARCHIVE_LOCAL_UPLOAD_INVALID'
                report['invalidSegments'] += 1
            first_pass.discard(str(path))
            if retry:
                queue.append(str(path))
            if progress is not None and time.monotonic() - last_progress >= 2:
                progress(dict(report))
                last_progress = time.monotonic()
            # A busy daemon or pending API job needs time, not a tight loop.
            if retry and (len(queue) == 1 or not first_pass):
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        report['remoteSync'] = 'unavailable' if source_failed else (
            'complete' if report['sourcePendingSegments'] == 0 and not report['invalidSegments'] else 'incomplete')
        # Recount staging after transfers; an empty .incoming/ is not pending.
        report.update(unfinished_counts(root))
        manifests = [Path(path)/'archive.json' for path in work]
        _best_effort_session_ends(root, values, manifests, report, client_factory, client=client,
                                  deadline=deadline)
        _best_effort_time_sync(root, values, manifests, report, client_factory, client=client,
                              expected_robot_id=expected_robot_id, deadline=deadline)
        _best_effort_processed_folder(root, values, manifests, report, client_factory,
                                      processed_folder, client=client, deadline=deadline)
        if (not source_failed and not report['pendingSegments'] and not report['invalidSegments'] and not any(report[key] for key in
                ('preparingSegments', 'activeSegments', 'stagingSegments'))):
            report['state'] = 'complete'
            return report, 0
        if report['lastError'] is None:
            report['lastError'] = 'ARCHIVE_LOCAL_SEALING_PENDING'
        return report, 3


def readable_summary(report):
    """Explain upload scope and unsealed remnants without claiming remote sync."""
    completed, selected = report.get('completedSegments'), report.get('selectedSegments')
    confirmed = (isinstance(selected, int) and selected > 0 and completed == selected
                 and report.get('pendingSegments') == 0 and report.get('invalidSegments') == 0
                 and report.get('remoteSync') in (None, 'not_checked', 'complete'))
    if report.get('state') == 'dry_run':
        lines = ['只读检查已结束，未执行上传。']
    elif confirmed:
        scope_name = '成品' if report.get('scope') == 'local-and-source-sealed-snapshot' else '本机成品'
        lines = [f'本次选中的 {completed} 个{scope_name}均已确认入仓。']
    elif report.get('state') == 'complete':
        lines = ['本次本机检查已完成，没有待上传的已封片成品。']
    else:
        lines = ['本次上传尚未全部完成，未确认数据继续保留在本机。']
    for key, label in (
            ('completedSegments', '已确认成品'), ('pendingSegments', '待上传成品'),
            ('invalidSegments', '无效清单或回执'), ('preparingSegments', '待封片或需处理片段'),
            ('activeSegments', '录制中片段'), ('stagingSegments', '中转暂存片段'),
            ('recoverySegments', '恢复目录条目')):
        lines.append(f'{label}：{report.get(key, "未核对")}')
    if 'uploadedThisRun' in report:
        lines.append(f'本次新增确认：{report["uploadedThisRun"]}')
    sync = report.get('timeSync') or {}
    if sync and (sync.get('generated') or sync.get('reused') or sync.get('selected')
                 or sync.get('skips') or sync.get('lastError')):
        lines.append(f'时间同步索引：已确认 {sync.get("completed", 0)}/{sync.get("selected", 0)}；'
                     f'新建 {sync.get("generated", 0)}，复用 {sync.get("reused", 0)}，待上传 {sync.get("pending", 0)}。')
        if sync.get('waitingForSessionEnd'):
            lines.append('部分同步索引等待本轮采集确认结束；视频入仓结果单独见上方。')
        reasons = dict(missingClockDomain='缺少可验证的同机时钟标记', missingSessionId='缺少采集轮次',
            notSealedVideo='尚未形成完整视频', unverifiedTimestampSemantics='时间戳含义未经验证',
            missing_rgb_or_depth='缺少同轮彩色或深度视频', storage_policy_excluded='当前存储策略未放行',
            robotMismatch='机器人身份不匹配', sourceUnavailable='来源清单不可读')
        details = [f'{description} {sync["skips"][key]}' for key, description in reasons.items()
                   if sync.get('skips', {}).get(key)]
        if details:
            lines.append('同步索引未生成或未上传原因：' + '；'.join(details) + '。')
        if sync.get('lastError') or sync.get('pending') or sync.get('errors'):
            lines.append('时间同步索引尚未全部确认；视频完成不代表同步索引完成。')
        code = sync.get('lastError')
        if isinstance(code, str) and re.fullmatch(r'ARCHIVE_[A-Z0-9_]{1,96}', code):
            lines.append('同步诊断：' + code)
    folder = report.get('processedFolder') or {}
    if folder.get('createdDatasets'):
        lines.append(f'处理数据目录：{folder["name"]}（{folder["createdDatasets"]} 个数据集）')
    if folder.get('lastError'):
        lines.append('处理数据目录尚未全部创建；视频及其他文件的入仓结果见上方。')
        lines.append('目录诊断：' + folder['lastError'])
    if report.get('preparingSegments', 0):
        prefix = '可上传成品已全部确认；' if confirmed else ''
        lines.append(f'{prefix}另有 {report["preparingSegments"]} 个历史或待封片片段'
                     '尚未形成可上传成品，需要继续封片或检查。')
    if report.get('recoverySegments', 0):
        lines.append('恢复目录中的原件仍保留，需另行核对其恢复结果。')
    if report.get('scope') == 'local-and-source-sealed-snapshot':
        lines.append(f'Jetson 本次选中成品：{report.get("sourceSelectedSegments", "未核对")}；'
                     f'尚待同步到本机：{report.get("sourcePendingSegments", "未核对")}')
        lines.append('范围：开始时本机与 Jetson 的已封片快照；新录制文件在下次上传时处理。')
    else:
        lines.append('范围：本次本机已封片快照；未检查 Jetson 的未同步数据。')
    code = report.get('lastError')
    explanations = {
        'ARCHIVE_LOCAL_SEALING_PENDING': '仍有待封片、录制中或中转中的本机片段，尚不能确认全部采集数据已入仓。',
        'ARCHIVE_UPLOAD_TIME_BUDGET_EXPIRED': '本次等待时间已到，未完成部分可稍后再次运行继续上传。',
        'ARCHIVE_UPLOAD_INTERRUPTED': '上传已中断，已保存的断点和本机原件继续保留。',
        'ARCHIVE_API_UNAVAILABLE': '数仓当前无法连接，请恢复连接后重试。',
        'ARCHIVE_LOCAL_ROOT_UNAVAILABLE': '本机归档目录不存在或不可用，请检查目录设置。',
        'ARCHIVE_LOCAL_SCAN_FAILED': '部分本机目录无法读取，尚未完成核对。',
        'ARCHIVE_UPLOAD_CONFIG_INVALID': '上传配置无法读取或格式有误，请检查本机配置。',
        'ARCHIVE_LOCAL_UPLOAD_INVALID': '部分本机文件或状态无法核对，请保留文件后检查。',
        'ARCHIVE_STATE_INVALID': '存在无法核对的上传回执，请保留原件并检查。',
        'ARCHIVE_MANIFEST_INVALID': '存在无法读取的成品清单，请保留原件并检查。',
        'ARCHIVE_MANIFEST_CHANGED': '成品清单与上传回执不一致，未将该片段计为完成。',
        'ARCHIVE_FOREGROUND_UPLOAD_BUSY': '已有另一个前台上传任务运行，请等待其完成。',
        'ARCHIVE_SOURCE_SYNC_UNAVAILABLE': 'Jetson 源端未能完成检查；已继续处理本机成品，请保持 Jetson 与工控机连接后重试。',
        'ARCHIVE_SOURCE_SYNC_FAILED': '部分 Jetson 成品未能同步；源端原件保留，请检查连接、空间或清单。',
        'ARCHIVE_SOURCE_SNAPSHOT_INVALID': 'Jetson 返回的成品清单格式无效，原件保留，请检查源端状态。',
        'ARCHIVE_SOURCE_MANIFEST_INVALID': 'Jetson 有损坏或无法读取的归档清单，健康文件继续上传，请保留原件并检查。',
        'ARCHIVE_SOURCE_MANIFEST_CHANGED': 'Jetson 与本机同一片段的清单不同，已保留两份数据，请核对后重试。',
        'ARCHIVE_ROBOT_IDENTITY_INVALID': '当前机器人身份缺失或无效，请检查机器人配置。',
        'ARCHIVE_ROBOT_IDENTITY_MISMATCH': '数据或上传配置属于其他机器人，已保留原件，未将其计为本车入仓完成。',
        'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT': '同目录已有内容相同、名称不同的文件，数仓去重规则限制上传；原件保留，可调整目标目录后重试。',
    }
    if code:
        explanation = explanations.get(code, '部分片段尚未确认入仓，请保留本机文件后检查或重试。')
        lines.append('说明：' + explanation)
        if isinstance(code, str) and re.fullmatch(r'ARCHIVE_[A-Z0-9_]{1,96}', code):
            lines.append('诊断代码：' + code)
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='同步已封片原始归档并上传；不启动设备，不操作采集开关，不删除源端文件。')
    parser.add_argument('--env-file', default=os.environ.get('ARCHIVE_ENV_FILE',
        str(Path.home() / '.config/robot-cloud/archive.env')))
    parser.add_argument('--archive-root')
    parser.add_argument('--timeout', type=float, default=os.environ.get('ARCHIVE_UPLOAD_WAIT_SECONDS', '600'))
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--sync-source', action='store_true', help='同时检查并同步 Jetson 已封片成品')
    scope.add_argument('--local-only', action='store_true', help='只上传本机已有成品')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--summary', action='store_true', help='以中文摘要展示结果，保留原有退出码')
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error('--timeout 需大于 0 且不超过 3600 秒')
    try:
        values = load_environment(args.env_file)
        from .robot_profile import validate_current_profile
        profile = validate_current_profile(values=values)
        root = args.archive_root or values.get('ARCHIVE_ROOT',
            str(Path.home() / '.local/share/robot-native-archive'))
        def show_progress(report):
            print(f'上传进行中：本次新增确认 {report["uploadedThisRun"]}，'
                  f'待入仓 {report["pendingSegments"]}，'
                  f'Jetson 待同步 {report.get("sourcePendingSegments", "未核对")}。',
                  file=sys.stderr, flush=True)
        from .archive_upload_folders import processed_folder_name
        report, result = run_upload(root, values, timeout=args.timeout, dry_run=args.dry_run,
            sync_source=args.sync_source, progress=show_progress if args.summary or args.sync_source else None,
            processed_folder=None if args.dry_run else processed_folder_name(),
            expected_robot_id=profile.get('warehouseId') if isinstance(profile, dict) else None)
    except (OSError, ValueError, ArchiveUploadError):
        report, result = dict(state='incomplete', scope='local-sealed-snapshot',
            remoteSync='not_checked', lastError='ARCHIVE_UPLOAD_CONFIG_INVALID'), 3
    print(readable_summary(report) if args.summary else
          json.dumps(report, ensure_ascii=False, allow_nan=False))
    return result


if __name__ == '__main__':
    if not {'--dry-run', '--help', '-h'}.intersection(sys.argv[1:]):
        from .archive_scheduling import background_priority
        background_priority()
    raise SystemExit(main())
