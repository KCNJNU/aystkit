"""Desktop recording releases only its own auxiliaries, never shared devices."""
import json
import shlex
import subprocess

import pytest

from collector import archive_devices as devices
from collector.archive_capture_session import CaptureSession
from collector.archive_core import atomic_json


KEYS = ('usb-cameras', 'berxel', 'fisheye', 'berxel-preview', 'robot-cloud-yolo-observer')


def lease():
    return dict(schemaVersion=1, units=[dict(unit='robot-berxel-preview-encoder.service',
        invocationId='a' * 32, mainPid=1234, execMainStartTimestampMonotonic='123456789')])


def completed(*, state='retained', malformed=False):
    rows = [dict(key=key, state=state) for key in KEYS]
    if malformed:
        rows.pop()
    return subprocess.CompletedProcess([], 0, json.dumps(dict(ok=True, role='jetson',
        stopOwnedCameras=True, dryRun=False, devices=rows)).encode(), b'')


@pytest.mark.parametrize('ownership', [None, dict(schemaVersion=1, units=[])])
def test_missing_or_legacy_ownership_never_contacts_or_stops_camera_host(ownership):
    result = devices.stop_owned_capture_cameras({}, ownership=ownership,
        runner=lambda *_a, **_kw: pytest.fail('contacted camera host without ownership'))
    assert result['ok']
    assert {row['key'] for row in result['devices']} == set(KEYS)
    assert all(row['state'] == 'retained' for row in result['devices'])


@pytest.mark.parametrize('ownership', [[], {}, dict(schemaVersion=2, units=[]),
    dict(schemaVersion=1, units='bad'), dict(schemaVersion=1, units=['x' * 17000])])
def test_invalid_ownership_fails_closed_without_dispatch(ownership):
    with pytest.raises(devices.DeviceStartupError):
        devices.stop_owned_capture_cameras({}, ownership=ownership,
            runner=lambda *_a, **_kw: pytest.fail('dispatched invalid ownership'))


def test_scoped_stop_passes_exact_lease_and_accepts_retained_shared_sources():
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        return completed()
    ownership = lease()
    result = devices.stop_owned_capture_cameras({}, ownership=ownership, runner=runner)
    assert result['ok'] and len(calls) == 1
    command, options = calls[0]
    remote = shlex.split(command[-1])
    assert '--stop-owned-recording-cameras' in remote
    assert '--stop-recording-cameras' not in remote
    assert json.loads(remote[remote.index('--camera-ownership-json') + 1]) == ownership
    assert 'StrictHostKeyChecking=yes' in command
    assert options['payload'] == devices.HELPER.read_bytes()


def test_scoped_stop_does_not_accept_incomplete_confirmation():
    with pytest.raises(devices.DeviceStartupError):
        devices.stop_owned_capture_cameras({}, ownership=lease(),
            runner=lambda *_a, **_kw: completed(malformed=True))


def test_scoped_stop_does_not_turn_an_auxiliary_failure_into_success():
    report = devices.stop_owned_capture_cameras({}, ownership=lease(),
        runner=lambda *_a, **_kw: completed(state='failed'))
    assert report['ok'] is False


def test_other_entry_gate_keeps_even_owned_auxiliaries_running(tmp_path, monkeypatch):
    from collector import archive_capture_session as capture
    session = CaptureSession(tmp_path)
    session.device_startup = dict(hosts=dict(jetson=dict(cameraOwnership=lease())))
    atomic_json(session.web_gate, dict(schemaVersion=1, scope='garage', enabled=True, expiresAt=None))
    monkeypatch.setattr(capture, 'stop_capture_cameras',
        lambda *_a, **_kw: pytest.fail('stopped another active recording'))
    assert session.close_cameras({})
    assert session.camera_shutdown['sharedSourcesRetained'] is True


def test_failed_startup_cannot_borrow_ownership_from_a_previous_marker(tmp_path, monkeypatch):
    from collector import archive_capture_session as capture
    session = CaptureSession(tmp_path)
    atomic_json(session.marker, dict(token='b' * 32,
        deviceStartup=dict(hosts=dict(jetson=dict(cameraOwnership=lease())))))
    received = []
    def finish(values, **options):
        received.append(options['ownership'])
        return devices.stop_owned_capture_cameras(values, **options,
            runner=lambda *_a, **_kw: pytest.fail('old lease used for failed new startup'))
    monkeypatch.setattr(capture, 'stop_capture_cameras', finish)
    assert session.close_cameras({})
    assert received == [None]
