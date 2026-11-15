"""Camera shutdown ownership and error paths, with hardware helpers isolated."""
import signal
from types import SimpleNamespace

import pytest

from collector import archive_capture_session as capture
from collector.archive_core import atomic_json


@pytest.fixture
def setup(tmp_path, monkeypatch):
    session = capture.CaptureSession(tmp_path/'control')
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    handlers = {}
    monkeypatch.setattr(capture.signal, 'signal', lambda number, handler: handlers.setdefault(number, handler))
    calls = []
    monkeypatch.setattr(session, 'ensure_recorders', lambda: calls.append('recorders'))
    monkeypatch.setattr(session, 'wait_ready', lambda _: calls.append('ready'))
    def sealed(_):
        assert session.read(session.gate)['enabled'] is False
        calls.append('sealed')
        return True
    monkeypatch.setattr(session, 'wait_sealed', sealed)
    def observe():
        session.video_sources.add('fixture-video')
        session.lidar_sources.add('/livox/lidar')
    monkeypatch.setattr(session, 'observe_sources', observe)
    def start(_values, **_options):
        calls.append('start-devices')
        return dict(ok=True, hosts={})
    monkeypatch.setattr(capture, 'start_capture_devices', start)
    def inspect(_values, **_options):
        calls.append('inspect-radar')
        return dict(ok=True, role='ipc', recordingOnly=True,
                    readOnlySources=True, sourceControl=False, devices=[])
    monkeypatch.setattr(capture, 'inspect_capture_radar', inspect)
    def stop(_values, **_options):
        # Normal cleanup releases recording before closing camera owners.
        assert session.read(session.gate).get('enabled') is not True
        calls.append('stop-cameras')
        return dict(ok=True, devices=[dict(key='berxel', state='stopped')], errors=[])
    monkeypatch.setattr(capture, 'stop_capture_cameras', stop)
    return session, calls, handlers


@pytest.mark.parametrize('end', ['duration', 'enter', 'sigint', 'sigterm', 'sighup'])
def test_all_normal_exit_paths_seal_then_close_cameras_once(setup, monkeypatch, end):
    session, calls, handlers = setup
    args = ['--duration', '.02', '--start-cameras']
    if end == 'enter':
        monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: True, readline=lambda: '\n'))
        monkeypatch.setattr(capture.select, 'select', lambda *_: ([capture.sys.stdin], [], []))
    elif end.startswith('sig'):
        number = {'sigint': signal.SIGINT, 'sigterm': signal.SIGTERM, 'sighup': signal.SIGHUP}[end]
        monkeypatch.setattr(capture.time, 'sleep', lambda _: handlers[number]())
    assert capture.main(args) == 0
    assert calls == ['recorders', 'ready', 'start-devices', 'sealed', 'stop-cameras']
    marker = session.read(session.marker)
    assert marker['state'] == 'stopped' and marker['cameraShutdown']['ok'] is True


@pytest.mark.parametrize('options', [[], ['--no-device-start']])
def test_passive_capture_never_starts_or_stops_camera_sources(setup, monkeypatch, options):
    session, calls, _handlers = setup
    monkeypatch.setattr(session, 'ensure_devices', lambda *_args, **_options: pytest.fail('started cameras'))
    monkeypatch.setattr(session, 'close_cameras', lambda *_args, **_options: pytest.fail('closed cameras'))
    assert capture.main(['--duration', '.01', *options]) == 0
    assert calls == ['recorders', 'ready', 'inspect-radar', 'sealed']
    assert session.read(session.marker)['cameraShutdown'] == dict(ok=True, skipped=True, reason='passive_sources')


@pytest.mark.parametrize('stage', ['recorders', 'ready', 'start-devices', 'recording', 'observation', 'sealed'])
def test_failure_never_skips_camera_shutdown(setup, monkeypatch, stage):
    session, calls, _handlers = setup
    def fail(*_args, **_options):
        raise OSError('fixture ' + stage)
    if stage == 'recorders':
        monkeypatch.setattr(session, 'ensure_recorders', fail)
    elif stage == 'ready':
        monkeypatch.setattr(session, 'wait_ready', fail)
    elif stage == 'start-devices':
        monkeypatch.setattr(capture, 'start_capture_devices', fail)
    elif stage == 'recording':
        renew = session.renew
        monkeypatch.setattr(session, 'renew', lambda **options: renew(**options) if options.get('first') else fail())
    elif stage == 'observation':
        monkeypatch.setattr(session, 'observe_sources', fail)
    else:
        monkeypatch.setattr(session, 'wait_sealed', fail)
    assert capture.main(['--duration', '.01', '--start-cameras']) == 2
    assert calls.count('stop-cameras') == 1
    assert session.read(session.gate).get('enabled') is not True


