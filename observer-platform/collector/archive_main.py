"""Independent opt-in garage recorder. Never starts sensors or vehicle controls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
from zoneinfo import ZoneInfo
from uuid import UUID

from .archive_core import ArchiveStore, GarageGate, atomic_json
from .archive_policy import camera_only_enabled, parse_ros_sensors, allowed_manifest_path
from .archive_upload import ArchiveUploadClient, ArchiveUploadConfig, ArchiveUploadError
from .archive_scheduling import run_background

LOG = logging.getLogger(__name__)

# These errors mean that the source was sealed in a form the derivative
# encoder cannot read.  The raw segment is still useful for inspection and
# must remain on disk, but retrying it during shutdown can never make progress.
TERMINAL_DERIVATIVE_ERRORS = frozenset({
    'video_source_file_invalid',
    'video_interrupted_staging_unsafe',
    'bag_source_file_invalid',
    'bag_source_path_invalid',
    'bag_source_link',
    'bag_manifest_invalid',
    'bag_pending_manifest_invalid',
    'bag_metadata_or_message_count_invalid',
    'bag_yaml_missing',
})


def _camera_segment_limit(environ=None):
    """Bound camera staging without changing legacy/default capture settings."""
    values = os.environ if environ is None else environ
    raw = values.get('ARCHIVE_CAMERA_SEGMENT_BYTES', str(512 * 1024**2))
    message = 'ARCHIVE_CAMERA_SEGMENT_BYTES must be an integer from 1 to 536870912'
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise ValueError(message)
    try:
        limit = int(raw)
    except ValueError:
        raise ValueError(message) from None
    if not 1 <= limit <= 512 * 1024**2:
        raise ValueError(message)
    return limit


def _terminal_video_pending(snapshot):
    """Return true when only permanently bad video sources remain pending."""
    if (not isinstance(snapshot, dict) or type(snapshot.get('pendingSegments')) is not int
            or snapshot['pendingSegments'] <= 0):
        return False
    if snapshot.get('terminalPendingSegments') != snapshot['pendingSegments']:
        # A historical bad input must never hide another, unattempted segment.
        return False
    errors = snapshot.get('errors')
    if not isinstance(errors, dict) or not errors:
        return False
    # VideoWorker prefixes errors with ``video:`` and ``bag:``.  Keep this
    # conservative: a worker/runtime error must still block a normal drain.
    return all((str(key).startswith(('video:', 'bag:'))
                and any(code in str(value) for code in TERMINAL_DERIVATIVE_ERRORS))
               for key, value in errors.items())


def finish_archive(adapter, store, video=None):
    """Drain accepted ROS messages before closing the raw segment store.

    A failed drain must not touch the store: the writer may still hold its
    lock. Unfinished local files remain available for the next recovery pass.
    Upload completion is intentionally independent from durable local storage.
    """
    report = dict(state='stop_failed', ros=None, store=None, errors={})
    if adapter is not None:
        try:
            if adapter.close(timeout=20) is not True:
                report['errors']['rosClose'] = 'WriterShutdownIncomplete'
                return report
        except Exception as error:
            report['errors']['rosClose'] = type(error).__name__
            return report
        try:
            ros = report['ros'] = adapter.snapshot()
            if (ros.get('workerAlive') is not False
                    or any(type(ros.get(key)) is not int or ros[key] != 0 for key in
                           ('queueMessages', 'queueBytes', 'inFlightMessages', 'subscriptionCleanupPending'))):
                report['errors']['rosClose'] = 'WriterShutdownUnconfirmed'
                return report
        except Exception as error:
            report['errors']['rosSnapshot'] = type(error).__name__
            return report
    try:
        store.close()
    except Exception as error:
        report['errors']['storeClose'] = type(error).__name__
    try:
        value = report['store'] = store.snapshot()
        if value.get('active') != {}:
            report['errors']['activeSegments'] = 'UnfinishedSegments'
        store_errors = value.get('errors', {})
        if isinstance(store_errors, dict) and store_errors.get('recovery'):
            report.setdefault('warnings', {})['recovery'] = store_errors['recovery']
        blocking_store_errors = {key: item for key, item in (store_errors.items()
                              if isinstance(store_errors, dict) else ())
                                 if key != 'recovery'}
        if blocking_store_errors:
            report['errors']['store'] = 'StorageErrorsPresent'
    except Exception as error:
        report['errors']['storeSnapshot'] = type(error).__name__
    if video is not None:
        try:
            refresh = getattr(video, 'refresh_pending', None)
            if refresh is not None:
                refresh()
            before = video.snapshot()
            if _terminal_video_pending(before):
                # Preserve the source and its diagnostic, but do not spend
                # three minutes retrying an input that cannot be decoded.
                report.setdefault('warnings', {})['videoFinalize'] = 'DeferredTerminalSource'
                close = getattr(video, 'close', None)
                if close is not None:
                    close()
            elif not video.drain(timeout=180):
                report['errors']['videoFinalize'] = 'VideoFinalizationIncomplete'
            else:
                close = getattr(video, 'close', None)
                if close is not None:
                    close()
            report['video'] = video.snapshot()
        except Exception as error:
            report['errors']['videoFinalize'] = type(error).__name__
    ros = report['ros']
    if ros is not None:
        try:
            if ros.get('tickError') or ros.get('gateError'):
                report['errors']['ros'] = 'RecorderErrorsPresent'
            for topic, value in ros['topics'].items():
                reasons = value.get('reasons', {})
                if value.get('error') or any(reasons.get(key, 0) for key in
                                           ('writer_error', 'writer_backoff', 'store_rejected')):
                    report['errors']['topic:' + topic] = 'TopicWriteErrorsPresent'
        except (KeyError, TypeError, AttributeError):
            report['errors']['ros'] = 'InvalidRecorderStatus'
    if not report['errors']:
        report['state'] = 'stopped'
    return report


def status_with_transfer(store, control, video=None, *, cached=False):
    value = store.snapshot()
    if video is not None:
        videos = video.snapshot(cached=True) if cached else video.snapshot()
        value['videoPendingSegments'] = videos['pendingSegments']
        value['videoFinalizing'] = videos['running']
        value['videoTerminalPending'] = _terminal_video_pending(videos)
        value['derivativeTerminalPending'] = value['videoTerminalPending']
        if videos.get('errors'):
            value['errors'] = {**value.get('errors', {}),
                'videoFinalization': '视频封装或原生数据压缩未完成，原始缓存保留，可在下次启动时重试'}
    try:
        path = Path(control) / 'source.json'
        if path.stat().st_size <= 512 * 1024:
            transfer = json.loads(path.read_text()).get('fileTransfer') or {}
            if transfer.get('lastError'):
                value['errors'] = {**value.get('errors', {}),
                    'sourceTransfer': str(transfer['lastError'])[:256]}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return value


class VideoWorker:
    """Finalize sealed video and lossless bags in two independent bounded lanes."""
    def __init__(self, root, robot_id, *, camera_only=False, **options):
        from .archive_video import VideoFinalizer
        from .archive_bag_compress import BagCompressionFinalizer
        if type(camera_only) is not bool:
            raise ValueError('camera_only must be a boolean')
        selection = {'camera_only': True} if camera_only else {}
        self.finalizer = VideoFinalizer(root, robot_id, **selection, **options)
        bag_options = {key: value for key, value in options.items()
                       if key in ('timeout_s', 'reserve_bytes')}
        self.bag_finalizer = BagCompressionFinalizer(root, robot_id, **selection, **bag_options)
        self.stopping = threading.Event()
        self.runtime_error = None
        self.runtime_errors = {}
        self.error_lock = threading.Lock()
        self.workers_started = threading.Event()
        self.wakeups = {kind: threading.Event() for kind in ('video', 'bag')}
        self.bag_thread = threading.Thread(target=run_background,
            args=(self._run_finalizer, 'bag', self.bag_finalizer), name='archive-bag-finalizer', daemon=True)
        self.thread = threading.Thread(target=run_background, args=(self.run,),
            name='archive-video-finalizer', daemon=True)

    def snapshot(self, *, cached=False):
        video = self.finalizer.status(cached=True) if cached else self.finalizer.status()
        bag = self.bag_finalizer.status(cached=True) if cached else self.bag_finalizer.status()
        with self.error_lock:
            runtime_errors = self.runtime_errors.copy()
        errors = {**{'video:' + key: value for key, value in video.get('errors', {}).items()},
                  **{'bag:' + key: value for key, value in bag.get('errors', {}).items()},
                  **runtime_errors}
        state = {key: (None if video.get(key, 0) is None or bag.get(key, 0) is None else
                      video.get(key, 0) + bag.get(key, 0))
                 for key in ('pendingSegments', 'terminalPendingSegments', 'finalizedSegments', 'failedAttempts', 'busySegments')}
        if cached:
            state['discoveryReady'] = (video.get('discoveryReady') is True
                                       and bag.get('discoveryReady') is True)
        return {**state, 'running': video['running'] or bag['running'], 'errors': errors,
                'video': video, 'bag': bag,
                'workerAlive': self.thread.is_alive() and self.bag_thread.is_alive(),
                'videoWorkerAlive': self.thread.is_alive(), 'bagWorkerAlive': self.bag_thread.is_alive()}

    def run(self):
        # Preserve the public thread.start() entry point while starting both
        # lanes together. A long video encode cannot starve incoming ROS bags.
        self.bag_thread.start()
        self.workers_started.set()
        self._run_finalizer('video', self.finalizer)

    def _run_finalizer(self, kind, finalizer):
        wakeup = self.wakeups[kind]
        while not self.stopping.is_set():
            wakeup.clear()
            completed = 0
            try:
                completed = finalizer.run_once(max_segments=1)
                with self.error_lock:
                    self.runtime_errors.pop('worker:' + kind, None)
            except Exception as error:
                with self.error_lock:
                    self.runtime_errors['worker:' + kind] = type(error).__name__
                LOG.error('Archive finalization pass unavailable; original source files retained')
            with self.error_lock:
                self.runtime_error = next(iter(self.runtime_errors.values()), None)
            # Process an existing backlog promptly; idle/failing lanes back off.
            if not completed:
                wakeup.wait(5)

    def refresh_pending(self):
        for kind, finalizer in (('video', self.finalizer), ('bag', self.bag_finalizer)):
            refresh = getattr(finalizer, 'refresh_pending', None)
            if refresh is not None:
                refresh()
            self.wakeups[kind].set()

    def drain(self, timeout):
        self.refresh_pending()
        deadline = time.monotonic()+timeout
        while True:
            state = self.snapshot()
            if state['pendingSegments'] == 0 and not state['running']:
                return not state.get('errors')
            if (not self.thread.is_alive()
                    or (self.workers_started.is_set() and not self.bag_thread.is_alive())
                    or time.monotonic() >= deadline):
                return False
            self.stopping.wait(.1)

    def close(self):
        self.stopping.set()
        for wakeup in self.wakeups.values():
            wakeup.set()
        deadline = time.monotonic() + 3
        for thread in (self.thread, self.bag_thread):
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline-time.monotonic()))
        # A derivative encoder may be inside ffprobe/ffmpeg when the recorder
        # is stopped.  The source segment is already closed and durable, so a
        # still-running daemon can finish or be retried at the next start; it
        # must not turn a successful raw shutdown into a service failure.


class UploadWorker:
    def __init__(self, root, config, stopping, gate=None, sensors=(), *, camera_only=False, control=None):
        self.root, self.config, self.stopping = Path(root), config, stopping
        self.status = dict(pendingSegments=0, completedSegments=0, lastError=None, lastSuccessAt=None,
                           storagePolicy='camera-only' if camera_only else 'all-sensors', skippedSegments=0)
        self.lock = threading.Lock()
        self.gate, self.sensors = gate, tuple(sensors)
        self.camera_only = camera_only
        self.control = Path(control) if control is not None else None
        self.retention_complete = set()
        self.thread = threading.Thread(target=run_background, args=(self.run,),
            name='archive-local-uploader', daemon=True)

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    @staticmethod
    def _segment_error(error):
        """Retry bad local material separately; back off a shared API failure."""
        if (error.status in (401, 403, 408, 429)
                or isinstance(error.status, int) and error.status >= 500):
            return False
        return error.code in {
            'ARCHIVE_MANIFEST_INVALID', 'ARCHIVE_PENDING_SEGMENT_INVALID',
            'ARCHIVE_MANIFEST_EXCLUDED_BY_STORAGE_POLICY',
            'ARCHIVE_MANIFEST_CHANGED', 'ARCHIVE_STATE_INVALID', 'ARCHIVE_NAME_INVALID',
            'ARCHIVE_FILE_CHANGED', 'ARCHIVE_FILE_SIZE_INVALID',
            'ARCHIVE_UPLOAD_LOCK_INVALID', 'ARCHIVE_UPLOAD_STATE_MISMATCH',
            'ARCHIVE_REMOTE_FILE_MISMATCH', 'ARCHIVE_REMOTE_OBJECT_MISSING',
            'ARCHIVE_DUPLICATE_CONTENT_NAME_CONFLICT',
            'ARCHIVE_ROBOT_IDENTITY_MISMATCH',
        }

    def run(self):
        client = ArchiveUploadClient(self.config)
        limiter = getattr(getattr(client, 'api', None), 'upload_limiter', None)
        if limiter is not None:
            limiter.stopping = self.stopping
        if self.control is not None:
            threading.Thread(target=self._run_session_ends, name='archive-session-end-uploader', daemon=True).start()
        from .archive_retention import prune_confirmed_copy
        ready_day = None
        while not self.stopping.is_set():
            manifests = sorted((self.root / 'days').rglob('archive.json')) if (self.root / 'days').exists() else []
            all_manifests = manifests
            manifests = [path for path in manifests if allowed_manifest_path(path, self.camera_only)]
            pending = [path for path in manifests if not (path.parent / 'uploaded.json').exists()]
            with self.lock:
                self.status.update(pendingSegments=len(pending), completedSegments=len(manifests) - len(pending),
                                   skippedSegments=len(all_manifests)-len(manifests))
            had_error = False
            upload_unavailable = False
            day = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
            if self.gate is not None and self.gate.enabled() and ready_day != day:
                try:
                    # Make today's dataset visible as soon as garage capture
                    # is armed, including explicitly empty sensor directories.
                    client.ensure_day(day, self.sensors)
                    ready_day = day
                except (ArchiveUploadError, OSError, ValueError) as error:
                    had_error = True
                    upload_unavailable = True
                    with self.lock:
                        self.status['lastError'] = error.code if isinstance(error, ArchiveUploadError) else type(error).__name__
            for manifest in pending:
                if self.stopping.is_set() or upload_unavailable:
                    break
                try:
                    result = client.upload_segment(manifest.parent)
                    with self.lock:
                        if not had_error:
                            self.status['lastError'] = None
                        if result['state'] == 'complete':
                            self.status['lastSuccessAt'] = datetime.now(timezone.utc).isoformat()
                            self.status['pendingSegments'] -= 1
                            self.status['completedSegments'] += 1
                except ArchiveUploadError as error:
                    had_error = True
                    with self.lock:
                        self.status['lastError'] = error.code
                    # A bad local segment must not starve later recordings.
                    # Shared API failures get one backoff, preserving all bytes.
                    if not self._segment_error(error):
                        upload_unavailable = True
                        break
                except Exception as error:
                    had_error = True
                    with self.lock:
                        self.status['lastError'] = type(error).__name__
                    if not isinstance(error, (OSError, ValueError, KeyError, TypeError,
                                              AttributeError, RecursionError, OverflowError)):
                        upload_unavailable = True
                        break
            # An unrelated pending upload must not retain every confirmed copy.
            # Each candidate independently rechecks its ACK, local bytes and
            # warehouse file access before the duplicate spool is released.
            for manifest in manifests:
                if self.stopping.is_set() or upload_unavailable:
                    break
                try:
                    if manifest.parent in self.retention_complete:
                        continue
                    pruned = prune_confirmed_copy(manifest.parent, client,
                        retain_seconds=int(os.environ.get('ARCHIVE_LOCAL_RETAIN_SECONDS', 300)))
                    marker = manifest.parent / 'local-copy-pruned.json'
                    if pruned or (marker.is_file() and not marker.is_symlink()
                                  and json.loads(marker.read_text()).get('state') == 'pruned'):
                        self.retention_complete.add(manifest.parent)
                except (ArchiveUploadError, OSError, ValueError, KeyError, TypeError):
                    # Preserve copies if server access or integrity could
                    # not be confirmed; the next normal pass will retry.
                    continue
            if not had_error:
                with self.lock:
                    self.status['lastError'] = None
            self.stopping.wait(30 if had_error else 15)

    def _run_session_ends(self):
        from .archive_session_end import upload_session_ends
        client = ArchiveUploadClient(self.config)
        client.api.timeout = 10
        while not self.stopping.is_set():
            try:
                result = upload_session_ends(self.control, self.root, client)
            except Exception as error:
                result = dict(lastError=type(error).__name__)
            with self.lock:
                self.status['sessionEnds'] = result
            self.stopping.wait(15)


def _upload_snapshot_factory(root, store, gate, recording_policy, *, upload=None,
                             camera_only=False, clock=None, cache_seconds=30,
                             background=False, stopping=None):
    """Display counts only; status callers never scan or read leases in background mode."""
    import math
    if (type(background) is not bool or type(cache_seconds) not in (int, float)
            or not math.isfinite(cache_seconds) or cache_seconds <= 0):
        raise ValueError('invalid upload count cache options')
    clock = time.monotonic if clock is None else clock
    cached = None
    cached_at = float('-inf')
    cached_key = None
    observed_at = None
    scan_error = None
    scanning = False
    force_in_progress = False
    generation = 0
    scan_cancelled = threading.Event()
    closed = threading.Event()
    worker = None
    lock = threading.Lock()
    force_lock = threading.Lock()

    class Cancelled(Exception):
        pass

    def stopped():
        return closed.is_set() or stopping is not None and stopping.is_set()

    def memory_key():
        return getattr(store, 'finalized', 0), getattr(store, 'closed', False)

    def invalidation_key():
        # A renewed desktop lease changes updatedAt, but not the count scope.
        # Gate reads belong to the worker (or explicit synchronous mode) only.
        declaration = gate.snapshot()
        return (*memory_key(), declaration.get('enabled') is True,
                declaration.get('desktopSession'))

    def collect(cancelled):
        days = Path(root) / 'days'
        if days.is_symlink() or days.exists() and not days.is_dir():
            raise ValueError('invalid archive count directory')
        total = selected = completed = 0
        def check():
            if cancelled():
                raise Cancelled()
        def walk_error(error):
            raise error
        check()
        if days.exists():
            for directory, children, files in os.walk(days, followlinks=False, onerror=walk_error):
                check()
                if 'archive.json' not in files:
                    continue
                path = Path(directory) / 'archive.json'
                total += 1
                if allowed_manifest_path(path, camera_only):
                    selected += 1
                    completed += (path.parent / 'uploaded.json').is_file()
                check()
        return dict(mode='pull_by_ipc', pendingSegments=selected-completed,
                    completedSegments=completed, lastError=None, lastSuccessAt=None,
                    skippedSegments=total-selected, storagePolicy=recording_policy['mode'])

    def refresh(key, *, force=False):
        nonlocal cached, cached_at, cached_key, observed_at, scan_error, scanning
        nonlocal generation, scan_cancelled, force_in_progress
        with lock:
            # A worker can have passed its due check before shutdown/force.
            # Recheck at publication ownership acquisition, so it cannot
            # invalidate the synchronous refresh's generation afterwards.
            if not force and (force_in_progress or stopped()):
                return
            if force:
                force_in_progress = True
                scan_cancelled.set()
            generation += 1
            current_generation = generation
            cancel = scan_cancelled = threading.Event()
            scanning = True
        try:
            # No filesystem call or traversal holds the publication lock.
            value = collect((lambda: False) if force else
                            (lambda: cancel.is_set() or stopped()))
        except Cancelled:
            return
        except Exception as error:
            with lock:
                if generation == current_generation:
                    scan_error = type(error).__name__
            if force or not background:
                raise
        else:
            stamp = clock()
            observed = datetime.now(timezone.utc).isoformat()
            with lock:
                if (generation == current_generation
                        and (force or not stopped() and not cancel.is_set())):
                    cached, cached_key, cached_at = value, key, stamp
                    observed_at, scan_error = observed, None
        finally:
            with lock:
                if generation == current_generation:
                    scanning = False
                if force:
                    force_in_progress = False

    def cached_status():
        # Both clock/key reads and the small publication copy are memory-only.
        now, current_memory_key = clock(), memory_key()
        with lock:
            value = dict(cached) if cached is not None else dict(mode='pull_by_ipc',
                pendingSegments=None, completedSegments=None, skippedSegments=None,
                lastError=None, lastSuccessAt=None, storagePolicy=recording_policy['mode'])
            age = None if cached is None else now-cached_at
            stale = (cached is not None and (age < 0 or age >= cache_seconds
                     or cached_key[:2] != current_memory_key))
            worker_alive = worker is not None and worker.is_alive()
            error = scan_error or ('CountWorkerStopped' if background and worker is not None
                                  and not worker_alive and not stopped() else None)
            state = ('CLOSED' if stopped() else 'ERROR' if error else
                     'PENDING' if cached is None else 'STALE' if stale else 'READY')
            value.update(countsState=state, countsKnown=cached is not None,
                countsObservedAt=observed_at, countsAgeS=age,
                countsWorkerAlive=worker_alive,
                countsScanning=scanning and (worker_alive or not background), countsError=error)
            if error:
                value['lastError'] = 'ARCHIVE_UPLOAD_STATUS_SCAN_FAILED'
            return value

    def run():
        nonlocal scan_error
        while not stopped():
            try:
                key, now = invalidation_key(), clock()
                with lock:
                    due = (cached is None or cached_key != key or now < cached_at
                           or now-cached_at >= cache_seconds or scan_error is not None)
                if due:
                    refresh(key)
            except Exception as error:
                with lock:
                    scan_error = type(error).__name__
            closed.wait(min(.25, cache_seconds))

    def upload_snapshot(*, force=False):
        if upload is not None:
            return upload.snapshot()
        if force:
            # Only explicit synchronous callers wait on this lock; ordinary
            # status reads and the background scan never acquire it.
            with force_lock:
                refresh(invalidation_key(), force=True)
                if background or stopped():
                    return cached_status()
                with lock:
                    return dict(cached)
        elif not background and not stopped():
            key, now = invalidation_key(), clock()
            with lock:
                due = cached is None or cached_key != key or now < cached_at or now-cached_at >= cache_seconds
            if due:
                refresh(key)
        if background or stopped():
            return cached_status()
        with lock:
            return dict(cached)

    def close(timeout=2):
        nonlocal generation, scanning
        closed.set()
        with lock:
            generation += 1
            scan_cancelled.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0, timeout))
        complete = worker is None or not worker.is_alive()
        if complete:
            with lock:
                scanning = False
        return complete

    upload_snapshot.close = close
    upload_snapshot.status = upload_snapshot
    if background and upload is None:
        worker = threading.Thread(target=run, name='archive-upload-counts', daemon=True)
        worker.start()

    return upload_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--garage', choices=('on', 'off'))
    args = parser.parse_args(argv)
    control = Path(os.environ.get('ARCHIVE_CONTROL_DIR', str(Path.home() / '.local/state/robot-cloud/garage-control')))
    root = Path(os.environ.get('ARCHIVE_ROOT', str(Path.home() / '.local/share/robot-native-archive')))
    gate = GarageGate(control / 'garage.json')
    if args.garage:
        gate.set(args.garage == 'on')
        print(json.dumps(dict(garageEnabled=gate.enabled(), scope='garage')))
        return
    if args.status:
        try:
            print((control / 'status.json').read_text())
        except OSError:
            print(json.dumps(dict(state='offline', enabled=gate.enabled())))
        return

    from .robot_profile import validate_current_profile
    validate_current_profile()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    guard = (control / 'recorder.lock').open('a')
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another native archive recorder owns this control directory') from None
    config = None if os.environ.get('ARCHIVE_UPLOAD_DISABLED') == '1' else ArchiveUploadConfig.from_environment(os.environ.get('ARCHIVE_UPLOAD_CONFIG'))
    robot_id = os.environ.get('ARCHIVE_ROBOT_ID') if config is None else config.robot_id
    if not robot_id:
        raise SystemExit('ARCHIVE_ROBOT_ID is required')
    robot_id = str(UUID(robot_id))
    camera_only = camera_only_enabled()
    sensor_names = parse_ros_sensors(os.environ.get('ARCHIVE_ROS_SENSORS'), camera_only)
    depth_video_mode = os.environ.get('ARCHIVE_DEPTH_VIDEO_MODE', 'lossless')
    if depth_video_mode not in ('lossless', 'pseudocolor'):
        raise ValueError('ARCHIVE_DEPTH_VIDEO_MODE must be lossless or pseudocolor')
    camera_segment_bytes = _camera_segment_limit()
    video_fps = float(os.environ.get('ARCHIVE_VIDEO_FPS', '0'))
    recording_policy = dict(mode='camera-only' if camera_only else 'all-sensors',
                            rosSensors=list(sensor_names) if sensor_names is not None else None,
                            depthVideoMode=depth_video_mode, cameraSegmentBytes=camera_segment_bytes,
                            videoTargetFps=video_fps, backgroundResourceChecks=True)
    store = ArchiveStore(root, robot_id, gate=gate, depth_video_mode=depth_video_mode,
        camera_segment_bytes=camera_segment_bytes, video_fps=video_fps, background_resources=True,
        resource_isolate_inventory=True,
        min_free_bytes=int(os.environ.get('ARCHIVE_MIN_FREE_BYTES', 10 * 1024**3)),
        max_spool_bytes=int(os.environ.get('ARCHIVE_MAX_SPOOL_BYTES', 20 * 1024**3)))
    video = VideoWorker(root, robot_id, camera_only=camera_only,
        reserve_bytes=int(os.environ.get('ARCHIVE_MIN_FREE_BYTES', 10 * 1024**3)),
        timeout_s=120)
    video.thread.start()
    stopping = threading.Event()
    for item in (signal.SIGTERM, signal.SIGINT):
        signal.signal(item, lambda *_: stopping.set())
    upload = UploadWorker(root, config, stopping, gate, sensor_names or (), camera_only=camera_only, control=control) if config is not None else None
    if upload:
        upload.thread.start()
    upload_snapshot = _upload_snapshot_factory(root, store, gate, recording_policy,
        upload=upload, camera_only=camera_only, background=True, stopping=stopping)
    import rclpy
    from rclpy.signals import SignalHandlerOptions
    from .archive_ros import build_archive_node
    # Keep SIGTERM/SIGINT under this recorder's control until accepted data has
    # drained. The default rclpy handlers shut the context down first, raising
    # ExternalShutdownException in spin_once during an ordinary service stop.
    rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
    node = adapter = None
    status_thread = None
    runtime_error = None
    try:
        node, adapter = build_archive_node(store, gate.enabled,
            sensors=sensor_names, image_fps=video_fps)
        LOG.info('Native archive standby started; garage=%s; no sensor startup/control/serial access', gate.enabled())
        def publish_status():
            while not stopping.is_set():
                try:
                    atomic_json(control / 'status.json', dict(updatedAt=datetime.now(timezone.utc).isoformat(),
                        store=status_with_transfer(store, control, video, cached=True), ros=adapter.snapshot(), upload=upload_snapshot(),
                        video=video.snapshot(cached=True), recordingPolicy=recording_policy,
                        camera=dict(state='waiting_source', detail='USB相机原生源须在源机接入，网页压缩流不用于归档')))
                except (OSError, ValueError):
                    LOG.error('Native archive status write unavailable')
                stopping.wait(2)
        status_thread = threading.Thread(target=publish_status, name='archive-status', daemon=True)
        status_thread.start()
        while not stopping.is_set() and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
    except BaseException as error:
        runtime_error = type(error).__name__
        raise
    finally:
        stopping.set()
        if status_thread:
            status_thread.join(timeout=3)
        report = finish_archive(adapter, store, video)
        report['recordingPolicy'] = recording_policy
        if runtime_error is not None:
            report['errors']['runtime'] = runtime_error
        def clean_up(name, action):
            try:
                action()
            except Exception as error:
                report['errors'][name] = type(error).__name__
        if node is not None:
            clean_up('nodeCleanup', node.destroy_node)
        if upload is not None:
            clean_up('uploadCleanup', lambda: upload.thread.join(timeout=3))
        clean_up('videoCleanup', video.close)
        clean_up('rosCleanup', lambda: rclpy.shutdown() if rclpy.ok() else None)
        try:
            report['upload'] = upload_snapshot(force=True)
        except Exception as error:
            report['errors']['uploadStatus'] = type(error).__name__
        def close_upload_counts():
            if upload_snapshot.close() is False:
                raise RuntimeError('UploadCountWorkerShutdownTimeout')
        clean_up('uploadStatusCleanup', close_upload_counts)
        if report['errors']:
            report['state'] = 'stop_failed'
        report['updatedAt'] = datetime.now(timezone.utc).isoformat()
        try:
            atomic_json(control / 'status.json', report)
        except Exception as error:
            report['state'] = 'stop_failed'
            LOG.error('Native archive final status write failed: %s', type(error).__name__)
        finally:
            guard.close()
    return 0 if report['state'] == 'stopped' else 1


if __name__ == '__main__':
    raise SystemExit(main())
