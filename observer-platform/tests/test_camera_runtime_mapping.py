"""Camera migration checks use private fake sysfs, HTTP responses and units."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.scripts import camera_owner as owner
from deploy.scripts import camera_status_gateway as gate


@pytest.fixture(autouse=True)
def no_real_control(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Real camera or service access forbidden')
    monkeypatch.setattr(owner.subprocess, 'run', forbidden)
    monkeypatch.setattr(owner.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(owner.time, 'sleep', lambda _: None)


def profile():
    ports = {'camera1': '2.3', 'camera2': '2.4', 'camera3': '2.2.1', 'camera4': '2.2.4.1'}
    return {'cameras': [
        {'id': name, 'label': direction, 'direction': direction, 'enabled': True,
         'device': '/dev/v4l/by-path/platform-3610000.usb-usb-0:' + ports[name] + ':1.0-video-index0'}
        for name, (_, direction, _) in owner.CAMERA_IDENTITIES.items()]}


def write_profile(tmp_path, value=None):
    path = tmp_path / 'cameras.json'
    path.write_text(json.dumps(profile() if value is None else value))
    path.chmod(0o600)
    return path


def record(argv):
    return '{ path=' + argv[0] + ' ; argv[]=' + ' '.join(argv) + ' ; ignore_errors=no ; }'


def test_shared_service_requires_exact_program_config_and_web_arguments():
    assert owner.trusted_exec_start(owner.SHARED_UNIT, record(owner.SHARED_ARGS))
    for index, value in [(0, '/bin/bash'), (2, '/tmp/camera_native_web.py'),
                         (4, '/tmp/cameras.json'), (7, '1920'), (9, '30'), (11, '2000000')]:
        argv = list(owner.SHARED_ARGS)
        argv[index] = value
        assert not owner.trusted_exec_start(owner.SHARED_UNIT, record(argv))
    assert not owner.trusted_exec_start(owner.SHARED_UNIT, record(owner.SHARED_ARGS + ['--host', '0.0.0.0']))
    assert not owner.trusted_exec_start('fake-camera.service', record(owner.SHARED_ARGS))


def test_default_uses_fixed_profile_and_invalid_existing_profile_never_falls_back(tmp_path, monkeypatch):
    path = write_profile(tmp_path)
    monkeypatch.setattr(owner, 'SHARED_CONFIG', str(path))
    assert owner.Settings().capture_config == str(path)
    assert owner.camera_ports(owner.Settings()) == {
        'usbFront': '2.4', 'usbLeft': '2.3', 'usbHub2': '2.2.4.1', 'usbHub3': '2.2.1'}
    path.write_text('broken')
    with pytest.raises(ValueError):
        owner.camera_ports(owner.Settings())
    path.unlink()
    assert owner.Settings().capture_config is None


@pytest.mark.parametrize('kind', ['duplicate', 'direction', 'disabled', 'noncanonical', 'missing', 'unknown'])
def test_invalid_mapping_is_rejected_before_any_device_lookup(tmp_path, kind):
    data = profile()
    if kind == 'duplicate':
        data['cameras'][3]['device'] = data['cameras'][2]['device']
    elif kind == 'direction':
        data['cameras'][2]['direction'] = 'left'
    elif kind == 'disabled':
        data['cameras'][2]['enabled'] = False
    elif kind == 'noncanonical':
        data['cameras'][2]['device'] = '/dev/video4'
    elif kind == 'missing':
        data['cameras'].pop()
    else:
        data['cameras'][2]['id'] = 'camera9'
    with pytest.raises(owner.OwnerError):
        owner.camera_ports(owner.Settings(capture_config=str(write_profile(tmp_path, data))))


def test_untrusted_permissions_and_symlink_mapping_fail_closed(tmp_path):
    path = write_profile(tmp_path)
    path.chmod(0o666)
    with pytest.raises(owner.OwnerError):
        owner.camera_ports(owner.Settings(capture_config=str(path)))
    path.chmod(0o600)
    link = tmp_path / 'link.json'
    link.symlink_to(path)
    with pytest.raises(OSError):
        owner.camera_ports(owner.Settings(capture_config=str(link)))


def fake_hardware(tmp_path, config):
    by_path, sysfs, devices = (tmp_path / name for name in ('by-path', 'sys', 'devices'))
    for path in (by_path, sysfs, devices):
        path.mkdir()
    settings = owner.Settings(capture_config=str(config), dev_by_path_root=str(by_path), sys_video_root=str(sysfs))
    parents = {}
    for index, (key, port) in enumerate(owner.camera_ports(settings).items()):
        physical = tmp_path / ('1-' + port)
        interface = physical / 'interface0'
        interface.mkdir(parents=True)
        (physical / 'idVendor').write_text('0bda')
        (physical / 'idProduct').write_text('5187' if key in ('usbFront', 'usbLeft') else 'd584')
        parents[key] = physical
        for number in (index * 2, index * 2 + 1):
            name = 'video' + str(number)
            (devices / name).write_bytes(b'NEVER_OPEN')
            entry = sysfs / name
            entry.mkdir()
            (entry / 'device').symlink_to(interface, target_is_directory=True)
            (entry / 'dev').write_text('81:' + str(number))
        (by_path / ('platform-3610000.usb-usb-0:' + port + ':1.0-video-index0')).symlink_to(devices / ('video' + str(index * 2)))
    return settings, parents


def test_owner_and_gateway_agree_on_audited_ports_and_reject_depth_substitution(tmp_path):
    settings, parents = fake_hardware(tmp_path, write_profile(tmp_path))
    nodes, missing = owner.SystemOps(settings).camera_nodes()
    mapping, connected = gate._node_mapping(settings)
    assert not missing and len(nodes) == len(mapping) == 8
    assert all(connected.values())
    assert mapping['/dev/video4'] == 'usbHub2' and mapping['/dev/video6'] == 'usbHub3'
    (parents['usbHub2'] / 'idVendor').write_text('19b4')
    (parents['usbHub2'] / 'idProduct').write_text('0001')
    with pytest.raises(owner.CameraIdentityError):
        owner.SystemOps(settings).camera_nodes()
    mapping, connected = gate._node_mapping(settings)
    assert connected['usbHub2'] is None
    assert '/dev/video4' not in mapping
    cache = gate.UsageCache(ops=SimpleNamespace(settings=settings, uid=1000,
        snapshot=lambda: owner.Snapshot(scan_errors=('identity mismatch',))))
    assert cache.probe_once()
    assert cache.value()['usbHub2']['connected'] is None


def status(frames=20, encoded=10):
    return {'cameras': [
        {'id': name, 'direction': direction, 'width': 640,
         'height': 480 if name in ('camera1', 'camera2') else 360,
         'capture_width': 2592 if name in ('camera1', 'camera2') else 1920,
         'capture_height': 1944 if name in ('camera1', 'camera2') else 1080,
         'requested_fps': 10, 'frames': frames, 'encoded_frames': encoded,
         'frame_age_ms': 20, 'codec': 'avc1.640028'}
        for name, (_, direction, _) in owner.CAMERA_IDENTITIES.items()]}


def fake_status(monkeypatch, values):
    responses = iter(values)
    monkeypatch.setattr(owner.urllib.request, 'build_opener',
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs:
            io.BytesIO(json.dumps(next(responses)).encode())))


def test_health_requires_all_capture_and_encoded_frames_to_advance(monkeypatch):
    fake_status(monkeypatch, [status(), status(22, 12), status(23, 12)])
    ops = owner.SystemOps(owner.Settings(capture_config=None))
    assert ops.healthy()
    assert not ops.healthy(), 'A frozen encoder is not a healthy stream'


@pytest.mark.parametrize('failure', ['stale', 'wrong_size', 'wrong_direction', 'missing', 'one_frozen'])
def test_invalid_or_partial_sources_never_count_as_healthy(monkeypatch, failure):
    second = status(22, 12)
    if failure == 'stale':
        second['cameras'][0]['frame_age_ms'] = 3000
    elif failure == 'wrong_size':
        second['cameras'][0]['width'] = 800
    elif failure == 'wrong_direction':
        second['cameras'][0]['direction'] = 'left'
    elif failure == 'missing':
        second['cameras'].pop()
    else:
        second['cameras'][0]['frames'] = 20
    fake_status(monkeypatch, [status(), second])
    assert not owner.SystemOps(owner.Settings(capture_config=None)).healthy()


def partial_status(frames=20, encoded=10):
    value = status(frames, encoded)
    value['cameras'][2] = dict(id='camera3', direction='right', connected=False,
                               state='disconnected', error='未连接', frames=0,
                               encoded_frames=0, frame_age_ms=None, codec=None)
    return value


def test_partial_health_requires_all_physically_present_cameras_and_explicit_missing_status(monkeypatch):
    fake_status(monkeypatch, [partial_status(), partial_status(22, 12)])
    ops = owner.SystemOps(owner.Settings(capture_config=None))
    assert ops.healthy(('camera1', 'camera2', 'camera4'))
    assert set(ops._health_frames) == {'camera1', 'camera2', 'camera4'}


@pytest.mark.parametrize('failure', ['frozen', 'stale', 'bad_encoder', 'missing_row', 'hidden_live', 'wrong_direction'])
def test_partial_health_never_ignores_present_camera_failure_or_hides_missing_status(monkeypatch, failure):
    second = partial_status(22, 12)
    if failure == 'frozen':
        second['cameras'][3]['frames'] = 20
    elif failure == 'stale':
        second['cameras'][0]['frame_age_ms'] = 3000
    elif failure == 'bad_encoder':
        second['cameras'][1]['video_error'] = 'encoder failed'
    elif failure == 'missing_row':
        second['cameras'].pop(2)
    elif failure == 'hidden_live':
        second['cameras'][2]['connected'] = True
    else:
        second['cameras'][2]['direction'] = 'left'
    fake_status(monkeypatch, [partial_status(), second])
    assert not owner.SystemOps(owner.Settings(capture_config=None)).healthy(('camera1', 'camera2', 'camera4'))


def test_reconnected_camera_with_old_placeholder_is_not_reported_as_working(monkeypatch):
    fake_status(monkeypatch, [partial_status()])
    ops = owner.SystemOps(owner.Settings(capture_config=None))
    assert not ops.healthy(tuple(owner.CAMERA_IDENTITIES))
    assert ops._reconnect_required == ('camera3',)


def test_no_present_cameras_cannot_be_healthy(monkeypatch):
    fake_status(monkeypatch, [partial_status()])
    assert not owner.SystemOps(owner.Settings(capture_config=None)).healthy(())


@pytest.mark.parametrize('field,value', [('frames', 1), ('frames', False), ('encoded_frames', 1),
                                        ('encoded_frames', False), ('frame_age_ms', 20), ('codec', 'avc1.640028')])
def test_missing_camera_cannot_report_buffered_frames_as_a_valid_disconnected_placeholder(monkeypatch, field, value):
    report = partial_status()
    report['cameras'][2][field] = value
    fake_status(monkeypatch, [report])
    assert not owner.SystemOps(owner.Settings(capture_config=None)).healthy(('camera1', 'camera2', 'camera4'))


def test_actual_absence_maps_to_correct_identity_without_reading_device(tmp_path, monkeypatch):
    settings, _ = fake_hardware(tmp_path, write_profile(tmp_path))
    anchor = Path(settings.dev_by_path_root) / 'platform-3610000.usb-usb-0:2.2.1:1.0-video-index0'
    anchor.unlink()
    ops = owner.SystemOps(settings)
    monkeypatch.setattr(ops, 'unit', lambda name: (owner.Unit(name), {}))
    monkeypatch.setattr(ops, 'holders', lambda *args: ())
    fake_status(monkeypatch, [partial_status(), partial_status(22, 12)])
    current = ops.snapshot()
    assert current.available == ('camera1', 'camera2', 'camera4')
    assert current.missing == ('2.2.1',) and current.missing_cameras == ('camera3',)
    assert current.healthy and not current.scan_errors


def test_present_camera_missing_identity_metadata_is_inspection_error_not_absence(tmp_path, monkeypatch):
    settings, parents = fake_hardware(tmp_path, write_profile(tmp_path))
    (parents['usbHub3'] / 'idProduct').unlink()
    ops = owner.SystemOps(settings)
    monkeypatch.setattr(ops, 'unit', lambda name: (owner.Unit(name), {}))
    current = ops.snapshot()
    assert current.scan_errors and not current.available and not current.healthy
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).preflight(current, True)


def test_broken_configured_symlink_is_unknown_identity_and_never_optional_absence(tmp_path, monkeypatch):
    settings, _ = fake_hardware(tmp_path, write_profile(tmp_path))
    anchor = Path(settings.dev_by_path_root) / 'platform-3610000.usb-usb-0:2.2.1:1.0-video-index0'
    anchor.resolve().unlink()
    with pytest.raises(owner.CameraIdentityError):
        owner.SystemOps(settings).camera_nodes()
    _, connected = gate._node_mapping(settings)
    assert connected['usbHub3'] is None
    ops = owner.SystemOps(settings)
    monkeypatch.setattr(ops, 'unit', lambda name: (owner.Unit(name), {}))
    current = ops.snapshot()
    assert current.scan_errors and not current.missing_cameras and not current.available


def test_same_model_at_wrong_usb_port_is_rejected_instead_of_remapped(tmp_path):
    settings, _ = fake_hardware(tmp_path, write_profile(tmp_path))
    anchor = Path(settings.dev_by_path_root) / 'platform-3610000.usb-usb-0:2.2.1:1.0-video-index0'
    other = Path(settings.dev_by_path_root) / 'platform-3610000.usb-usb-0:2.2.4.1:1.0-video-index0'
    anchor.unlink()
    anchor.symlink_to(other.resolve())
    with pytest.raises(owner.CameraIdentityError):
        owner.SystemOps(settings).camera_nodes()


def test_configured_start_uses_verified_shared_unit_and_exact_invocation_cleanup(tmp_path, monkeypatch):
    path = write_profile(tmp_path)
    monkeypatch.setattr(owner, 'SHARED_CONFIG', str(path))
    ops = owner.SystemOps(owner.Settings(capture_config=str(path)))
    commands, stopped = [], []
    values = {'ExecStart': record(owner.SHARED_ARGS), 'InvocationID': 'a' * 32}
    units = iter([(owner.Unit(owner.SHARED_UNIT), values),
                  (owner.Unit(owner.SHARED_UNIT), values),
                  (owner.Unit(owner.SHARED_UNIT, active=True), values)])
    monkeypatch.setattr(ops, 'unit', lambda name: next(units))
    monkeypatch.setattr(ops, 'command', lambda args: commands.append(args) or SimpleNamespace(returncode=0))
    receipt = ops.start_web()
    assert receipt == owner.StartReceipt(owner.SHARED_UNIT, 'a' * 32)
    assert commands == [['systemctl', '--user', 'start', owner.SHARED_UNIT]]
    monkeypatch.setattr(ops, 'unit', lambda name: (owner.Unit(owner.SHARED_UNIT, active=True), values))
    monkeypatch.setattr(ops, 'stop_unit', stopped.append)
    ops.stop_new(receipt)
    assert stopped == [owner.SHARED_UNIT]
    values['InvocationID'] = 'b' * 32
    with pytest.raises(owner.OwnerError):
        ops.stop_new(receipt)
    assert stopped == [owner.SHARED_UNIT]


def test_missing_or_untrusted_shared_unit_is_never_started(tmp_path, monkeypatch):
    path = write_profile(tmp_path)
    monkeypatch.setattr(owner, 'SHARED_CONFIG', str(path))
    ops = owner.SystemOps(owner.Settings(capture_config=str(path)))
    monkeypatch.setattr(ops, 'unit', lambda name: (owner.Unit(owner.SHARED_UNIT), {'ExecStart': '/tmp/evil.py'}))
    with pytest.raises(owner.OwnerError):
        ops.start_web()
