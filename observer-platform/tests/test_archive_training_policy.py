"""Storage selection preserves trainable media without transferring raw caches."""
from copy import deepcopy
import json
import subprocess

import pytest

from collector import archive_devices as devices
from collector.archive_policy import (allowed_archive_manifest, allowed_manifest_path,
    parse_ros_sensors, training_manifest_allowed)
from deploy.scripts import device_sources


def bag(sensor='雷达MID360'):
    return dict(sensor=sensor, metadata=dict(bagCompressionReady=True),
                files=['compressed-bag/metadata.yaml', 'compressed-bag/capture_0.db3.zst'])


@pytest.mark.parametrize('sensor', ['雷达MID360', '二维雷达scan', '二维雷达蓝海', '定位与TF'])
def test_lidar_and_pose_require_lossless_compressed_bag_outputs(sensor):
    value = bag(sensor)
    assert training_manifest_allowed(value)
    for raw in ('bag/capture_0.db3', 'frames.mjpg', 'index.jsonl'):
        invalid = deepcopy(value); invalid['files'].append(raw)
        assert not training_manifest_allowed(invalid)


def test_video_conversion_remains_selected_before_final_output_is_ready(tmp_path, monkeypatch):
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '1')
    value = dict(sensor='Berxel深度', metadata=dict(videoRequired=True), files=['bag/raw.db3'])
    path = tmp_path/'video-pending.json'; path.write_text(json.dumps(value))
    assert allowed_archive_manifest(value) and allowed_manifest_path(path)
    assert not training_manifest_allowed(value)
    value.update(files=['video.mkv']); value['metadata']['videoReady'] = True
    assert training_manifest_allowed(value)


def test_camera_calibration_only_bag_is_retained_but_not_image_or_cloud_bags():
    value = bag('Berxel深度')
    value['metadata'].update(cameraMetadataOnly=True,
        topics={'/info': dict(type='sensor_msgs/msg/CameraInfo')})
    assert training_manifest_allowed(value)
    value['metadata']['topics']['/image'] = dict(type='sensor_msgs/msg/Image')
    assert not training_manifest_allowed(value)
    assert not training_manifest_allowed(bag('Berxel点云'))


@pytest.mark.parametrize('files', [None, [], [dict(path='x')], ['compressed-bag/metadata.yaml']*2])
def test_malformed_output_lists_fail_closed(files):
    value = bag(); value['files'] = files
    assert not training_manifest_allowed(value)


def test_recording_policy_does_not_subscribe_redundant_camera_cloud_or_logs(monkeypatch):
    monkeypatch.setenv('ARCHIVE_TRAINING_ONLY', '1')
    selected = parse_ros_sensors('雷达MID360,定位与TF,Berxel彩色,Berxel深度,Berxel点云,诊断日志,覆盖任务')
    assert selected == ('雷达MID360', '定位与TF', 'Berxel彩色', 'Berxel深度')


def camera_report(state='already_stopped'):
    keys = ('usb-cameras', 'berxel', 'fisheye', 'berxel-preview', 'robot-cloud-yolo-observer')
    return dict(ok=True, role='jetson', stopCameras=True, dryRun=False,
                devices=[dict(key=key, state=state) for key in keys])


def test_camera_shutdown_uses_pinned_private_ssh_and_requires_all_camera_results(monkeypatch):
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess([], 0, json.dumps(camera_report()).encode(), b'')
    assert devices.stop_capture_cameras({}, runner=runner)['ok']
    assert len(calls) == 1
    command, options = calls[0]
    assert command[-1].endswith(' jetson --stop-recording-cameras')
    assert 'StrictHostKeyChecking=yes' in command
    assert options['payload'] == devices.HELPER.read_bytes()
    invalid = camera_report(); invalid['devices'].pop()
    with pytest.raises(devices.DeviceStartupError):
        devices.stop_capture_cameras({}, runner=lambda *_a, **_kw:
            subprocess.CompletedProcess([], 0, json.dumps(invalid).encode(), b''))


def test_shutdown_still_runs_when_terminal_is_closed(monkeypatch):
    monkeypatch.setattr('builtins.print', lambda *_a, **_kw: (_ for _ in ()).throw(BrokenPipeError()))
    assert devices.stop_capture_cameras({}, runner=lambda *_a, **_kw:
        subprocess.CompletedProcess([], 0, json.dumps(camera_report()).encode(), b''))['ok']


def test_camera_stop_never_dispatches_radar_or_archive_operations(tmp_path):
    manager = device_sources.RecordingDeviceSources('jetson', home=tmp_path)
    calls = []
    def closed(key):
        calls.append(key); return dict(key=key, state='already_stopped')
    manager.stop_camera = lambda: closed('usb-cameras')
    manager.stop_berxel_preview = lambda: closed('berxel-preview')
    manager.stop_source = lambda source: closed(source.key)
    manager.stop_support = lambda unit: closed(unit.removesuffix('.service'))
    assert manager.stop_recording_cameras()['ok']
    assert set(calls) == {row['key'] for row in camera_report()['devices']}
