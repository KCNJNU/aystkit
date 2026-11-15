"""Pull-only count displays must not rescan the complete archive every 2 seconds."""
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from collector.archive_core import GarageGate
from collector.archive_main import _upload_snapshot_factory


def segment(root, name='one', sensor='原相机前'):
    path = root / 'days' / '2026-11-02' / sensor / 'time-slot' / name / 'archive.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'sensor': sensor, 'metadata': {}}))
    return path


def setup_cache(tmp_path, monkeypatch, *, camera_only=False):
    monkeypatch.delenv('ARCHIVE_TRAINING_ONLY', raising=False)
    root = tmp_path / 'archive'
    gate = GarageGate(tmp_path / 'control' / 'garage.json')
    gate.set(False)
    store = SimpleNamespace(finalized=0, closed=False)
    now = [100.0]
    calls = []
    original = os.walk

    def counted(path, **options):
        if path == root / 'days':
            calls.append('archive.json')
        return original(path, **options)

    monkeypatch.setattr(os, 'walk', counted)
    snapshot = _upload_snapshot_factory(root, store, gate, {'mode': 'camera-only' if camera_only else 'all-sensors'},
        camera_only=camera_only, clock=lambda: now[0])
    return root, store, gate, now, calls, snapshot


def test_repeated_status_ticks_reuse_counts_until_monotonic_deadline(tmp_path, monkeypatch):
    root, _, _, now, calls, snapshot = setup_cache(tmp_path, monkeypatch)
    manifest = segment(root)
    assert snapshot()['pendingSegments'] == 1
    (manifest.parent / 'uploaded.json').write_text('{}')
    for tick in range(2, 30, 2):
        now[0] = 100.0 + tick
        assert snapshot()['pendingSegments'] == 1
    assert len(calls) == 1
    now[0] = 130.0
    assert snapshot()['completedSegments'] == 1
    assert snapshot()['pendingSegments'] == 0
    assert len(calls) == 2


def test_newly_sealed_store_segment_invalidates_without_waiting(tmp_path, monkeypatch):
    root, store, _, now, calls, snapshot = setup_cache(tmp_path, monkeypatch)
    segment(root)
    assert snapshot()['pendingSegments'] == 1
    segment(root, 'two')
    store.finalized += 1
    now[0] += 0.01
    assert snapshot()['pendingSegments'] == 2
    assert len(calls) == 2


def test_gate_edges_refresh_but_lease_heartbeat_revision_does_not_invalidate(tmp_path, monkeypatch):
    root, _, gate, now, calls, snapshot = setup_cache(tmp_path, monkeypatch)
    first = segment(root)
    assert snapshot()['pendingSegments'] == 1
    (first.parent / 'uploaded.json').write_text('{}')
    gate.set(True)
    assert snapshot()['completedSegments'] == 1
    segment(root, 'two')
    gate.set(False)
    assert snapshot()['pendingSegments'] == 1
    segment(root, 'three')
    # A renewed lease/revision does not change the selection or rescan history.
    value = json.loads(gate.path.read_text())
    value['updatedAt'] = '2026-11-02T12:00:00Z'
    gate.path.write_text(json.dumps(value))
    assert snapshot()['pendingSegments'] == 1
    assert len(calls) == 3
    now[0] += 30
    assert snapshot()['pendingSegments'] == 2
    assert len(calls) == 4


def test_missing_days_and_external_camera_segments_refresh_within_ttl(tmp_path, monkeypatch):
    root, _, _, now, calls, snapshot = setup_cache(tmp_path, monkeypatch)
    assert snapshot()['pendingSegments'] == 0
    segment(root)
    now[0] += 29.99
    assert snapshot()['pendingSegments'] == 0
    assert calls == []
    now[0] += 0.01
    assert snapshot()['pendingSegments'] == 1
    assert len(calls) == 1


def test_shutdown_refresh_and_forced_final_report_never_reuse_old_counts(tmp_path, monkeypatch):
    root, store, _, _, calls, snapshot = setup_cache(tmp_path, monkeypatch)
    first = segment(root)
    assert snapshot()['pendingSegments'] == 1
    (first.parent / 'uploaded.json').write_text('{}')
    store.closed = True
    assert snapshot()['completedSegments'] == 1
    second = segment(root, 'two')
    assert snapshot()['pendingSegments'] == 0
    assert snapshot(force=True)['pendingSegments'] == 1
    assert len(calls) == 3
    assert second.is_file()