@pytest.mark.parametrize('stage', ['recorders', 'ready', 'start-devices'])
def test_startup_cancel_closes_cameras_without_arming_lease(setup, monkeypatch, stage):
    session, calls, handlers = setup
    def cancel(*_args, **_options):
        handlers[signal.SIGTERM]()
        if stage == 'start-devices':
            raise capture.DeviceStartupCancelled()
    if stage == 'recorders':
        monkeypatch.setattr(session, 'ensure_recorders', cancel)
    elif stage == 'ready':
        monkeypatch.setattr(session, 'wait_ready', cancel)
    else:
        monkeypatch.setattr(capture, 'start_capture_devices', cancel)
    assert capture.main(['--duration', '.01', '--start-cameras']) == 0
    assert calls.count('stop-cameras') == 1
    assert not session.gate.exists() and not session.marker.exists()


@pytest.mark.parametrize('failure', ['reported', 'exception', 'malformed'])
def test_unconfirmed_shutdown_never_reports_success(setup, monkeypatch, capsys, failure):
    session, _calls, _handlers = setup
    def stop(*_args, **_options):
        if failure == 'exception':
            raise capture.DeviceStartupError('private camera link unavailable')
        return {} if failure == 'malformed' else dict(ok=False, devices=[dict(key='berxel', state='failed')])
    monkeypatch.setattr(capture, 'stop_capture_cameras', stop)
    assert capture.main(['--duration', '.01', '--start-cameras']) == 3
    assert session.read(session.marker)['cameraShutdown']['ok'] is False
    result = capsys.readouterr()
    assert '已确认全部相机关闭' not in result.out
    assert '未确认' in result.err


@pytest.mark.parametrize('mode', [['--status'], ['--dry-run'], ['--dry-run', '--stop']])
def test_status_and_dry_run_do_not_touch_cameras(setup, mode):
    _session, calls, _handlers = setup
    assert capture.main(mode) == 0
    assert calls == []


@pytest.mark.parametrize('state', ['missing', 'stopped', 'old-process'])
def test_stop_with_no_owned_camera_lease_does_not_call_camera_operations(setup, monkeypatch, state):
    session, calls, _handlers = setup
    if state != 'missing':
        atomic_json(session.marker, dict(state='recording' if state == 'old-process' else 'stopped',
            pid=123, processStart='stale', bootId='old-boot'))
    monkeypatch.setattr(capture.os, 'kill', lambda *_: pytest.fail('signalled unrelated process'))
    assert capture.main(['--stop']) == 0
    assert calls == []


def test_rejected_existing_capture_does_not_shut_down_another_session(setup):
    session, calls, _handlers = setup
    atomic_json(session.web_gate, dict(schemaVersion=1, scope='garage', enabled=True, expiresAt=None))
    assert capture.main(['--duration', '.01']) == 2
    assert calls == []


def test_disconnected_terminal_does_not_prevent_camera_cleanup(setup, monkeypatch):
    session, calls, _handlers = setup
    class ClosedTerminal:
        def write(self, _value):
            raise BrokenPipeError('fixture terminal closed')
        def flush(self):
            raise BrokenPipeError('fixture terminal closed')
    monkeypatch.setattr(capture.sys, 'stdout', ClosedTerminal())
    monkeypatch.setattr(capture.sys, 'stderr', ClosedTerminal())
    assert session.close_cameras({})
    assert calls == ['stop-cameras']


def test_real_pty_hangup_without_sighup_seals_and_closes_cameras(setup, monkeypatch):
    """Losing isatty must end an interactive capture, never switch it to headless."""
    import os
    import pty
    session, calls, _handlers = setup
    master, slave = pty.openpty()
    stream = os.fdopen(slave, 'r')
    monkeypatch.setattr(capture.sys, 'stdin', stream)
    observe = session.observe_sources
    hung_up = False
    def lose_terminal():
        nonlocal hung_up
        observe()
        if not hung_up:
            os.close(master)
            hung_up = True
    monkeypatch.setattr(session, 'observe_sources', lose_terminal)
    monkeypatch.setattr(capture.time, 'sleep',
        lambda _: pytest.fail('disconnected interactive capture became unattended headless capture'))
    try:
        assert capture.main(['--start-cameras']) == 0
        assert hung_up
        assert calls == ['recorders', 'ready', 'start-devices', 'sealed', 'stop-cameras']
        assert session.read(session.gate)['enabled'] is False
        assert session.read(session.marker)['cameraShutdown']['ok'] is True
        proof = session.read(session.control/'session-ends'/(session.token+'.json'))
        assert proof['state'] == 'sealed'
    finally:
        if not hung_up:
            os.close(master)
        stream.close()


def test_terminal_lost_during_startup_never_arms_recording_lease(setup, monkeypatch):
    session, calls, _handlers = setup
    connected = True
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: connected))
    def lose_terminal(_):
        nonlocal connected
        calls.append('ready')
        connected = False
    monkeypatch.setattr(session, 'wait_ready', lose_terminal)
    assert capture.main(['--start-cameras']) == 0
    assert calls == ['recorders', 'ready', 'stop-cameras']
    assert not session.gate.exists()
