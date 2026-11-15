"""Capture teardown cannot stop shared algorithm inputs or another invocation."""
import json
from types import SimpleNamespace

import pytest

from deploy.scripts import device_sources as sources


def completed(stdout='', code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr='')


def lease(unit=sources.BERXEL_PREVIEW_UNIT, **changes):
    row = dict(unit=unit, invocationId='a' * 32, mainPid=12,
               execMainStartTimestampMonotonic='2000000')
    row.update(changes)
    return dict(schemaVersion=1, units=[row])


def identity(row, **changes):
    value = dict(LoadState='loaded', ActiveState='active',
        InvocationID=row['invocationId'], MainPID=str(row['mainPid']),
        ExecMainStartTimestampMonotonic=row['execMainStartTimestampMonotonic'],
        **{key: '' for key in sources.CAMERA_STOP_DEPENDENCIES})
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def forbid_live(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('camera ownership test invoked a live system operation')
    monkeypatch.setattr(sources.subprocess, 'run', forbidden)
    monkeypatch.setattr(sources.os, 'kill', forbidden)


@pytest.fixture
def manager(tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail('unexpected device command')
    return sources.RecordingDeviceSources('jetson', home=tmp_path, runner=forbidden)


def gate(manager, *, enabled=False, desktop=None):
    control = manager.home / '.local/state/robot-cloud/garage-control'
    control.mkdir(parents=True, exist_ok=True)
    (control / 'garage.json').write_text(json.dumps(dict(schemaVersion=1, scope='garage', enabled=enabled)))
    if desktop is not None:
        (control / 'desktop-lease.json').write_text(json.dumps(desktop))


@pytest.mark.parametrize('unit', sorted(sources.SHARED_CAMERA_SOURCE_UNITS))
def test_hardware_source_remains_running_even_when_this_capture_created_it(manager, unit):
    report = manager.stop_owned_recording_cameras(lease(unit))
    assert report['ok'] and report['stopOwnedCameras']
    row = next(row for row in report['devices'] if row['unit'] == unit)
    assert row['state'] == 'retained'
    assert row['reason'] == 'shared_source_may_be_in_use'


@pytest.mark.parametrize('bad', [
    {'schemaVersion': True, 'units': []}, {'schemaVersion': 1, 'units': [{}]},
    lease('robot-device-mid360.service'), lease(invocationId='0' * 32),
    lease(mainPid=True), lease(execMainStartTimestampMonotonic='0'),
    lease(invocationId='a' * 33), {'schemaVersion': 1, 'units': lease()['units'] * 2},
    '{"schemaVersion":1,"units":[],"units":[]}', ' ' * 16385,
])
def test_invalid_payload_fails_before_any_command(manager, bad):
    with pytest.raises(sources.SourceError):
        manager.stop_owned_recording_cameras(bad)


def test_empty_legacy_ownership_never_closes_any_camera(manager):
    rows = manager.stop_owned_recording_cameras(dict(schemaVersion=1, units=[]))['devices']
    assert len(rows) == 5 and all(row['state'] == 'retained' for row in rows)


def test_live_web_gate_preserves_even_owned_optional_worker(manager):
    gate(manager, enabled=True)
    report = manager.stop_owned_recording_cameras(lease())
    assert report['ok'] and report['devices'][0]['reason'] == 'shared_capture_enabled'


def test_another_desktop_lease_preserves_owned_optional_worker(manager):
    gate(manager, desktop=dict(enabled=True, expiresAt=sources.time.time() + 25))
    report = manager.stop_owned_recording_cameras(lease())
    assert report['ok'] and report['devices'][0]['reason'] == 'shared_capture_enabled'


def test_missing_gate_retains_worker_without_command(manager):
    report = manager.stop_owned_recording_cameras(lease())
    assert report['ok'] and report['devices'][0]['reason'] == 'camera_ownership_unconfirmed'


@pytest.mark.parametrize('changes', [dict(InvocationID='b' * 32), dict(MainPID='13'),
    dict(ExecMainStartTimestampMonotonic='3000000'), dict(ActiveState='activating')])
def test_replaced_or_restarting_unit_is_preserved(manager, changes):
    gate(manager)
    payload = lease()
    manager.camera_unit_identity = lambda _: identity(payload['units'][0], **changes)
    report = manager.stop_owned_recording_cameras(payload)
    assert report['devices'][0]['reason'] == 'camera_identity_changed'


@pytest.mark.parametrize('dependency', sources.CAMERA_STOP_DEPENDENCIES)
def test_any_unconfirmed_or_propagating_stop_dependency_retains_worker(manager, dependency):
    gate(manager)
    payload = lease()
    manager.camera_unit_identity = lambda _: identity(payload['units'][0], **{dependency: 'algorithm.service'})
    assert manager.stop_owned_recording_cameras(payload)['devices'][0]['reason'] == 'camera_stop_dependencies_unconfirmed'


def test_gate_reenabled_between_identity_and_stop_preserves_worker(manager):
    payload = lease()
    checks = iter((False, True))
    manager.shared_capture_enabled = lambda: next(checks)
    manager.camera_unit_identity = lambda _: identity(payload['units'][0])
    assert manager.stop_owned_recording_cameras(payload)['devices'][0]['reason'] == 'shared_capture_enabled'


@pytest.mark.parametrize('unit', [sources.BERXEL_PREVIEW_UNIT, sources.YOLO_UNIT])
def test_only_owned_unchanged_unshared_auxiliary_worker_is_stopped(manager, unit):
    gate(manager)
    payload = lease(unit)
    calls = []
    manager.camera_unit_identity = lambda _: identity(payload['units'][0])
    manager.stop_berxel_preview = lambda: calls.append(unit) or dict(state='stopped')
    manager.stop_support = lambda name: calls.append(name) or dict(state='stopped')
    report = manager.stop_owned_recording_cameras(payload)
    row = next(row for row in report['devices'] if row['unit'] == unit)
    assert report['ok'] and row['state'] == 'stopped'
    assert calls == [unit]


def test_missing_unit_already_stopped_without_stop_operation(manager):
    gate(manager)
    manager.camera_unit_identity = lambda _: dict(LoadState='not-found', ActiveState='inactive')
    assert manager.stop_owned_recording_cameras(lease())['devices'][0]['state'] == 'already_stopped'


def test_dry_run_reads_no_gate_or_service(manager):
    report = manager.stop_owned_recording_cameras(lease(), dry_run=True)
    assert report['ok'] and report['dryRun'] and report['devices'][0]['reason'] == 'dry_run'


def test_only_successful_actual_start_claims_new_invocation(manager, monkeypatch):
    monkeypatch.setattr(sources.time, 'monotonic_ns', lambda: 1000000000)
    payload = lease()
    state = identity(payload['units'][0])
    manager.runner = lambda *_args, **_kwargs: completed('\n'.join(k + '=' + v for k, v in state.items()))
    manager.run(['systemd-run', '--user', '--unit=' + sources.BERXEL_PREVIEW_UNIT, '--', '/fixture'])
    manager.remember_camera_ownership()
    assert manager.camera_ownership() == payload


@pytest.mark.parametrize('failure', ['predates', 'failed-start', 'missing-identity'])
def test_ambiguous_start_is_never_claimed(manager, monkeypatch, failure):
    monkeypatch.setattr(sources.time, 'monotonic_ns', lambda: 3000000000)
    state = identity(lease()['units'][0])
    if failure == 'missing-identity':
        state.pop('InvocationID')
    manager.runner = lambda command, **_kwargs: completed('\n'.join(k + '=' + v for k, v in state.items()),
        code=1 if command[0] == 'systemd-run' and failure == 'failed-start' else 0)
    manager.run(['systemd-run', '--user', '--unit=' + sources.BERXEL_PREVIEW_UNIT, '--', '/fixture'])
    manager.remember_camera_ownership()
    assert manager.camera_ownership()['units'] == []


def test_reused_sources_do_not_acquire_any_ownership(manager):
    manager.support = lambda name, status=False: dict(key=name.removesuffix('.service'), state='reused')
    manager.recording_source = lambda source, status=False: dict(key=source.key, state='reused')
    manager.camera = lambda *args, **kwargs: dict(key='usb-cameras', state='reused')
    manager.fisheye = lambda status=False: dict(key='fisheye', state='reused')
    report = manager.recording_only()
    assert report['ok'] and report['cameraOwnership'] == dict(schemaVersion=1, units=[])


def test_new_partial_usb_source_is_recorded_but_reused_partial_source_is_not(manager, monkeypatch):
    from tests.test_device_sources import FakeOwner
    monkeypatch.setattr(sources.time, 'monotonic_ns', lambda: 1000000000)
    owner = FakeOwner()
    owner.wait_healthy = lambda: SimpleNamespace(units={}, healthy=True,
        available=('camera1', 'camera2'), missing_cameras=('camera3', 'camera4'))
    manager.owner_module = owner
    payload = lease(sources.SHARED_CAMERA_UNIT)
    manager.camera_unit_identity = lambda _: identity(payload['units'][0])
    result = manager.camera(require_archive=False)
    assert result['state'] == 'partial' and result['dataVerified']
    assert manager.camera_ownership() == payload
    assert owner.commands == [['systemctl', '--user', 'start', owner.SHARED_UNIT]]
    reused = sources.RecordingDeviceSources('jetson', home=manager.home,
        runner=lambda *_a, **_kw: pytest.fail('reused USB queried ownership'))
    owner.current = SimpleNamespace(units={owner.SHARED_UNIT: SimpleNamespace(active=True)},
        healthy=True, available=('camera1', 'camera2'), missing_cameras=('camera3', 'camera4'))
    reused.owner_module = owner
    assert reused.camera(require_archive=False)['state'] == 'partial'
    assert reused.camera_ownership()['units'] == []


@pytest.mark.parametrize('args', [
    ['jetson', '--stop-owned-recording-cameras'],
    ['ipc', '--stop-owned-recording-cameras', '--camera-ownership-json', '{}'],
    ['jetson', '--camera-ownership-json', '{}'],
    ['jetson', '--stop-owned-recording-cameras', '--camera-ownership-json', '{}', '--recording-only'],
])
def test_cli_rejects_missing_or_mismatched_scoped_arguments(args):
    with pytest.raises(SystemExit) as error:
        sources.main(args)
    assert error.value.code == 2


def test_cli_receives_lease_in_argv_without_reading_transport_stdin(monkeypatch, capsys):
    class BrokenInput:
        def read(self, *_args):
            pytest.fail('remote startup transport stdin was consumed')
    monkeypatch.setattr(sources.sys, 'stdin', BrokenInput())
    payload = json.dumps(dict(schemaVersion=1, units=[]))
    assert sources.main(['jetson', '--stop-owned-recording-cameras', '--camera-ownership-json', payload]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['stopOwnedCameras'] and len(report['devices']) == 5
    assert {row['key'] for row in report['devices']} == set(sources.CAMERA_OWNERSHIP_KEYS.values())
    assert all(row['state'] == 'retained' for row in report['devices'])


def test_normal_systemd_default_requirements_do_not_count_as_camera_stop_links(manager):
    gate(manager)
    payload = lease()
    manager.camera_unit_identity = lambda _: identity(payload['units'][0], Requires='app.slice basic.target')
    manager.stop_berxel_preview = lambda: dict(state='stopped')
    assert manager.stop_owned_recording_cameras(payload)['devices'][0]['state'] == 'stopped'
