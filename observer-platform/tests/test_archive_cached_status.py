"""Periodic derivative telemetry must never join filesystem discovery work."""
from pathlib import Path
import threading
import time

import pytest

from collector.archive_video import VideoFinalizer
from collector.archive_bag_compress import BagCompressionFinalizer


@pytest.fixture(params=[(VideoFinalizer, 'video-pending.json'),
                       (BagCompressionFinalizer, 'bag-pending.json')])
def finalizer(request, tmp_path):
    cls, name = request.param
    return cls(tmp_path, 'fixture'), name


def test_cached_status_before_discovery_is_unknown_and_cannot_scan(finalizer, monkeypatch):
    worker, name = finalizer
    def forbidden(*_args, **_kwargs): pytest.fail('cached status attempted filesystem discovery')
    monkeypatch.setattr(worker, 'pending_candidates', forbidden)
    monkeypatch.setattr(worker, '_selected_errors', forbidden)
    with monkeypatch.context() as scoped:
        for method in ('stat', 'is_file', 'is_dir', 'is_symlink', 'exists', 'read_text', 'rglob'):
            scoped.setattr(Path, method, forbidden)
        result = worker.status(cached=True)
    assert result['discoveryReady'] is False
    assert result['pendingSegments'] is None
    assert result['discoveryAgeS'] is None
    assert result['terminalPendingSegments'] == 0


def test_cached_status_ignores_discovery_and_policy_locks_while_scan_blocks(finalizer, monkeypatch):
    worker, name = finalizer
    old = worker.root/'days/2026-11-04/原相机前/one/id'/name
    worker.pending_cache = (old,)
    worker.pending_scanned_at = time.monotonic()
    worker.counters['finalizedSegments'] = 3
    key = str(old.parent.relative_to(worker.root/'days'))
    worker.errors[key] = 'source-invalid'
    worker.deferred.add(key)
    worker.camera_only = True
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    response = []
    def slow_scan():
        entered.set()
        assert release.wait(3)
        return []
    worker._candidates = slow_scan
    def discovery():
        with worker.state_lock:
            worker.pending_candidates(force=True)
    def status():
        response.append(worker.status(cached=True));returned.set()
    scanner=threading.Thread(target=discovery)
    reader=threading.Thread(target=status)
    try:
        scanner.start()
        assert entered.wait(1)
        monkeypatch.setattr(worker, '_selected_errors', lambda: pytest.fail('cached status rechecked a policy file'))
        reader.start()
        assert returned.wait(.5), 'telemetry waited on discovery/state locks'
        assert not release.is_set()
        result = response[0]
        assert result['pendingSegments'] == result['terminalPendingSegments'] == 1
        assert result['finalizedSegments'] == 3
        assert result['errors'] == {key: 'source-invalid'}
    finally:
        release.set();scanner.join(2);reader.join(2)
    assert worker.status(cached=True)['pendingSegments'] == 0


def test_cached_status_uses_last_inventory_while_default_status_remains_fresh(finalizer, monkeypatch):
    worker, name = finalizer
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '0')
    pending=worker.root/'days/2026-11-04/原相机前/one/id'/name
    pending.parent.mkdir(parents=True)
    pending.write_text('{}')
    assert worker.status()['pendingSegments'] == 1
    pending.unlink()
    # Cached telemetry is intentionally the last discovery, not a stop proof.
    assert worker.status(cached=True)['pendingSegments'] == 1
    assert worker.status()['pendingSegments'] == 0
    pending.write_text('{}')
    worker.refresh_pending()
    assert worker.status(cached=True)['pendingSegments'] == 1


def test_camera_only_cached_errors_include_only_previously_selected_sources(finalizer):
    worker, name = finalizer
    worker.camera_only=True
    worker.pending_cache=()
    worker.pending_scanned_at=time.monotonic()
    worker.errors={'camera-allowed': 'camera-error', 'radar-excluded': 'radar-error'}
    worker.selected_error_keys=frozenset({'camera-allowed'})
    result=worker.status(cached=True)
    assert result['errors'] == {'camera-allowed': 'camera-error'}
    result['errors'].clear()
    assert worker.errors['camera-allowed'] == 'camera-error'