def test_policy_filter_and_independent_return_dict_are_preserved(tmp_path, monkeypatch):
    root, _, _, _, calls, snapshot = setup_cache(tmp_path, monkeypatch, camera_only=True)
    segment(root)
    segment(root, 'radar', sensor='雷达MID360')
    status = snapshot()
    assert status == dict(mode='pull_by_ipc', pendingSegments=1, completedSegments=0,
                          skippedSegments=1, lastError=None, lastSuccessAt=None, storagePolicy='camera-only')
    status['pendingSegments'] = 999
    assert snapshot()['pendingSegments'] == 1
    assert len(calls) == 1


def test_live_upload_worker_always_delegates_without_gate_or_disk_reads(tmp_path, monkeypatch):
    calls = []

    def worker_status():
        calls.append(None)
        return {'pendingSegments': len(calls)}

    class UnusedGate:
        def snapshot(self):
            raise AssertionError('real uploader status must not use the display cache')

    monkeypatch.setattr(Path, 'rglob', lambda *_: (_ for _ in ()).throw(AssertionError('unexpected disk scan')))
    snapshot = _upload_snapshot_factory(tmp_path, object(), UnusedGate(), {'mode': 'all-sensors'},
        upload=SimpleNamespace(snapshot=worker_status))
    assert snapshot()['pendingSegments'] == 1
    assert snapshot()['pendingSegments'] == 2
    assert snapshot(force=True)['pendingSegments'] == 3


