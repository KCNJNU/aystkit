"""Host coordination with fake locks, processes, metadata and system commands."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector import robot_shutdown as shutdown
from collector.robot_shutdown_state import ROS_QUEUES


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def forbid_external_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Host unit tests must never execute a real command')
    monkeypatch.setattr(shutdown.subprocess, 'run', forbidden)
    monkeypatch.setattr(shutdown.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(shutdown.os, 'kill', forbidden)


def host():
    value = shutdown.Host.__new__(shutdown.Host)
    value.home = Path('/FAKE/home')
    value.values = {'ARCHIVE_ROOT': '/FAKE/archive'}
    value.control = Path('/FAKE/control')
    value.state = Path('/FAKE/state')
    value.root = Path('/FAKE/archive')
    value.boot_id = '11111111-1111-1111-1111-111111111111'
    value.boot_time = 90
    return value


def final_archive():
    return dict(updatedAt=iso(100), state='stopped', errors={},
        ros=dict(active=False, workerAlive=False, **dict.fromkeys(ROS_QUEUES, 0)),
        store=dict(active={}, errors={}),
        video=dict(pendingSegments=0, running=False, errors={}),
        upload=dict(pendingSegments=7, lastError='network_offline'))


def test_lock_capture_notifies_verified_owner_before_lock_wait_and_never_signals_itself(monkeypatch):
    value = host()
    events = []
    marker = dict(state='recording', bootId=value.boot_id, pid=17, processStart='456')
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: marker)
    monkeypatch.setattr(shutdown, 'process_identity', lambda pid: '456' if pid == 17 else None)
    monkeypatch.setattr(shutdown, 'safe_directory', lambda path, **k: path)
    class Session:
        marker = value.control / 'desktop-capture.json'
        def __init__(self, control): assert control == value.control
        def stop(self, values): events.append(('notify', values))
    @contextmanager
    def lock(path, **kwargs):
        events.append(('lock', path.name))
        yield
        events.append(('unlock', path.name))
    monkeypatch.setattr(shutdown, 'CaptureSession', Session)
    monkeypatch.setattr(shutdown, 'file_lock', lock)
    with ExitStack() as stack:
        value.lock_capture(stack)
        assert events[:4] == [('notify', value.values), ('lock', 'desktop-capture.lock'),
            ('lock', 'start-all-devices.lock'), ('lock', '.upload-only.lock')]
    assert events[-1] == ('unlock', 'desktop-capture.lock')


@pytest.mark.parametrize('change,notify', [
    ({'bootId': 'different'}, True), ({'processStart': 'old-start'}, True),
    ({'state': 'stopped'}, True), ({}, False)])
def test_old_or_unrequested_capture_owner_never_signalled(monkeypatch, change, notify):
    value = host()
    marker = dict(state='recording', bootId=value.boot_id, pid=17, processStart='456')
    marker.update(change)
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: marker)
    monkeypatch.setattr(shutdown, 'process_identity', lambda pid: '456')
    monkeypatch.setattr(shutdown, 'safe_directory', lambda path, **k: path)
    class Session:
        marker = value.control / 'desktop-capture.json'
        def __init__(self, control): pass
        def stop(self, values): raise AssertionError('must not signal old owner')
    @contextmanager
    def lock(*a, **k): yield
    monkeypatch.setattr(shutdown, 'CaptureSession', Session)
    monkeypatch.setattr(shutdown, 'file_lock', lock)
    with ExitStack() as stack:
        value.lock_capture(stack, notify=notify)


def test_close_gates_preserves_previous_metadata_and_never_changes_devices(monkeypatch):
    value = host()
    original = dict(enabled=True, desktopSession='abc', custom='preserve')
    writes = []
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: original)
    monkeypatch.setattr(shutdown, 'atomic_json', lambda path, data: writes.append((path, data)))
    monkeypatch.setattr(shutdown.time, 'time', lambda: 100)
    monkeypatch.setattr(shutdown, 'utc_now', lambda: iso(100))
    value.close_gates()
    assert [path.name for path, _ in writes] == ['garage.json', 'desktop-lease.json']
    assert all(data['enabled'] is False and data['expiresAt'] == 100
               and data['custom'] == 'preserve' for _, data in writes)
    assert original['enabled'] is True


def test_finish_native_waits_for_own_successful_stop_then_syncs_without_requiring_upload(monkeypatch):
    value = host()
    calls = []
    value.unit = lambda name: {'LoadState': 'loaded', 'ActiveState': 'active'}
    value.stop_unit = lambda name: calls.append(('stop', name)) or True
    value.command = lambda args, **kwargs: calls.append(('command', args)) or ''
    monkeypatch.setattr(shutdown.time, 'time', lambda: 100)
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: final_archive())
    result = value.finish_native()
    assert calls == [('stop', shutdown.NATIVE), ('command', ['/usr/bin/sync'])]
    assert result['state'] == 'locally_saved' and result['pendingUpload'] == 7
    assert result['warnings'] == ['archive_upload_deferred']


@pytest.mark.parametrize('mutate', [
    lambda report: report.update(updatedAt=iso(99)),
    lambda report: report.update(state='stop_failed'),
    lambda report: report['ros'].update(inFlightMessages=1),
    lambda report: report['store']['errors'].update(storage='full'),
])
def test_finish_native_never_syncs_or_proves_success_on_incomplete_stop(monkeypatch, mutate):
    value = host()
    report = final_archive()
    mutate(report)
    value.unit = lambda name: {'LoadState': 'loaded', 'ActiveState': 'active'}
    value.stop_unit = lambda name: True
    value.command = lambda *a, **k: pytest.fail('must not sync before verifying save')
    monkeypatch.setattr(shutdown.time, 'time', lambda: 100)
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: report)
    with pytest.raises(shutdown.ShutdownError, match='归档保存尚未确认'):
        value.finish_native()


def test_inactive_native_accepts_its_current_boot_final_status_but_not_previous_boot(monkeypatch):
    value = host()
    value.unit = lambda name: {'LoadState': 'loaded', 'ActiveState': 'inactive',
                             'ExecMainStartTimestampMonotonic': '5000000'}
    value.stop_unit = lambda name: False
    value.command = lambda *a, **k: ''
    report = final_archive()
    monkeypatch.setattr(shutdown.time, 'time', lambda: 120)
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: report)
    assert value.finish_native()['state'] == 'locally_saved'
    report['updatedAt'] = iso(89)
    with pytest.raises(shutdown.ShutdownError, match='归档保存尚未确认'):
        value.finish_native()


CAMERA_KEYS = ('usb-cameras', 'berxel', 'fisheye', 'berxel-preview',
               'robot-cloud-yolo-observer')


def camera_rows():
    return [dict(key=key, state='already_stopped') for key in CAMERA_KEYS]


def fake_camera_manager(monkeypatch, *, ok=True, scan_errors=(), holders=(), units=None,
                        report=None, rows=None):
    from deploy.scripts import device_sources
    snapshot = SimpleNamespace(scan_errors=scan_errors, holders=holders,
        units=units if units is not None else {'shared': SimpleNamespace(active=False)})
    owner = SimpleNamespace(CAPTURE_UNITS=('shared',),
                            SystemOps=lambda: SimpleNamespace(snapshot=lambda: snapshot))
    class Manager:
        def __init__(self, role): assert role == 'jetson'
        def stop_recording_cameras(self):
            return dict(ok=ok, devices=camera_rows() if rows is None else rows)
        def load_camera_owner(self): return owner
    monkeypatch.setattr(device_sources, 'RecordingDeviceSources', Manager)
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k: report)
    monkeypatch.setattr(shutdown.time, 'time', lambda: 100)


@pytest.mark.parametrize('kwargs', [dict(ok=False), dict(scan_errors=['permission']),
    dict(holders=[17]), dict(units={}), dict(units={'shared': SimpleNamespace(active=True)}),
    dict(units={'shared': SimpleNamespace(active=0)})])
def test_camera_shutdown_never_ignores_device_helper_or_snapshot_failures(monkeypatch, kwargs):
    fake_camera_manager(monkeypatch, **kwargs)
    with pytest.raises(shutdown.ShutdownError):
        host().close_cameras()


@pytest.mark.parametrize('rows', [[], camera_rows()[:-1],
    camera_rows()[:-1] + [dict(key='usb-cameras', state='already_stopped')],
    camera_rows()[:-1] + [dict(key='unexpected', state='already_stopped')],
    camera_rows()[:-1] + [dict(key=CAMERA_KEYS[-1], state='external_running')]])
def test_camera_top_level_ok_never_hides_missing_duplicate_or_failed_device_rows(monkeypatch, rows):
    fake_camera_manager(monkeypatch, rows=rows)
    with pytest.raises(shutdown.ShutdownError, match='相机未全部正常停止'):
        host().close_cameras()


def test_camera_missing_status_reports_unknown_history_not_never_started(monkeypatch):
    fake_camera_manager(monkeypatch, report=None)
    assert host().close_cameras() == dict(state='READY', warnings=['camera_archive_status_missing'])


def test_snapshot_preserves_unknown_fields_in_failed_recorder_report(monkeypatch):
    value = host()
    value.unit = lambda name: {'LoadState': 'loaded', 'ActiveState': 'failed'}
    monkeypatch.setattr(shutdown, 'read_json', lambda *a, **k:
        dict(updatedAt=iso(100), state='stop_failed', ros=None, store=None, upload=None))
    result = value.snapshot()
    assert result['archiveState'] == 'stop_failed'
    assert result['rosActive'] is None and result['activeSegments'] is None
    assert result['pendingUpload'] is None and result['storageErrors'] == []


def test_camera_current_boot_failed_capture_never_permitted(monkeypatch):
    report = dict(updatedAt=iso(99), state='stop_failed', store=None,
        sink=dict(enabled=False, writerRunning=False, queueFrames=0,
                  queueBytes=0, inFlightFrames=0, errors={}))
    fake_camera_manager(monkeypatch, report=report)
    with pytest.raises(shutdown.ShutdownError, match='camera_stop_failed'):
        host().close_cameras()


def test_camera_previous_boot_failure_only_warns_after_current_process_snapshot_closed(monkeypatch):
    report = dict(updatedAt=iso(89), state='stop_failed', store=None,
        sink=dict(enabled=False, writerRunning=False, queueFrames=0,
                  queueBytes=0, inFlightFrames=0, errors={}))
    fake_camera_manager(monkeypatch, report=report)
    result = host().close_cameras()
    assert result['state'] == 'READY'
    assert result['warnings'] == ['camera_previous_boot_capture_unconfirmed']


def test_stop_unit_rejects_failed_result_and_only_stops_exact_requested_native_unit():
    value = host()
    states = iter([dict(LoadState='loaded', ActiveState='active'),
                   dict(LoadState='loaded', ActiveState='failed', MainPID='0', Result='exit-code')])
    calls = []
    value.unit = lambda name: next(states)
    value.command = lambda args, **kwargs: calls.append(args) or ''
    with pytest.raises(shutdown.ShutdownError, match='服务未正常退出'):
        value.stop_unit(shutdown.NATIVE)
    assert calls == [['/usr/bin/systemctl', '--user', 'stop', shutdown.NATIVE]]


@pytest.mark.parametrize('phase', ['check', 'cancel', 'commit', 'wrong'])
def test_remote_check_cancel_or_wrong_phase_never_launches_authorization_or_changes_host(
        monkeypatch, capsys, phase):
    import json
    value = host()
    nonce = 'a' * 64
    value.snapshot = lambda: {'readOnly': True}
    monkeypatch.setattr(shutdown, 'Host', lambda: value)
    monkeypatch.setattr(shutdown.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(shutdown, 'JsonPeer', lambda process: SimpleNamespace(
        receive=lambda timeout: shutdown.transaction_message(phase, nonce, value.boot_id)))
    monkeypatch.setattr(shutdown, 'spawn_peer', lambda *a: pytest.fail('no root authorization allowed'))
    result = shutdown.remote_main(nonce)
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert messages[0]['phase'] == 'hello'
    assert result == (0 if phase in ('check', 'cancel') else 2)
    assert messages[-1]['phase'] == ('checked' if result == 0 else 'error')


def test_remote_seal_failure_never_sends_commit_to_power_helper(monkeypatch, capsys):
    import json
    value = host()
    nonce = 'a' * 64
    events = []
    value.snapshot = lambda: {'readOnly': True}
    value.lock_capture = lambda stack, **kwargs: events.append('lock')
    value.close_gates = lambda: events.append('gates')
    value.close_cameras = lambda: events.append('cameras') or {'state': 'READY'}
    def unfinished():
        events.append('native_failed')
        raise shutdown.ShutdownError('unsaved fixture')
    value.finish_native = unfinished
    phases = iter(['authorize', 'seal'])
    monkeypatch.setattr(shutdown, 'Host', lambda: value)
    monkeypatch.setattr(shutdown.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(shutdown, 'JsonPeer', lambda process: SimpleNamespace(receive=lambda timeout:
        shutdown.transaction_message(next(phases), nonce, value.boot_id)))
    root = SimpleNamespace(
        receive=lambda timeout: shutdown.transaction_message('ready', nonce, value.boot_id),
        send=lambda message: pytest.fail('unsaved data must never commit power'),
        close=lambda: events.append('root_closed'))
    monkeypatch.setattr(shutdown, 'spawn_peer', lambda *a: root)
    monkeypatch.setattr(shutdown, 'root_command', lambda token: ['FAKE_ROOT', token])
    assert shutdown.remote_main(nonce) == 2
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [message['phase'] for message in messages] == ['hello', 'authorized', 'error']
    assert events == ['lock', 'gates', 'cameras', 'native_failed', 'root_closed']
