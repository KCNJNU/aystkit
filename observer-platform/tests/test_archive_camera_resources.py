"""USB JPEG writer must not wait for inventory scans or statvfs probes."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from collector import archive_resources
from collector.archive_camera import NativeCameraSink
from collector.archive_resources import StorageMonitor, spool_bytes
from tests.test_archive_camera import jpeg, record, store


def monitored(tmp_path, monkeypatch, *, scan=None, threaded=False, **limits):
    clock, probes, monitors = [0.0], [], []
    def factory(root, **_options):
        def disk(path):
            probes.append(threading.get_ident())
            return SimpleNamespace(free=1_000_000_000)
        monitor = StorageMonitor(root, disk_usage=disk,
            scan=scan or (lambda *_: 0), interval=.01, spool_interval=10,
            max_age=15, monotonic=lambda: clock[0])
        if not threaded:
            monitor.start = lambda: None
        monitors.append(monitor)
        return monitor
    monkeypatch.setattr(archive_resources, 'StorageMonitor', factory)
    target = store(tmp_path, background_resources=True, **limits)
    return target, monitors[0], clock, probes


def wait_for(predicate):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate()


def test_usb_unknown_capacity_fails_closed_without_inline_probe(tmp_path, monkeypatch):
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch)
    try:
        assert not record(target, jpeg())
        assert target.snapshot()['resourceMonitor']['state'] == 'PENDING'
        assert probes == [] and not list(tmp_path.rglob('frames.mjpg'))
    finally:
        target.close()
    assert monitor.snapshot()['state'] == 'CLOSED'


def test_usb_exact_jpeg_and_index_bytes_are_reserved_and_quota_remains_enforced(tmp_path, monkeypatch):
    raw = jpeg()
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch,
        max_spool_bytes=len(raw)+4096+1)
    monitor._probe_once()
    try:
        assert record(target, raw)
        payload = next(tmp_path.rglob('frames.mjpg'))
        index = next(tmp_path.rglob('index.jsonl'))
        exact = len(raw)+index.stat().st_size
        assert monitor.snapshot()['acceptedBytes'] == exact
        assert monitor.snapshot()['spoolBytes'] == exact
        assert monitor.snapshot()['inFlightBytes'] == 0
        assert not record(target, raw, 2)
        assert next(tmp_path.rglob('frames.mjpg')).read_bytes() == raw
        assert len(probes) == 1, 'writing and status must not perform statvfs'
    finally:
        target.close()


def test_usb_stale_or_failed_probe_cannot_admit_new_frames(tmp_path, monkeypatch):
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch)
    monitor._probe_once()
    raw = jpeg()
    try:
        assert record(target, raw)
        clock[0] = 16
        assert not record(target, raw, 2)
        assert target.snapshot()['resourceMonitor']['state'] == 'STALE'
        monitor.scan = lambda *_: (_ for _ in ()).throw(OSError('fixture unreadable inventory'))
        monitor._probe_once()
        assert not record(target, raw, 3)
        assert target.snapshot()['resourceMonitor']['state'] == 'ERROR'
        monitor.scan = lambda *_: 0
        clock[0] = 26
        monitor._probe_once()
        assert record(target, raw, 4)
        assert 'storage' not in target.snapshot()['errors']
    finally:
        target.close()


def test_blocked_usb_inventory_does_not_block_writer_status_or_camera_tap(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    scans = []
    def scan(root, stopping):
        size = spool_bytes(root, stopping)
        scans.append(size)
        if len(scans) == 2:
            entered.set()
            assert release.wait(3), 'isolated blocked inventory must be released'
        return size
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch, scan=scan, threaded=True)
    sink = NativeCameraSink(target)
    raw = jpeg()
    try:
        wait_for(lambda: monitor.snapshot()['ready'])
        sink.start()
        clock[0] = 10
        assert entered.wait(2)
        for sequence in range(40):
            assert sink.submit('camera1', raw, sequence=sequence, timestamp_ns=sequence+1,
                wall_time=time.time(), width=19, height=13)
        assert sink.flush(timeout=2)
        # A synchronous scan in NativeCameraStore._record would keep the writer
        # and this store-lock snapshot blocked until release, overflowing later.
        assert not release.is_set()
        assert sink.snapshot()['counters']['written'] == 40
        assert sink.snapshot()['counters'].get('queueDropped', 0) == 0
        snapshot = target.snapshot()
        assert snapshot['resourceMonitor']['inFlightBytes'] == 0
        assert snapshot['resourceMonitor']['unscannedBytes'] > len(raw)*40
        assert next(tmp_path.rglob('frames.mjpg')).read_bytes() == raw*40
        assert len(set(probes)) == 1 and probes[0] != threading.get_ident()
    finally:
        release.set()
        assert sink.close(timeout=2)
    assert monitor.snapshot()['state'] == 'CLOSED'


def test_usb_failed_write_releases_inflight_reservation_and_preserves_original(tmp_path, monkeypatch):
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch)
    monitor._probe_once()
    raw = jpeg()
    try:
        assert record(target, raw)
        original = target.active['camera1']['framesFile']
        class FailedFile:
            def write(self, data): raise OSError('fixture disk failure')
            def close(self): original.close()
        target.active['camera1']['framesFile'] = FailedFile()
        with pytest.raises(OSError): record(target, raw, 2)
        assert monitor.snapshot()['inFlightBytes'] == 0
        assert next((tmp_path/'recovery').rglob('frames.mjpg')).read_bytes() == raw
    finally:
        target.close()


def test_usb_pending_monitor_shutdown_is_explicit_and_retryable(tmp_path, monkeypatch):
    target, monitor, clock, probes = monitored(tmp_path, monkeypatch)
    outcomes = iter((False, True))
    monitor.close = lambda: next(outcomes)
    target.close()
    assert target.closed and 'resourceMonitor' in target.snapshot()['errors']
    target.close()
    assert 'resourceMonitor' not in target.snapshot()['errors']