def wait_for(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def test_background_getter_stays_memory_only_while_first_scan_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv('ARCHIVE_TRAINING_ONLY', raising=False)
    segment(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = os.walk
    calls = []
    def blocked(path, **options):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(path, **options)
    monkeypatch.setattr(os, 'walk', blocked)
    class WorkerOnlyGate:
        def snapshot(self):
            assert threading.get_ident() != main_thread
            return {'enabled': True, 'desktopSession':'a'*32}
    main_thread = threading.get_ident()
    snapshot = _upload_snapshot_factory(tmp_path, SimpleNamespace(finalized=0, closed=False),
        WorkerOnlyGate(), {'mode':'all-sensors'}, background=True)
    try:
        assert entered.wait(1)
        started = time.monotonic()
        for _ in range(20):
            state = snapshot()
            assert state['countsState'] == 'PENDING'
            assert state['countsKnown'] is False
            assert state['pendingSegments'] is None and state['completedSegments'] is None
            assert state['countsObservedAt'] is None
        assert time.monotonic()-started < .1
        assert len(calls) == 1
        assert main_thread not in calls
        assert snapshot.close(timeout=.01) is False
        closed = snapshot.status()
        assert closed['countsState'] == 'CLOSED' and closed['countsWorkerAlive']
        assert closed['pendingSegments'] is None and not closed['countsKnown']
    finally:
        release.set()
        assert snapshot.close(timeout=2)
    state = snapshot()
    assert state['countsState'] == 'CLOSED' and not state['countsWorkerAlive']
    assert state['pendingSegments'] is None


def test_background_counts_have_fresh_stale_error_states_and_survive_lease_renewals(tmp_path, monkeypatch):
    monkeypatch.delenv('ARCHIVE_TRAINING_ONLY', raising=False)
    manifest = segment(tmp_path)
    now, calls = [100.0], []
    entered, release = threading.Event(), threading.Event()
    original = os.walk
    class Gate:
        revision = 0
        def snapshot(self):
            return {'enabled':True, 'desktopSession':'b'*32, 'updatedAt':self.revision}
    gate = Gate()
    def counted(path, **options):
        calls.append(threading.get_ident())
        if len(calls) == 2:
            entered.set()
            assert release.wait(3)
        return original(path, **options)
    monkeypatch.setattr(os, 'walk', counted)
    snapshot = _upload_snapshot_factory(tmp_path, SimpleNamespace(finalized=0, closed=False),
        gate, {'mode':'all-sensors'}, background=True, clock=lambda:now[0])
    try:
        wait_for(lambda: snapshot()['countsState']=='READY')
        first = snapshot()
        assert first['pendingSegments']==1 and first['countsObservedAt']
        for revision in range(20):
            gate.revision = revision
            snapshot()
        time.sleep(.3)
        assert len(calls)==1
        (manifest.parent/'uploaded.json').write_text('{}')
        now[0] += 30
        assert entered.wait(1)
        stale = snapshot()
        assert stale['countsState']=='STALE' and stale['pendingSegments']==1
        assert stale['countsObservedAt']==first['countsObservedAt']
        release.set()
        wait_for(lambda:snapshot()['countsState']=='READY')
        assert snapshot()['completedSegments']==1 and snapshot()['pendingSegments']==0
    finally:
        release.set()
        assert snapshot.close()


def test_background_scan_io_error_retains_unknown_and_force_propagates_failure(tmp_path, monkeypatch):
    segment(tmp_path)
    def denied(_path, **options):
        options['onerror'](PermissionError('private path must not leak'))
        return iter(())
    monkeypatch.setattr(os, 'walk', denied)
    snapshot = _upload_snapshot_factory(tmp_path, SimpleNamespace(finalized=0, closed=False),
        SimpleNamespace(snapshot=lambda:{'enabled':False}), {'mode':'all-sensors'}, background=True)
    try:
        wait_for(lambda:snapshot()['countsState']=='ERROR')
        state=snapshot()
        assert state['pendingSegments'] is None and state['completedSegments'] is None
        assert state['lastError']=='ARCHIVE_UPLOAD_STATUS_SCAN_FAILED'
        assert state['countsError']=='PermissionError'
        assert 'private path' not in json.dumps(state)
        import pytest
        with pytest.raises(PermissionError):
            snapshot(force=True)
    finally:
        assert snapshot.close()


def test_force_refresh_after_external_stop_is_exact_and_old_worker_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.delenv('ARCHIVE_TRAINING_ONLY', raising=False)
    manifest = segment(tmp_path)
    stopping, entered, release = threading.Event(), threading.Event(), threading.Event()
    original = os.walk
    calls = []
    def first_blocked(path, **options):
        calls.append(threading.get_ident())
        if len(calls)==1:
            entered.set()
            assert release.wait(3)
        return original(path, **options)
    monkeypatch.setattr(os, 'walk', first_blocked)
    snapshot = _upload_snapshot_factory(tmp_path, SimpleNamespace(finalized=0, closed=False),
        SimpleNamespace(snapshot=lambda:{'enabled':False}), {'mode':'all-sensors'},
        background=True, stopping=stopping)
    try:
        assert entered.wait(1)
        stopping.set()
        (manifest.parent/'uploaded.json').write_text('{}')
        result = snapshot(force=True)
        assert result['countsState']=='CLOSED' and result['countsKnown']
        assert result['completedSegments']==1 and result['pendingSegments']==0
        observed=result['countsObservedAt']
        release.set()
        assert snapshot.close(timeout=2)
        assert snapshot()['countsObservedAt']==observed
        assert snapshot()['completedSegments']==1
    finally:
        release.set()
        snapshot.close()


def test_late_background_refresh_cannot_displace_a_running_force_refresh(tmp_path, monkeypatch):
    monkeypatch.delenv('ARCHIVE_TRAINING_ONLY', raising=False)
    manifest = segment(tmp_path)
    gate_entered, gate_release, worker_revisited = threading.Event(), threading.Event(), threading.Event()
    force_entered, force_release = threading.Event(), threading.Event()
    original = os.walk
    class Gate:
        worker_calls = 0
        def snapshot(self):
            if threading.current_thread().name == 'archive-upload-counts':
                self.worker_calls += 1
                if self.worker_calls == 1:
                    gate_entered.set()
                    assert gate_release.wait(3)
                else:
                    worker_revisited.set()
            return {'enabled':False}
    def block_force(path, **options):
        if threading.current_thread().name == 'forced-count-refresh':
            force_entered.set()
            assert force_release.wait(3)
        return original(path, **options)
    monkeypatch.setattr(os, 'walk', block_force)
    snapshot = _upload_snapshot_factory(tmp_path, SimpleNamespace(finalized=0, closed=False),
        Gate(), {'mode':'all-sensors'}, background=True)
    results = []
    forced = threading.Thread(target=lambda:results.append(snapshot(force=True)),
                              name='forced-count-refresh')
    try:
        assert gate_entered.wait(1)
        forced.start()
        assert force_entered.wait(1)
        gate_release.set()
        # The worker reaches refresh only after force owns publication. A
        # missing guard lets its older count replace force's generation.
        assert worker_revisited.wait(1)
        (manifest.parent/'uploaded.json').write_text('{}')
        force_release.set()
        forced.join(timeout=2)
        assert not forced.is_alive()
        assert len(results)==1 and results[0]['countsKnown']
        assert results[0]['completedSegments']==1 and results[0]['pendingSegments']==0
    finally:
        gate_release.set(); force_release.set()
        if forced.ident is not None:
            forced.join(timeout=2)
        assert snapshot.close()
