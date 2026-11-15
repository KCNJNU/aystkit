"""Real read-only child probes; no ROS participant, device or runtime service."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from collector import archive_resources
from collector.archive_core import ArchiveStore
from collector.archive_resources import (IsolatedInventoryProbe, ProbeCancelled,
                                         StorageMonitor, TreeChanged)
from tests.test_archive_resources import Writer, wait_for


def command(probe, code):
    probe._command = lambda _root: [sys.executable, '-B', '-c', code]


def test_real_child_scans_original_files_and_never_follows_external_links(tmp_path):
    root, outside = tmp_path/'spool', tmp_path/'outside'
    root.mkdir(); outside.mkdir()
    original = root/'video.mkv'; original.write_bytes(b'original-native-video')
    (outside/'other').write_bytes(b'not in this inventory')
    (root/'link').symlink_to(outside, target_is_directory=True)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    probe = IsolatedInventoryProbe()
    assert probe(root, threading.Event()) == len(b'original-native-video')
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    assert probe.child is None and probe.close()


def test_real_missing_root_preserves_the_strict_scanners_tree_changed_error(tmp_path):
    probe = IsolatedInventoryProbe()
    with pytest.raises(TreeChanged):
        probe(tmp_path/'missing', threading.Event())
    assert probe.child is None


@pytest.mark.parametrize('name,kind', [('TreeChanged', TreeChanged), ('OSError', OSError),
                                     ('PermissionError', PermissionError)])
def test_child_error_protocol_preserves_fail_closed_categories(tmp_path, name, kind):
    probe = IsolatedInventoryProbe()
    command(probe, 'import sys;print('+repr(json.dumps(dict(ok=False,error=name)))+');sys.exit(1)')
    with pytest.raises(kind):
        probe(tmp_path, threading.Event())
    assert probe.child is None


@pytest.mark.parametrize('payload,code', [
    ('not JSON', 0), ('[]', 0), ('{"ok":1,"bytes":3}', 0),
    ('{"ok":true,"bytes":true}', 0), ('{"ok":true,"bytes":-1}', 0),
    ('{"ok":true,"bytes":1,"extra":1}', 0),
    ('{"ok":false,"ok":true,"bytes":1}', 0),
    ('{"ok":false,"error":[]}', 1), ('{"ok":false,"error":"unknown"}', 1),
    ('{"ok":true,"bytes":1}', 7), ('{"ok":false,"error":"TreeChanged"}', 0),
])
def test_invalid_child_output_never_certifies_available_storage(tmp_path, payload, code):
    probe = IsolatedInventoryProbe()
    command(probe, 'import sys;print('+repr(payload)+');sys.exit('+str(code)+')')
    with pytest.raises(OSError):
        probe(tmp_path, threading.Event())
    assert probe.child is None


@pytest.mark.parametrize('stream', ('stdout', 'stderr'))
def test_worker_output_is_bounded_before_it_is_accumulated(tmp_path, stream):
    probe = IsolatedInventoryProbe()
    command(probe, 'import sys;sys.'+stream+'.write("x"*1000000);sys.'+stream+'.flush()')
    with pytest.raises(OSError, match='exceeded limit'):
        probe(tmp_path, threading.Event())
    assert probe.child is None


def test_deadline_terminates_only_the_owned_child(tmp_path):
    probe = IsolatedInventoryProbe(timeout=.1)
    command(probe, 'import time;time.sleep(10)')
    control = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(10)'])
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            probe(tmp_path, threading.Event())
        assert time.monotonic()-started < 2
        assert probe.child is None and control.poll() is None
    finally:
        control.terminate(); control.wait(timeout=2)


def test_close_cancels_only_this_probe_and_reclaims_its_worker(tmp_path):
    probe = IsolatedInventoryProbe()
    command(probe, 'import time;time.sleep(10)')
    errors = []
    def scan():
        try:
            probe(tmp_path, threading.Event())
        except Exception as error:
            errors.append(type(error))
    worker = threading.Thread(target=scan)
    worker.start()
    wait_for(lambda: probe.child is not None)
    owned = probe.child
    try:
        assert probe.close()
        worker.join(timeout=2)
        assert not worker.is_alive() and owned.poll() is not None
        assert errors == [ProbeCancelled] and probe.child is None
    finally:
        probe.close(); worker.join(timeout=2)


def test_expired_parent_baseline_cannot_launch_a_fresh_probe(tmp_path, monkeypatch):
    probe = IsolatedInventoryProbe(monotonic=lambda: 16)
    monkeypatch.setattr(archive_resources.subprocess, 'Popen',
        lambda *_args, **_options: pytest.fail('expired baseline launched a new child'))
    with pytest.raises(TimeoutError):
        probe(tmp_path, threading.Event(), started_at=0)


def test_late_valid_result_cannot_refresh_the_original_parent_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    probe = IsolatedInventoryProbe(monotonic=lambda: clock[0])
    command(probe, 'print("{\\"ok\\":true,\\"bytes\\":7}")')
    original = archive_resources.selectors.DefaultSelector.select
    def late_select(selector, timeout=None):
        result = original(selector, timeout)
        if result:
            clock[0] = 16  # Parent resumes after its original 15-second limit.
        return result
    monkeypatch.setattr(archive_resources.selectors.DefaultSelector, 'select', late_select)
    with pytest.raises(TimeoutError):
        probe(tmp_path, threading.Event(), started_at=0)
    assert probe.child is None


def test_ros_store_can_explicitly_use_an_isolated_inventory_without_changing_raw_bytes(tmp_path):
    store = ArchiveStore(tmp_path, 'fixture', writer_factory=Writer, compression=False,
        min_free_bytes=1, max_spool_bytes=1000000, background_resources=True,
        resource_isolate_inventory=True)
    try:
        wait_for(lambda: store.resource_monitor.snapshot()['ready'])
        assert store.snapshot()['resourceMonitor']['isolatedInventory'] is True
        assert store.record_accepted('雷达MID360', '/livox/lidar', 'CustomMsg', b'raw-original', 1)
    finally:
        store.close()
    assert next(tmp_path.rglob('data.db3')).read_bytes() == b'raw-original'
    assert store.resource_monitor.close()


def test_monitor_close_interrupts_an_isolated_inventory_child(tmp_path):
    monitor = StorageMonitor(tmp_path, isolate_inventory=True)
    command(monitor.inventory_probe, 'import time;time.sleep(10)')
    monitor.start()
    wait_for(lambda: monitor.inventory_probe.child is not None)
    owned = monitor.inventory_probe.child
    assert monitor.close(timeout=2)
    assert owned.poll() is not None and monitor.inventory_probe.child is None
    assert not monitor.thread.is_alive() and not monitor.spool_thread.is_alive()


def test_default_and_custom_scans_remain_in_process_until_explicitly_enabled(tmp_path):
    for options in ({}, {'scan': lambda *_args: 0}):
        monitor = StorageMonitor(tmp_path, **options)
        assert monitor.inventory_probe is None
        assert monitor.snapshot()['isolatedInventory'] is False
        assert monitor.close()
    with pytest.raises(ValueError):
        StorageMonitor(tmp_path, scan=lambda *_args: 0, isolate_inventory=True)


@pytest.mark.parametrize('phase', ('terminate', 'kill'))
def test_child_natural_exit_during_stop_is_safe_without_broad_error_suppression(phase):
    class ExitedChild:
        calls = 0
        def poll(self):
            return None if self.calls == 0 else 0
        def terminate(self):
            if phase == 'terminate':
                self.calls += 1
                raise ProcessLookupError('natural exit before signal')
        def wait(self, timeout):
            if phase == 'kill' and self.calls == 0:
                self.calls += 1
                raise subprocess.TimeoutExpired('fixture-child', timeout)
            return 0
        def kill(self):
            raise ProcessLookupError('natural exit before escalation')
    assert IsolatedInventoryProbe._stop_owned(ExitedChild())


def test_child_termination_permission_errors_are_not_suppressed():
    class InaccessibleChild:
        def poll(self):return None
        def terminate(self):raise PermissionError('unexpected ownership change')
    with pytest.raises(PermissionError):
        IsolatedInventoryProbe._stop_owned(InaccessibleChild())
