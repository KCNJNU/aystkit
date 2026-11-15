"""Background disk probes for archival; no ROS callback scans the filesystem.

A scan baseline is captured before touching disk. Bytes accepted during the scan
are added to its result, even if that double-counts some already observed bytes.
In-flight writes are never part of a baseline until a later scan can observe them.
After a complete baseline exists, ordinary concurrent renames may briefly keep
that baseline plus subsequent accepted bytes within its original max_age only.
They trigger faster retries and never extend the full inventory's validity.
"""
from __future__ import annotations

import math
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import sys
import threading
import time


class ProbeCancelled(Exception):
    pass


class TreeChanged(OSError):
    pass


class IsolatedInventoryProbe:
    """One owned read-only child, independent of a ROS writer's native GIL.

    The deadline and accounting baseline belong to the parent before launch.
    A child finishing early cannot certify a result delivered after that age.
    Only bounded JSON and error names cross the process boundary; no ROS or
    application runtime is imported by the worker entry point below.
    """
    MAX_OUTPUT_BYTES = 4096

    def __init__(self, *, timeout=15, monotonic=time.monotonic):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid isolated inventory timeout')
        self.timeout, self.monotonic = timeout, monotonic
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.child = None

    def _command(self, root):
        return [sys.executable, '-B', str(Path(__file__).resolve()),
                '--inventory-scan', str(Path(root))]

    @staticmethod
    def _stop_owned(child, timeout=.6):
        deadline = time.monotonic() + max(0, timeout)
        if child.poll() is None:
            try:
                child.terminate()
            except ProcessLookupError:
                pass  # Natural exit or another close won the race.
        try:
            child.wait(timeout=min(.3, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return False
        return child.poll() is not None

    def __call__(self, root, cancelled, *, started_at=None):
        started = self.monotonic() if started_at is None else started_at
        if type(started) not in (int, float) or not math.isfinite(started):
            raise ValueError('invalid parent inventory baseline')
        with self.lock:
            if self.stopping.is_set() or cancelled.is_set():
                raise ProbeCancelled()
            if self.child is not None:
                raise RuntimeError('inventory child already running')
            if not 0 <= self.monotonic() - started < self.timeout:
                raise TimeoutError('inventory parent baseline expired before launch')
            child = subprocess.Popen(self._command(root), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.child = child
        selector = None
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        try:
            selector = selectors.DefaultSelector()
            for name, stream in (('stdout', child.stdout), ('stderr', child.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or child.poll() is None:
                if self.stopping.is_set() or cancelled.is_set():
                    raise ProbeCancelled()
                remaining = self.timeout - (self.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('inventory parent deadline exceeded')
                for key, _events in selector.select(timeout=min(.1, remaining)):
                    buffer = output[key.data]
                    chunk = os.read(key.fileobj.fileno(), self.MAX_OUTPUT_BYTES-len(buffer)+1)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffer.extend(chunk)
                        if len(buffer) > self.MAX_OUTPUT_BYTES:
                            raise OSError('isolated inventory output exceeded limit')
            if self.stopping.is_set() or cancelled.is_set():
                raise ProbeCancelled()
            if not 0 <= self.monotonic() - started < self.timeout:
                raise TimeoutError('inventory result arrived after parent deadline')
            if output['stderr']:
                raise OSError('isolated inventory worker reported stderr')
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate inventory result field')
                    result[key] = value
                return result
            try:
                result = json.loads(output['stdout'], object_pairs_hook=unique_fields)
            except (UnicodeError, ValueError, RecursionError) as error:
                raise OSError('invalid isolated inventory JSON') from error
            if type(result) is not dict or type(result.get('ok')) is not bool:
                raise OSError('invalid isolated inventory result')
            if result['ok'] is True:
                if (child.returncode != 0 or set(result) != {'ok', 'bytes'}
                        or type(result['bytes']) is not int or result['bytes'] < 0):
                    raise OSError('invalid isolated inventory byte count')
                return result['bytes']
            errors = {'TreeChanged': TreeChanged, 'ProbeCancelled': ProbeCancelled,
                      'FileNotFoundError': FileNotFoundError, 'PermissionError': PermissionError,
                      'OSError': OSError, 'ValueError': ValueError, 'ProbeError': OSError}
            if (child.returncode != 1 or set(result) != {'ok', 'error'}
                    or type(result.get('error')) is not str or result['error'] not in errors):
                raise OSError('invalid isolated inventory error')
            raise errors[result['error']]('isolated inventory scan failed')
        finally:
            if selector is not None:
                selector.close()
            try:
                self._stop_owned(child)
            finally:
                child.stdout.close(); child.stderr.close()
                with self.lock:
                    # An unexpected permission failure must retain the owned
                    # handle for a later close, never silently orphan a child.
                    if self.child is child and child.poll() is not None:
                        self.child = None

    def close(self, timeout=.6):
        self.stopping.set()
        with self.lock:
            child = self.child
        return child is None or self._stop_owned(child, timeout)


def spool_bytes(root, cancelled):
    """Repair changed directories without rescanning an otherwise stable tree."""
    return _tree_bytes(root, cancelled)


def _tree_bytes(root, cancelled):
    """Keep an upper bound across bounded local repairs of concurrent renames.

    Finalizers replace small manifests and move sealed segments while recording.
    Restarting the entire historical tree for each such change may never finish.
    Revisit changed directories instead, retaining every previously counted byte:
    a rename or deletion can overcount this sample, but cannot erase old bytes.
    Three unsettled repair rounds still fail closed; no partial inventory is used.
    """
    root = Path(root)
    pending, directories, sizes = {root}, {}, {}

    def signature(info):
        return info.st_dev, info.st_ino, info.st_mtime_ns

    def revisit_parent(directory, changed):
        directories.pop(directory, None)
        if directory == root:
            raise TreeChanged('archive resource root changed')
        changed.add(directory.parent)

    for _attempt in range(3):
        changed = set()
        while pending:
            if cancelled.is_set():
                raise ProbeCancelled()
            directory = pending.pop()
            try:
                before = directory.stat(follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    revisit_parent(directory, changed)
                    continue
                directories[directory] = signature(before)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if cancelled.is_set():
                            raise ProbeCancelled()
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            changed.add(directory)
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            path = Path(entry.path)
                            if directories.get(path) != signature(info):
                                pending.add(path)
                        elif stat.S_ISREG(info.st_mode):
                            sizes[entry.path] = max(sizes.get(entry.path, 0), info.st_size)
            except FileNotFoundError:
                revisit_parent(directory, changed)
        for directory, observed in tuple(directories.items()):
            if cancelled.is_set():
                raise ProbeCancelled()
            try:
                info = directory.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    revisit_parent(directory, changed)
                elif signature(info) != observed:
                    changed.add(directory)
            except FileNotFoundError:
                revisit_parent(directory, changed)
        if not changed:
            return sum(sizes.values())
        pending = changed
    raise TreeChanged('archive resource tree kept changing during local repairs')


class StorageMonitor:
    """Independent disk/inventory workers; admission and status never do I/O."""
    def __init__(self, root, *, disk_usage=shutil.disk_usage, scan=spool_bytes,
                 interval=2.0, spool_interval=10.0, max_age=15.0,
                 monotonic=time.monotonic, isolate_inventory=False):
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                for value in (interval, spool_interval, max_age))
                or interval > max_age or spool_interval > max_age):
            raise ValueError('invalid archive resource probe intervals')
        if type(isolate_inventory) is not bool or isolate_inventory and scan is not spool_bytes:
            raise ValueError('isolated inventory requires the built-in strict scanner')
        self.root, self.disk_usage, self.scan = Path(root), disk_usage, scan
        self.interval, self.spool_interval, self.max_age = interval, spool_interval, max_age
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = None
        self.spool_thread = None
        self.total_bytes = self.in_flight_bytes = 0
        self.free = self.spool = None
        self.free_base = self.spool_base = 0
        self.free_at = self.spool_at = None
        self.spool_checked_at = None
        self.spool_estimated = False
        self.retry_spool = False
        self.errors = {}
        self.last_scan_attempt = float('-inf')
        self.last_scan_duration = None
        self.inventory_probe = IsolatedInventoryProbe(timeout=max_age, monotonic=monotonic) if isolate_inventory else None

    def start(self):
        with self.lock:
            if self.stopping.is_set():
                raise RuntimeError('archive resource monitor is closed')
            if self.thread is None:
                self.thread = threading.Thread(target=self._run,
                    name='archive-storage-monitor', daemon=True)
                self.spool_thread = threading.Thread(target=self._run_spool,
                    name='archive-spool-monitor', daemon=True)
                self.thread.start()
                self.spool_thread.start()

    def begin_write(self, size):
        if type(size) is not int or size < 0:
            raise ValueError('invalid archive resource byte count')
        with self.lock:
            self.total_bytes += size
            self.in_flight_bytes += size

    def finish_write(self, size):
        with self.lock:
            self.in_flight_bytes -= size
            if self.in_flight_bytes < 0:
                raise RuntimeError('archive resource accounting imbalance')

    def snapshot(self, incoming_bytes=0):
        now = self.monotonic()
        with self.lock:
            free_age = None if self.free_at is None else now - self.free_at
            spool_age = None if self.spool_at is None else now - self.spool_at
            spool_check_age = None if self.spool_checked_at is None else now - self.spool_checked_at
            stale = any(age is not None and (age < 0 or age > self.max_age)
                        for age in (free_age, spool_age))
            pending = self.free is None or self.spool is None
            running = self.thread is not None and self.thread.is_alive()
            spool_running = self.spool_thread is not None and self.spool_thread.is_alive()
            errors = dict(self.errors)
            if self.thread is not None and not running and not self.stopping.is_set():
                errors['worker'] = 'ResourceProbeWorkerStopped'
            if self.spool_thread is not None and not spool_running and not self.stopping.is_set():
                errors['spoolWorker'] = 'ResourceProbeWorkerStopped'
            state = ('CLOSED' if self.stopping.is_set() else 'ERROR' if errors else
                     'PENDING' if pending else 'STALE' if stale else 'READY')
            free_delta = self.total_bytes - self.free_base
            spool_delta = self.total_bytes - self.spool_base
            return dict(state=state, ready=state == 'READY', workerAlive=running,
                spoolWorkerAlive=spool_running, scanDurationS=self.last_scan_duration,
                isolatedInventory=self.inventory_probe is not None,
                freeBytes=None if self.free is None else max(0, self.free-free_delta-incoming_bytes),
                spoolBytes=None if self.spool is None else self.spool+spool_delta+incoming_bytes,
                observedSpoolBytes=self.spool, unscannedBytes=spool_delta,
                acceptedBytes=self.total_bytes, inFlightBytes=self.in_flight_bytes,
                freeAgeS=free_age, spoolAgeS=spool_age, spoolCheckAgeS=spool_check_age,
                spoolEstimateOnly=self.spool_estimated,
                warnings={'spool': 'TreeChanged'} if self.spool_estimated else {}, errors=errors)

    def _probe_once(self):
        """Synchronous probe pair for deterministic diagnostics and unit tests."""
        self._probe_disk()
        self._probe_spool()

    def _probe_disk(self):
        started = self.monotonic()
        with self.lock:
            baseline = self.total_bytes-self.in_flight_bytes
        try:
            free = self.disk_usage(self.root).free
            if type(free) is not int or free < 0:
                raise ValueError('invalid disk probe result')
            with self.lock:
                self.free, self.free_base, self.free_at = free, baseline, started
                self.errors.pop('disk', None)
        except Exception as error:
            with self.lock:
                self.errors['disk'] = type(error).__name__

    def _probe_spool(self, *, force=False):
        started = self.monotonic()
        with self.lock:
            baseline = self.total_bytes-self.in_flight_bytes
        if self.stopping.is_set() or (not force and not self.retry_spool
                and started-self.last_scan_attempt < self.spool_interval):
            return
        self.last_scan_attempt = started
        try:
            size = (self.inventory_probe(self.root, self.stopping, started_at=started)
                    if self.inventory_probe is not None else self.scan(self.root, self.stopping))
            if type(size) is not int or size < 0:
                raise ValueError('invalid spool probe result')
            with self.lock:
                self.spool, self.spool_base, self.spool_at = size, baseline, started
                self.spool_checked_at, self.spool_estimated = started, False
                self.retry_spool = False
                self.errors.pop('spool', None)
        except ProbeCancelled:
            pass
        except TreeChanged:
            with self.lock:
                self.retry_spool = True
                if self.spool is not None and self.errors.get('spool') in (None, 'TreeChanged'):
                    # Renaming/sealing/upload cleanup is expected while recording.
                    # Keep all previously observed bytes and the full accepted
                    # delta; a failed scan cannot certify deletion or advance its
                    # accounting baseline or its original max_age deadline.
                    # Real I/O errors remain fail-closed.
                    self.spool_checked_at, self.spool_estimated = started, True
                    self.errors.pop('spool', None)
                else:
                    # With no complete baseline there is no safe estimate. A
                    # prior I/O failure also requires a complete successful scan.
                    self.errors.setdefault('spool', 'TreeChanged')
        except Exception as error:
            with self.lock:
                self.retry_spool = False
                self.errors['spool'] = type(error).__name__
        finally:
            with self.lock:
                self.last_scan_duration = max(0, self.monotonic() - started)

    def _run(self):
        while not self.stopping.is_set():
            self._probe_disk()
            self.stopping.wait(self.interval)

    def _run_spool(self):
        # Reserve two further attempts inside the same strict inventory age.
        # Schedule from the scan's START, so slow work consumes its interval
        # instead of adding another full sleep after it finishes. A changed
        # tree retries promptly; the previous baseline's age never advances.
        period = min(self.spool_interval, self.max_age / 3)
        failures = 0
        while not self.stopping.is_set():
            self._probe_spool(force=True)
            with self.lock:
                changed = self.retry_spool
                due = self.last_scan_attempt + period
            failures = failures + 1 if changed else 0
            retry_delay = min(1, .1 * 2 ** min(failures - 1, 4)) if changed else 0
            delay = retry_delay if changed else max(.1, due - self.monotonic())
            deadline = self.monotonic() + delay
            while not self.stopping.is_set():
                remaining = deadline - self.monotonic()
                if remaining <= 1e-9:
                    break
                if self.stopping.wait(min(self.interval, remaining)):
                    return

    def close(self, timeout=2.0):
        self.stopping.set()
        deadline = time.monotonic() + max(0, timeout)
        child_stopped = (self.inventory_probe is None or
            self.inventory_probe.close(timeout=max(0, deadline - time.monotonic())))
        for thread in (self.thread, self.spool_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        return child_stopped and all(thread is None or not thread.is_alive()
                   for thread in (self.thread, self.spool_thread))


def _inventory_worker_main(root):
    """Standalone stdlib-only child; never imports ROS or starts devices."""
    try:
        size = spool_bytes(Path(root), threading.Event())
        result, code = dict(ok=True, bytes=size), 0
    except Exception as error:
        kinds = (ProbeCancelled, TreeChanged, FileNotFoundError, PermissionError, OSError, ValueError)
        kind = next((item.__name__ for item in kinds if isinstance(error, item)), 'ProbeError')
        result, code = dict(ok=False, error=kind), 1
    print(json.dumps(result, separators=(',', ':')), flush=True)
    return code


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--inventory-scan':
        raise SystemExit('expected --inventory-scan root')
    raise SystemExit(_inventory_worker_main(sys.argv[2]))
