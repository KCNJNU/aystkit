"""Existing intrinsics are metadata, never camera or ROS control."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from collector import camera_calibration as calibration


@pytest.fixture
def registry():
    camera = dict(imageWidth=1920, imageHeight=1080, distortionModel='plumb_bob',
        K=[700, 0, 960, 0, 701, 540, 0, 0, 1], D=[.01, -.02, .003, .004, .005],
        R=[1, 0, 0, 0, 1, 0, 0, 0, 1], P=[680, 0, 955, 0, 0, 681, 535, 0, 0, 0, 1, 0],
        sourceYamlSha256='b'*64)
    cameras = {}
    for index, (camera_id, direction) in enumerate(calibration.DIRECTIONS.items(), 1):
        cameras[camera_id] = dict(copy.deepcopy(camera), id=camera_id, direction=direction,
            device=f'/dev/v4l/by-path/platform-3610000.xhci-usb-0:2.{index}:1.0-video-index0')
    return dict(schemaVersion=1, robotId='robot-1', sourceResultId='result_20261109_123538_33f712',
        sourceArchiveSha256='a'*64, quality='candidate_no_independent_validation', cameras=cameras)


def write_registry(tmp_path, registry, mode=0o600):
    path = tmp_path/'calibration.json'
    path.write_text(json.dumps(registry), encoding='utf-8')
    path.chmod(mode)
    return path


def test_optional_missing_does_not_require_any_camera_or_ros(tmp_path, monkeypatch):
    missing = tmp_path/'absent.json'
    monkeypatch.setattr(calibration, 'DEFAULT_PATH', missing)
    assert calibration.load_registry() is None
    assert calibration.get_camera_calibration('camera1', 1920, 1080) is None


def test_matched_snapshot_preserves_native_intrinsics_and_provenance(tmp_path, registry):
    path = write_registry(tmp_path, registry)
    assert calibration.load_registry(path, robot_id='robot-1') == registry
    result = calibration.get_camera_calibration('camera1', 1920, 1080, path=path, robot_id='robot-1')
    assert result['state'] == 'matched'
    assert result['nativeImageInfo'] == result['currentImageInfo']
    assert result['nativeImageInfo']['K'] == registry['cameras']['camera1']['K']
    assert result['nativeSize'] == result['sourceSize'] == result['size'] == dict(width=1920, height=1080)
    assert result['direction'] == 'front' and result['robotId'] == 'robot-1'
    assert result['sourceArchiveSha256'] == 'a'*64 and result['sourceYamlSha256'] == 'b'*64
    assert result['quality'] == 'candidate_no_independent_validation'
    assert result['transform'] == dict(kind='identity')
    assert len(json.dumps(result, allow_nan=False)) < 4096
    result['nativeImageInfo']['K'][0] = 999
    assert result['currentImageInfo']['K'][0] == 700
    assert registry['cameras']['camera1']['K'][0] == 700


def test_mismatch_never_presents_native_matrix_as_current(registry):
    result = calibration.get_camera_calibration('camera1', 1280, 720, registry=registry)
    assert result['state'] == 'resolution_mismatch'
    assert 'currentImageInfo' not in result and 'K' not in result
    assert result['nativeImageInfo']['width'] == 1920
    assert result['size'] == dict(width=1280, height=720)
    assert result['transform'] == dict(kind='none', reason='resolution_mismatch')


def test_explicit_pure_resize_scales_both_rows_and_keeps_native_D_R(registry):
    result = calibration.get_camera_calibration('camera1', 960, 810, registry=registry, resize=True)
    native, current = result['nativeImageInfo'], result['currentImageInfo']
    assert result['state'] == 'resized'
    assert current['K'] == [350, 0, 480, 0, 525.75, 405, 0, 0, 1]
    assert current['P'] == [340, 0, 477.5, 0, 0, 510.75, 401.25, 0, 0, 0, 1, 0]
    assert current['D'] == native['D'] and current['R'] == native['R']
    assert native['K'] == registry['cameras']['camera1']['K']
    assert result['transform'] == dict(kind='resize', scaleX=.5, scaleY=.75, crop=False)


def test_other_unconfigured_camera_has_no_calibration(registry):
    assert calibration.get_camera_calibration('berxel', 640, 480, registry=registry) is None


@pytest.mark.parametrize('field,value', [('imageWidth', 0), ('imageHeight', True),
    ('distortionModel', 'equidistant'), ('direction', 'rear'), ('id', 'camera4'),
    ('device', '/dev/video0'), ('sourceYamlSha256', 'not a hash'),
    ('K', [1]*8), ('D', [0]*4), ('R', [0]*9), ('P', [1]*11),
    ('K', [0, 0, 960, 0, 701, 540, 0, 0, 1]),
    ('K', [700, 0, 960, 0, -1, 540, 0, 0, 1]),
    ('K', [700, 0, 960, 0, 701, 540, 0, 1, 1]),
    ('D', [float('nan'), 0, 0, 0, 0]), ('D', [float('inf'), 0, 0, 0, 0]),
    ('D', [10**1000, 0, 0, 0, 0]), ('D', [True, 0, 0, 0, 0])])
def test_rejects_invalid_camera_values(registry, field, value):
    registry['cameras']['camera1'][field] = value
    with pytest.raises(calibration.CalibrationError):
        calibration.get_camera_calibration('camera1', 1920, 1080, registry=registry)


@pytest.mark.parametrize('field,value', [('schemaVersion', True), ('schemaVersion', 2),
    ('robotId', '../other'), ('quality', 'verified'), ('sourceArchiveSha256', 'short'),
    ('sourceResultId', 'a\ncommand')])
def test_rejects_invalid_registry_values(registry, field, value):
    registry[field] = value
    with pytest.raises(calibration.CalibrationError):
        calibration.get_camera_calibration('camera1', 1920, 1080, registry=registry)


def test_rejects_robot_mismatch_for_file_and_provided_registry(tmp_path, registry):
    path = write_registry(tmp_path, registry)
    for kwargs in (dict(path=path), dict(registry=registry)):
        with pytest.raises(calibration.CalibrationError, match='robotId'):
            calibration.get_camera_calibration('camera1', 1920, 1080, robot_id='robot-2', **kwargs)


def test_rejects_duplicate_camera_devices_or_missing_camera(registry):
    registry['cameras']['camera2']['device'] = registry['cameras']['camera1']['device']
    with pytest.raises(calibration.CalibrationError, match='distinct'):
        calibration.get_camera_calibration('camera1', 1920, 1080, registry=registry)
    registry['cameras'].pop('camera2')
    with pytest.raises(calibration.CalibrationError, match='four USB'):
        calibration.get_camera_calibration('camera1', 1920, 1080, registry=registry)


@pytest.mark.parametrize('mode', [0o644, 0o660, 0o606])
def test_rejects_public_or_group_access(tmp_path, registry, mode):
    path = write_registry(tmp_path, registry, mode)
    with pytest.raises(calibration.CalibrationError, match='private'):
        calibration.load_registry(path)


def test_rejects_foreign_owner(tmp_path, registry, monkeypatch):
    path = write_registry(tmp_path, registry)
    owner = os.stat(path).st_uid
    monkeypatch.setattr(calibration.os, 'getuid', lambda: owner+1)
    with pytest.raises(calibration.CalibrationError, match='owned'):
        calibration.load_registry(path)


def test_rejects_symlink_directory_and_fifo_without_blocking(tmp_path, registry):
    path = write_registry(tmp_path, registry)
    link, fifo = tmp_path/'symlink.json', tmp_path/'fifo.json'
    link.symlink_to(path)
    os.mkfifo(fifo, 0o600)
    for candidate in (link, tmp_path, fifo):
        with pytest.raises(calibration.CalibrationError):
            calibration.load_registry(candidate)


@pytest.mark.parametrize('content', [b'[]', b'{', b'{"schemaVersion":1,"schemaVersion":1}',
    b'{"x":NaN}', b'\xff', b' '*65537])
def test_rejects_malformed_or_oversized_json(tmp_path, content):
    path = tmp_path/'bad.json'
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(calibration.CalibrationError):
        calibration.load_registry(path)


@pytest.mark.parametrize('width,height,resize', [(0, 1080, False), (1920, None, False),
    (1920., 1080, False), (True, 1080, False), (1920, 1080, 'yes'), (2**32, 1080, False)])
def test_rejects_ambiguous_dimensions_and_resize(registry, width, height, resize):
    with pytest.raises(calibration.CalibrationError):
        calibration.get_camera_calibration('camera1', width, height, registry=registry, resize=resize)


def test_cli_is_read_only_and_does_not_import_ROS_or_open_device(tmp_path, registry):
    path = write_registry(tmp_path, registry)
    before = path.read_bytes()
    package = Path(calibration.__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, '-B', '-m', 'collector.camera_calibration',
        '--camera', 'camera1', '--config', str(path), '--width', '960', '--height', '540', '--resize'],
        cwd=package, capture_output=True, text=True, timeout=5, check=True)
    value = json.loads(result.stdout)
    assert value['state'] == 'resized' and value['currentImageInfo']['K'][0] == 350
    assert not result.stderr and path.read_bytes() == before
    # The utility's dependencies are standard-library only, so it also runs outside a ROS environment.
    assert 'rclpy' not in calibration.__dict__ and 'cv2' not in calibration.__dict__


def test_cli_error_and_missing_optional_registry(tmp_path, registry, capsys):
    missing = tmp_path/'not-there.json'
    assert calibration.main(['--camera', 'camera1', '--config', str(missing)]) == 0
    assert json.loads(capsys.readouterr().out) is None
    path = write_registry(tmp_path, registry)
    assert calibration.main(['--camera', 'camera1', '--config', str(path), '--robot-id', 'robot-2']) == 2
    assert json.loads(capsys.readouterr().err)['state'] == 'invalid'
    with pytest.raises(SystemExit):
        calibration.main(['--camera', 'camera1', '--config', str(path), '--width', '960'])
