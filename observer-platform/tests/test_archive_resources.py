"""Storage admission remains bounded while slow filesystem work is off the writer."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from collector import archive_resources
from collector.archive_core import ArchiveStore
from collector.archive_resources import ProbeCancelled, StorageMonitor, TreeChanged, spool_bytes
from collector.archive_ros import ArchiveRosAdapter


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir()
        self.rows = []
    def write(self, topic, kind, data, timestamp):
        self.rows.append((topic, kind, data, timestamp))
        with (self.path/'data.db3').open('ab') as output:
            output.write(data)
    def close(self):
        (self.path/'metadata.yaml').write_text('fixture')


class Node:
    def __init__(self):
        self.callbacks = {}
    def create_subscription(self, kind, topic, callback, qos, **options):
        assert options['raw'] is True
        self.callbacks[topic] = callback
        return topic
    def destroy_subscription(self, topic):
        self.callbacks.pop(topic, None)


def wait_for(predicate):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def monitored_store(tmp_path, monkeypatch, *, scan=None, clock=None, threaded=False, **limits):
    clock = [0.0] if clock is None else clock
    probes = []
    monitors = []
    def factory(root, **options):
        def disk(path):
            probes.append(threading.get_ident())
            return SimpleNamespace(free=1_000_000_000)
        monitor = StorageMonitor(root, disk_usage=disk,
            scan=scan or (lambda root, stop: 0), interval=.01,
            spool_interval=10, max_age=15, monotonic=lambda: clock[0])
        if not threaded:
            monitor.start = lambda: None
        monitors.append(monitor)
        return monitor
    monkeypatch.setattr(archive_resources, 'StorageMonitor', factory)
    store = ArchiveStore(tmp_path, 'test-robot', writer_factory=Writer, compression=False,
        min_free_bytes=10, background_resources=True, **limits)
    return store, monitors[0], clock, probes


def test_unknown_probe_fails_closed_without_querying_disk_from_writer_or_status(tmp_path, monkeypatch):
    store, monitor, _, probes = monitored_store(tmp_path, monkeypatch)
    try:
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'CDR', 1)
        state = store.snapshot()
        assert state['resourceMonitor']['state'] == 'PENDING'
        assert state['freeBytes'] is None and state['active'] == {}
        assert probes == [] and not (tmp_path/'.active').exists()
    finally:
        store.close()
    assert monitor.snapshot()['state'] == 'CLOSED'


def test_delta_and_next_message_size_both_count_before_admission(tmp_path, monkeypatch):
    store, monitor, _, probes = monitored_store(tmp_path, monkeypatch, max_spool_bytes=5)
    monitor._probe_once()
    try:
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'abcd', 1)
        assert not store.record_accepted('雷达MID360', '/livox/imu', 'Imu', b'ef', 2)
        assert monitor.snapshot()['spoolBytes'] == 4
        assert monitor.snapshot()['freeBytes'] == 1_000_000_000-4
        assert next(tmp_path.rglob('data.db3')).read_bytes() == b'abcd'
        assert len(probes) == 1, 'neither per-message admission nor status performs statvfs'
    finally:
        store.close()


def test_probe_age_and_failure_never_reuse_a_previously_good_admission(tmp_path, monkeypatch):
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch)
    monitor._probe_once()
    try:
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'first', 1)
        clock[0] = 16
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'stale', 2)
        assert monitor.snapshot()['state'] == 'STALE'
        monitor.scan = lambda *_: (_ for _ in ()).throw(OSError('isolated scan failure'))
        monitor._probe_once()
        assert monitor.snapshot()['state'] == 'ERROR'
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'failed', 3)
        assert next(tmp_path.rglob('data.db3')).read_bytes() == b'first'
        clock[0] = 26
        monitor.scan = lambda *_: 0
        monitor._probe_once()
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'recovered', 4)
        assert 'storage' not in store.snapshot()['errors']
    finally:
        store.close()


def test_inflight_write_cannot_disappear_into_a_scan_baseline(tmp_path):
    clock, observed = [0.0], [0]
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=100),
        scan=lambda *_: observed[0], monotonic=lambda: clock[0])
    monitor._probe_once()
    monitor.begin_write(7)
    clock[0] = 10
    monitor._probe_once()  # The reserved bytes are not on disk yet.
    monitor.finish_write(7)
    assert monitor.snapshot()['spoolBytes'] == 7
    assert monitor.snapshot()['freeBytes'] == 93
    assert monitor.snapshot()['unscannedBytes'] == 7
    observed[0], clock[0] = 7, 20
    monitor._probe_once()
    assert monitor.snapshot()['spoolBytes'] == 7
    assert monitor.snapshot()['unscannedBytes'] == 0
    assert monitor.close()


def test_blocked_tree_scan_never_blocks_raw_callbacks_writer_or_status(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    scans = []
    def scan(root, stop):
        size = spool_bytes(root, stop)
        scans.append(size)
        if len(scans) == 2:
            entered.set()
            assert release.wait(3), 'test must release its isolated blocking probe'
        return size
    store, monitor, clock, probes = monitored_store(tmp_path, monkeypatch, scan=scan, threaded=True)
    adapter = ArchiveRosAdapter(store, lambda: True, sensors=('雷达MID360',))
    node = Node()
    adapter.bind(node, lambda name: name, lambda spec: None)
    try:
        wait_for(lambda: monitor.snapshot()['ready'])
        adapter.reconcile_gate()
        original = [b'\x00\x01CDR'+index.to_bytes(4, 'little') for index in range(100)]
        clock[0] = 10
        assert entered.wait(2)
        for raw in original:
            node.callbacks['/livox/lidar'](raw)
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['written'] == len(original))
        assert not release.is_set(), 'all writes completed while the full-tree scan remained blocked'
        assert store.snapshot()['resourceMonitor']['unscannedBytes'] == sum(map(len, original))
        assert next(tmp_path.rglob('data.db3')).read_bytes() == b''.join(original)
        assert len(set(probes)) == 1 and probes[0] != threading.get_ident()
        release.set()
        wait_for(lambda: monitor.snapshot()['spoolAgeS'] == 0)
        assert monitor.snapshot()['spoolBytes'] >= sum(map(len, original))
        assert adapter.snapshot()['topics']['/livox/lidar']['dropped'] == 0
    finally:
        release.set()
        assert adapter.close()
        store.close()
    assert monitor.thread is not None and not monitor.thread.is_alive()
    assert monitor.snapshot()['inFlightBytes'] == 0


def test_free_probe_runs_more_often_than_full_tree_scan(tmp_path):
    clock, calls = [0.0], []
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: (calls.append('disk') or SimpleNamespace(free=100)),
        scan=lambda *_: (calls.append('scan') or 0), monotonic=lambda: clock[0])
    for moment in (0, 2, 4, 6, 8, 10):
        clock[0] = moment
        monitor._probe_once()
    assert calls.count('disk') == 6 and calls.count('scan') == 2
    assert monitor.close()


def test_close_cancels_probe_and_reclaims_its_thread_without_holding_writer_lock(tmp_path, monkeypatch):
    entered = threading.Event()
    def scan(root, stop):
        entered.set()
        assert stop.wait(2)
        raise ProbeCancelled()
    store, monitor, _, _ = monitored_store(tmp_path, monkeypatch, scan=scan, threaded=True)
    assert entered.wait(2)
    store.close()
    assert not monitor.thread.is_alive()
    assert 'resourceMonitor' not in store.snapshot()['errors']


def test_close_reports_an_unfinished_probe_and_can_retry_cleanup(tmp_path, monkeypatch):
    store, monitor, _, _ = monitored_store(tmp_path, monkeypatch)
    original_close = monitor.close
    monitor.close = lambda: False
    store.close()
    assert store.snapshot()['errors']['resourceMonitor'] == 'ResourceMonitorShutdownTimeout'
    monitor.close = original_close
    store.close()
    assert 'resourceMonitor' not in store.snapshot()['errors']


def test_default_scanner_counts_regular_files_without_following_external_links(tmp_path):
    root, outside = tmp_path/'spool', tmp_path/'outside'
    root.mkdir(); outside.mkdir()
    (root/'raw').write_bytes(b'original')
    (outside/'secret').write_bytes(b'outside must not be read')
    (root/'linked').symlink_to(outside, target_is_directory=True)
    (root/'file-link').symlink_to(outside/'secret')
    assert spool_bytes(root, threading.Event()) == len(b'original')


def test_disk_probe_error_does_not_allow_a_good_spool_sample_to_admit_data(tmp_path, monkeypatch):
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch)
    monitor._probe_once()
    try:
        clock[0] = 2
        monitor.disk_usage = lambda _: (_ for _ in ()).throw(OSError('isolated disk failure'))
        monitor._probe_once()
        assert monitor.snapshot()['errors'] == {'disk': 'OSError'}
        assert not store.record_accepted('雷达MID360', '/livox/imu', 'Imu', b'original', 1)
        assert not (tmp_path/'.active').exists()
    finally:
        store.close()


def test_a_directory_renamed_during_inventory_is_retried_without_omitting_its_bytes(tmp_path, monkeypatch):
    (tmp_path/'source').mkdir()
    (tmp_path/'source'/'raw').write_bytes(b'keep every source byte')
    original = archive_resources.os.scandir
    moved = []
    def moving_scan(path):
        if Path(path) == tmp_path/'source' and not moved:
            (tmp_path/'source').rename(tmp_path/'sealed')
            moved.append(True)
        return original(path)
    monkeypatch.setattr(archive_resources.os, 'scandir', moving_scan)
    assert spool_bytes(tmp_path, threading.Event()) == len(b'keep every source byte')
    assert moved


def test_probe_worker_exit_is_never_reported_as_available(tmp_path):
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=100), scan=lambda *_: 0)
    monitor._probe_once()
    worker = threading.Thread(target=lambda: None)
    worker.start(); worker.join()
    monitor.thread = worker
    assert monitor.snapshot()['state'] == 'ERROR'
    assert monitor.snapshot()['errors']['worker'] == 'ResourceProbeWorkerStopped'
    assert monitor.close()


def test_tree_changes_before_first_complete_inventory_never_admit_data(tmp_path, monkeypatch):
    def changing_tree(*_args):
        raise TreeChanged('fixture recording rotation')
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch, scan=changing_tree)
    try:
        for moment in (0, 10, 20):
            clock[0] = moment
            monitor._probe_once()
            report = monitor.snapshot()
            assert report['state'] == 'ERROR' and report['spoolBytes'] is None
            assert report['errors'] == {'spool': 'TreeChanged'}
            assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'unknown', 1)
        assert not (tmp_path/'.active').exists()
    finally:
        store.close()


def test_live_rotations_keep_full_accepted_delta_and_still_enforce_spool_limit(tmp_path, monkeypatch):
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch, max_spool_bytes=8)
    monitor._probe_once()
    try:
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'abcd', 1)
        original_baseline = (monitor.spool, monitor.spool_base, monitor.spool_at)
        def changing_tree(*_args):
            raise TreeChanged('fixture finalizer rename')
        monitor.scan = changing_tree
        for moment in (10, 12):
            clock[0] = moment
            monitor._probe_once()
            report = monitor.snapshot(incoming_bytes=4)
            assert report['ready'] and report['spoolEstimateOnly']
            assert report['warnings'] == {'spool': 'TreeChanged'} and report['errors'] == {}
            assert report['spoolBytes'] == 8 and report['unscannedBytes'] == 4
            assert report['spoolAgeS'] == moment and report['spoolCheckAgeS'] == 0
            assert (monitor.spool, monitor.spool_base, monitor.spool_at) == original_baseline
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'efgh', 2)
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'i', 3)
        assert monitor.snapshot()['spoolBytes'] == 8
        assert next(tmp_path.rglob('data.db3')).read_bytes() == b'abcdefgh'
        assert '本地缓存达到上限' in store.snapshot()['errors']['storage']
    finally:
        store.close()


def test_renames_during_every_inventory_retry_keep_previously_counted_bytes(tmp_path, monkeypatch):
    (tmp_path/'source').mkdir()
    data = b'keep complete original source'
    (tmp_path/'source/raw').write_bytes(data)
    clock = [0.0]
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
                             monotonic=lambda: clock[0])
    monitor._probe_once()
    original = archive_resources.os.scandir
    moves = []
    def rotating_scan(path):
        if Path(path) == tmp_path:
            before = tmp_path.stat()
            source, target = (tmp_path/'source', tmp_path/'sealed') if (tmp_path/'source').exists() else (tmp_path/'sealed', tmp_path/'source')
            source.rename(target)
            archive_resources.os.utime(tmp_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            moves.append(True)
        return original(path)
    monkeypatch.setattr(archive_resources.os, 'scandir', rotating_scan)
    monitor.begin_write(7)  # This write is not yet represented by an inventory.
    clock[0] = 10
    monitor._probe_once()
    report = monitor.snapshot()
    assert len(moves) == 3 and report['ready'] and report['spoolEstimateOnly']
    assert report['observedSpoolBytes'] == len(data)
    assert report['spoolBytes'] == len(data) + 7 and report['inFlightBytes'] == 7
    monitor.finish_write(7)
    assert monitor.close()


@pytest.mark.parametrize('failure', [OSError('fixture disk failure'), PermissionError('fixture access failure')])
def test_tree_changed_never_clears_real_inventory_errors_without_complete_success(tmp_path, failure):
    clock = [0.0]
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=lambda *_: 20, monotonic=lambda: clock[0])
    monitor._probe_once()
    monitor.begin_write(7)
    monitor.finish_write(7)
    clock[0] = 10
    monitor.scan = lambda *_: (_ for _ in ()).throw(failure)
    monitor._probe_once()
    assert monitor.snapshot()['errors'] == {'spool': type(failure).__name__}
    clock[0] = 20
    monitor.scan = lambda *_: (_ for _ in ()).throw(TreeChanged('fixture rotation'))
    monitor._probe_once()
    report = monitor.snapshot()
    assert report['state'] == 'ERROR' and report['errors'] == {'spool': type(failure).__name__}
    assert report['spoolBytes'] == 27
    clock[0] = 30
    monitor.scan = lambda *_: 27
    monitor._probe_once()
    report = monitor.snapshot()
    assert report['ready'] and report['errors'] == {} and not report['spoolEstimateOnly']
    assert report['spoolBytes'] == 27 and report['spoolAgeS'] == 0
    assert monitor.close()


def test_changing_inventory_never_bypasses_disk_reserve_or_disk_probe_failure(tmp_path, monkeypatch):
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch)
    monitor._probe_once()
    try:
        def changing_tree(*_args):
            raise TreeChanged('fixture finalizer activity')
        monitor.scan = changing_tree
        monitor.disk_usage = lambda _: SimpleNamespace(free=10)
        clock[0] = 10
        monitor._probe_once()
        assert monitor.snapshot()['ready']
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'a', 1)
        assert '磁盘剩余空间低于预留值' in store.snapshot()['errors']['storage']
        monitor.disk_usage = lambda _: (_ for _ in ()).throw(OSError('fixture disk failure'))
        clock[0] = 20
        monitor._probe_once()
        assert monitor.snapshot()['state'] == 'ERROR'
        assert monitor.snapshot()['errors'] == {'disk': 'OSError'}
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'b', 2)
        assert not (tmp_path/'.active').exists()
    finally:
        store.close()


def test_conservative_inventory_still_expires_if_probes_stop_making_progress(tmp_path):
    clock = [0.0]
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=lambda *_: 20, monotonic=lambda: clock[0])
    monitor._probe_once()
    clock[0] = 10
    monitor.scan = lambda *_: (_ for _ in ()).throw(TreeChanged('fixture rotation'))
    monitor._probe_once()
    assert monitor.snapshot()['ready']
    clock[0] = 26
    assert monitor.snapshot()['state'] == 'STALE'
    assert monitor.snapshot()['spoolBytes'] == 20
    assert monitor.close()


def test_repeated_tree_changes_cannot_refresh_full_inventory_age_and_stable_scan_recovers(tmp_path, monkeypatch):
    store, monitor, clock, _ = monitored_store(tmp_path, monkeypatch)
    monitor._probe_once()
    try:
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'first', 1)
        def changing_tree(*_args):
            raise TreeChanged('fixture continuing directory rotation')
        monitor.scan = changing_tree
        for moment in (10, 12, 14):
            clock[0] = moment
            monitor._probe_once()
            assert monitor.snapshot()['ready']
        clock[0] = 16
        monitor._probe_once()
        report = monitor.snapshot()
        assert report['state'] == 'STALE' and report['spoolAgeS'] == 16
        assert report['spoolCheckAgeS'] == 0 and report['freeAgeS'] == 0
        assert not store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'blocked', 2)
        assert monitor.snapshot()['spoolBytes'] == len(b'first')
        clock[0] = 18
        monitor.scan = lambda *_: len(b'first')
        monitor._probe_once()
        report = monitor.snapshot()
        assert report['ready'] and report['spoolAgeS'] == 0 and not report['spoolEstimateOnly']
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'after', 3)
        assert sorted(path.read_bytes() for path in tmp_path.rglob('data.db3')) == sorted([b'first', b'after'])
    finally:
        store.close()


def test_tree_changed_retries_on_worker_interval_instead_of_regular_spool_interval(tmp_path):
    clock, calls = [0.0], []
    def scan(*_args):
        calls.append(clock[0])
        if clock[0] in (10, 12, 14):
            raise TreeChanged('fixture recording rename')
        return 0
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=scan, interval=2, spool_interval=10, monotonic=lambda: clock[0])
    for moment in (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20):
        clock[0] = moment
        monitor._probe_once()
    assert calls == [0, 10, 12, 14, 16]
    assert monitor.snapshot()['ready'] and not monitor.snapshot()['spoolEstimateOnly']
    assert monitor.close()


def test_busy_segment_repairs_do_not_restart_the_historical_tree_or_forget_deleted_bytes(tmp_path, monkeypatch):
    history, active = tmp_path/'history', tmp_path/'active'
    history.mkdir(); active.mkdir()
    (history/'video.mkv').write_bytes(b'historical-video')
    (active/'original').write_bytes(b'keep-in-conservative-inventory')
    original = archive_resources.os.scandir
    calls = []

    class ChangingDirectory:
        def __init__(self, path):
            self.path, self.scan = path, original(path)

        def __enter__(self):
            return self.scan.__enter__()

        def __exit__(self, *args):
            result = self.scan.__exit__(*args)
            if self.path == active and calls.count(active) <= 2:
                # Cleanup and atomic metadata replacement happen after all
                # entries were counted, like concurrent segment finalization.
                before = active.stat()
                previous = active/('original' if calls.count(active) == 1 else 'metadata-1')
                previous.unlink()
                (active/('metadata-' + str(calls.count(active)))).write_bytes(b'new')
                archive_resources.os.utime(active,
                    ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return result

    def recording_scan(path):
        calls.append(Path(path))
        return ChangingDirectory(Path(path))

    monkeypatch.setattr(archive_resources.os, 'scandir', recording_scan)
    size = spool_bytes(tmp_path, threading.Event())
    assert calls.count(history) == 1, 'busy metadata must not rescan settled historical videos'
    assert calls.count(active) == 3
    assert size == len(b'historical-video') + len(b'keep-in-conservative-inventory') + 2*len(b'new')
    assert size > sum(path.stat().st_size for path in tmp_path.rglob('*') if path.is_file())


def test_blocked_inventory_cannot_starve_disk_probe_and_still_expires_safely(tmp_path):
    entered, release = threading.Event(), threading.Event()
    clock, scans, disks = [0.0], [], []

    def scan(_root, _stop):
        scans.append(clock[0])
        if len(scans) == 2:
            entered.set()
            assert release.wait(3)
        return 20

    def disk(_root):
        disks.append(clock[0])
        return SimpleNamespace(free=1000)

    monitor = StorageMonitor(tmp_path, disk_usage=disk, scan=scan,
        interval=.01, spool_interval=10, max_age=15, monotonic=lambda: clock[0])
    try:
        monitor.start()
        wait_for(lambda: monitor.snapshot()['ready'])
        monitor.begin_write(7); monitor.finish_write(7)
        clock[0] = 10
        assert entered.wait(2)
        probes_before_clock_advance = len(disks)
        clock[0] = 16
        wait_for(lambda: len(disks) > probes_before_clock_advance
                 and monitor.snapshot()['freeAgeS'] == 0)
        report = monitor.snapshot()
        assert disks[-1] == 16, 'disk reserve must refresh while inventory I/O is blocked'
        assert report['state'] == 'STALE' and report['spoolAgeS'] == 16
        assert report['freeAgeS'] == 0 and report['freeBytes'] == 1000
        assert report['spoolBytes'] == 27, 'pending bytes cannot disappear from old inventory'
        assert report['workerAlive'] and report['spoolWorkerAlive']
        assert not release.is_set()
        release.set()
        wait_for(lambda: monitor.snapshot()['ready'])
    finally:
        release.set()
        assert monitor.close()
    assert not monitor.thread.is_alive() and not monitor.spool_thread.is_alive()


def test_inventory_worker_death_is_never_hidden_by_a_live_disk_worker(tmp_path):
    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=lambda *_: 0)
    monitor._probe_once()
    finished = threading.Thread(target=lambda: None)
    finished.start(); finished.join()
    monitor.spool_thread = finished
    assert monitor.snapshot()['state'] == 'ERROR'
    assert monitor.snapshot()['errors']['spoolWorker'] == 'ResourceProbeWorkerStopped'
    assert monitor.close()


def test_background_scan_starts_on_absolute_deadlines_and_retries_before_inventory_expires(tmp_path):
    clock, starts, states, delays = [0.0], [], [], []

    def scan(_root, _stop):
        starts.append(clock[0])
        clock[0] += 4  # Full-load scan time must consume, not extend, the period.
        if len(starts) > 1:
            states.append(monitor.snapshot()['state'])
        if len(starts) == 2:
            raise TreeChanged('fixture one concurrent finalization')
        return 20

    class ScheduledStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            delays.append(delay)
            clock[0] += delay
            monitor._probe_disk()
            self.stopped = len(starts) >= 3
            return self.stopped

    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=scan, interval=10, spool_interval=10, max_age=15, monotonic=lambda: clock[0])
    monitor.stopping = ScheduledStop()
    monitor._probe_disk()
    monitor._run_spool()
    assert starts == pytest.approx([0, 5, 9.1])
    assert delays[:2] == pytest.approx([1, .1])
    assert states == ['READY', 'READY'], 'one changed scan must recover within the original 15s age'
    assert monitor.spool_at == pytest.approx(9.1)
    assert monitor.max_age == 15 and monitor.spool_base == 0


def test_continuing_background_tree_changes_back_off_without_refreshing_expired_baseline(tmp_path):
    clock, starts, delays = [0.0], [], []

    def scan(_root, _stop):
        starts.append(clock[0])
        clock[0] += 2
        if len(starts) > 1:
            raise TreeChanged('fixture never stable')
        return 20

    class ScheduledStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            assert .1 - 1e-9 <= delay <= 5, 'bounded waits prevent a retry busy loop'
            delays.append(delay)
            clock[0] += delay
            monitor._probe_disk()
            self.stopped = len(starts) >= 7
            return self.stopped

    monitor = StorageMonitor(tmp_path, disk_usage=lambda _: SimpleNamespace(free=1000),
        scan=scan, interval=10, spool_interval=10, max_age=15, monotonic=lambda: clock[0])
    monitor.stopping = ScheduledStop()
    monitor._probe_disk()
    monitor._run_spool()
    monitor.stopping.stopped = False  # Inspect admission before normal shutdown.
    report = monitor.snapshot()
    assert delays[1:] == pytest.approx([.1, .2, .4, .8, 1, 1])
    assert report['state'] == 'STALE' and report['spoolAgeS'] > 15
    assert report['freeAgeS'] == 0 and report['spoolCheckAgeS'] < 4
    assert monitor.spool_at == 0 and monitor.spool_base == 0
    assert report['spoolBytes'] == 20
