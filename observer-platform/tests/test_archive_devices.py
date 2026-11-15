"""Device-start orchestration uses isolated helpers, never physical devices."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from collector import archive_devices as devices


def completed(host, rows=None, *, ok=True, returncode=0):
    if rows is None:
        rows = [dict(key='mid360' if host == 'ipc' else 'usb-cameras', state='reused')]
    return subprocess.CompletedProcess([], returncode,
        json.dumps(dict(role=host, recordingOnly=True, ok=ok, devices=rows,
            **(dict(readOnlySources=True, sourceControl=False) if host == 'ipc' else {}))).encode(), b'')


def test_inspects_local_radar_and_starts_cameras_over_pinned_private_ssh_without_algorithms():
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        return completed('ipc' if len(calls) == 1 else 'jetson')
    report = devices.start_capture_devices({'ARCHIVE_SOURCE_HOST': '192.168.1.50'}, runner=runner)
    assert report['ok'] and len(calls) == 2
    local, remote = calls
    assert local[0] == ['/usr/bin/python3', '-B', str(devices.HELPER), 'ipc', '--read-only-sources',
                        '--recording-only', '--status']
    assert local[1]['timeout'] == devices.LIDAR_STATUS_TIMEOUT == 8
    assert remote[1]['timeout'] == 150
    assert remote[0][0] == '/usr/bin/ssh'
    assert 'StrictHostKeyChecking=yes' in remote[0]
    assert 'ClearAllForwardings=yes' in remote[0]
    assert remote[0][-2] == 'nvidia@192.168.1.50'
    assert remote[0][-1].startswith('/usr/bin/python3 -B -c ')
    assert remote[0][-1].endswith(' jetson --recording-only --with-camera-preview')
    assert '--with-camera-yolo' not in remote[0][-1]
    assert '<recording-startup>' in remote[0][-1]
    assert remote[1]['payload'] == devices.HELPER.read_bytes()
    assert 'payload' not in local[1]
    assert all('stop' not in argument and 'restart' not in argument
               and 'run_fastlio' not in argument for call, _ in calls for argument in call)


def test_radar_inspection_is_local_bounded_and_independent_of_camera_link(monkeypatch):
    monkeypatch.setattr(devices, 'ssh_options',
        lambda *_args: pytest.fail('read-only radar inspection consulted the camera link'))
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        return completed('ipc', [dict(key=key, state='inactive')
            for key in ('mid360', 'stl19p', 'bluesea')], ok=False, returncode=1)
    result = devices.inspect_capture_radar({'ARCHIVE_SOURCE_HOST': 'unreachable.invalid'}, runner=runner)
    assert result['ok'] is False and result['readOnlySources'] is True
    assert result['sourceControl'] is False
    assert len(calls) == 1
    command, options = calls[0]
    assert command == ['/usr/bin/python3', '-B', str(devices.HELPER), 'ipc', '--read-only-sources',
                       '--recording-only', '--status']
    assert options['timeout'] == devices.LIDAR_STATUS_TIMEOUT == 8
    assert callable(options['cancelled']) and 'payload' not in options


def test_radar_inspection_rejects_invalid_or_camera_report():
    for result in (completed('jetson'), subprocess.CompletedProcess([], 0, b'not json', b'')):
        with pytest.raises(devices.DeviceStartupError):
            devices.inspect_capture_radar({}, runner=lambda *_args, **_options: result)


def test_radar_inspection_cancellation_does_not_launch_helper():
    with pytest.raises(devices.DeviceStartupCancelled):
        devices.inspect_capture_radar({}, cancelled=lambda: True,
            runner=lambda *_args, **_options: pytest.fail('launched helper after cancellation'))


def test_camera_recognition_is_only_started_by_explicit_request():
    calls = []
    def runner(command, **_options):
        calls.append(command)
        return completed('ipc' if command[0] == '/usr/bin/python3' else 'jetson')
    assert devices.start_capture_devices({'ARCHIVE_START_CAMERA_YOLO': 'true'}, runner=runner)['ok']
    assert calls[-1][-1].endswith('--with-camera-preview --with-camera-yolo')


def test_camera_preview_failure_is_reported_separately_from_available_raw_source(capsys):
    def runner(command, **_options):
        if command[0] == '/usr/bin/python3':
            return completed('ipc')
        return completed('jetson', [dict(key='berxel', state='reused', dataVerified=False),
            dict(key='berxel-preview', state='reused', dataVerified=False),
            dict(key='berxel-frames', state='failed', error='深度预览帧未增长')],
            ok=False, returncode=1)
    report = devices.start_capture_devices({}, runner=runner)
    assert report['hosts']['jetson']['devices'][0]['state'] == 'reused'
    assert report['incompleteDevices'] == [dict(host='jetson', key='berxel-frames', state='failed')]
    output = capsys.readouterr().out
    assert 'Berxel 彩色/深度及网页连续帧检查' in output and '深度预览帧未增长' in output


def test_camera_yolo_failure_preserves_recording_sources_and_reports_service_separately(capsys):
    def runner(command, **_options):
        if command[0] == '/usr/bin/python3':
            return completed('ipc')
        return completed('jetson', [dict(key='berxel', state='reused'),
            dict(key='usb-cameras', state='reused', dataVerified=True),
            dict(key='robot-cloud-yolo-observer', state='failed', error='识别服务启动失败')],
            ok=False, returncode=1)
    report = devices.start_capture_devices({}, runner=runner)
    assert report['hosts']['jetson']['devices'][0]['state'] == 'reused'
    assert report['hosts']['jetson']['devices'][1]['state'] == 'reused'
    assert report['incompleteDevices'] == [dict(host='jetson', key='robot-cloud-yolo-observer', state='failed')]
    output = capsys.readouterr().out
    assert '相机识别服务' in output and '识别服务启动失败' in output


def test_failed_local_device_report_keeps_camera_start_and_available_data(capsys):
    calls = []
    def runner(command, **_options):
        calls.append(command)
        if len(calls) == 1:
            return completed('ipc', [dict(key='mid360', state='failed', error='not connected'),
                                     dict(key='stl19p', state='reused')], ok=False, returncode=1)
        return completed('jetson')
    report = devices.start_capture_devices({}, runner=runner)
    assert not report['ok'] and len(calls) == 2
    assert report['hosts']['jetson']['ok'] is True
    assert report['incompleteDevices'] == [dict(host='ipc', key='mid360', state='failed')]
    assert 'not connected' in capsys.readouterr().out


def test_all_radars_offline_still_prepares_cameras_without_starting_radar_or_claiming_data(capsys):
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        if command[0] == '/usr/bin/python3':
            assert command[-2:] == ['--recording-only', '--status']
            return completed('ipc', [dict(key=key, state='inactive', dataVerified=False)
                for key in ('mid360', 'stl19p', 'bluesea')], ok=False, returncode=1)
        return completed('jetson')
    report = devices.start_capture_devices({}, runner=runner)
    assert len(calls) == 2
    assert report['hosts']['jetson']['ok'] is True
    assert report['ok'] is False and report['hosts']['ipc']['ok'] is False
    assert report['incompleteDevices'] == [dict(host='ipc', key=key, state='inactive')
        for key in ('mid360', 'stl19p', 'bluesea')]
    output = capsys.readouterr().out
    assert '只读检查现有雷达' in output and '不等待雷达开启或首帧' in output
    assert all('systemd-run' not in command and 'run_fastlio.sh' not in command
               for command, _options in calls)


@pytest.mark.parametrize('failure', [devices.DeviceStartupError('read-only radar status timed out'),
    subprocess.TimeoutExpired('radar-status', devices.LIDAR_STATUS_TIMEOUT)])
def test_bounded_radar_status_timeout_does_not_prevent_camera_start(failure):
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        if command[0] == '/usr/bin/python3':
            assert '--status' in command
            assert options['timeout'] <= 8
            raise failure
        return completed('jetson')
    report = devices.start_capture_devices({}, runner=runner)
    assert len(calls) == 2 and calls[1][1]['timeout'] == 150
    assert report['hosts']['jetson']['ok'] is True
    assert report['hosts']['ipc']['ok'] is False
    assert report['incompleteDevices'] == [dict(host='ipc', key='ipc', state='failed')]
    assert str(failure) == report['hosts']['ipc']['error']


@pytest.mark.parametrize('failure', [OSError('private link down'),
    subprocess.CompletedProcess([], 255, b'', b'Permission denied')])
def test_private_camera_link_failure_does_not_erase_successful_local_sources(failure):
    calls = []
    def runner(command, **_options):
        calls.append(command)
        if len(calls) == 1:
            return completed('ipc')
        if isinstance(failure, Exception):
            raise failure
        return failure
    report = devices.start_capture_devices({}, runner=runner)
    assert not report['ok'] and report['hosts']['ipc']['ok']
    assert report['incompleteDevices'] == [dict(host='jetson', key='jetson', state='failed')]


def test_partial_or_skipped_sources_are_visible_and_never_become_complete():
    report = devices.start_capture_devices({}, runner=lambda command, **_options:
        completed('ipc' if command[0] == '/usr/bin/python3' else 'jetson',
                  [dict(key='usb-cameras', state='partial'), dict(key='fisheye', state='skipped')]))
    assert report['ok'] is False
    assert len(report['incompleteDevices']) == 4


@pytest.mark.parametrize('body', [b'{}', b'[]', b'not json',
    b'{"role":"ipc","recordingOnly":false,"devices":[]}',
    json.dumps(dict(role='jetson', recordingOnly=True, devices=[dict(key='x', state='started')])).encode(),
    json.dumps(dict(role='ipc', recordingOnly=True, devices=[dict(key='x', state='unknown')])).encode(),
    json.dumps(dict(role='ipc', recordingOnly=True, devices=[dict(key='x', state='reused')]*2)).encode()])
def test_malformed_or_wrong_mode_report_is_rejected(body):
    with pytest.raises(devices.DeviceStartupError):
        devices._report(subprocess.CompletedProcess([], 0, body, b''), 'ipc')


def test_missing_helper_fails_before_any_start(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, 'HELPER', tmp_path/'absent.py')
    with pytest.raises(devices.DeviceStartupError):
        devices.start_capture_devices({}, runner=lambda *_args, **_options: pytest.fail('started'))
    assert list(tmp_path.iterdir()) == []


def test_cancel_before_first_source_does_not_run_any_command():
    with pytest.raises(devices.DeviceStartupCancelled):
        devices.start_capture_devices({}, cancelled=lambda: True,
            runner=lambda *_args, **_options: pytest.fail('started'))


def test_cancel_after_first_host_does_not_start_the_other():
    calls = []
    def runner(command, **_options):
        calls.append(command)
        return completed('ipc')
    with pytest.raises(devices.DeviceStartupCancelled):
        devices.start_capture_devices({}, cancelled=lambda: bool(calls), runner=runner)
    assert len(calls) == 1


def test_remote_transport_cancel_is_exposed_as_startup_cancel():
    def runner(command, **_options):
        if command[0] == '/usr/bin/python3':
            return completed('ipc')
        raise devices.RemoteStartupCancelled()
    with pytest.raises(devices.DeviceStartupCancelled):
        devices.start_capture_devices({}, runner=runner)


def test_remote_transport_failure_preserves_local_success():
    def runner(command, **_options):
        if command[0] == '/usr/bin/python3':
            return completed('ipc')
        raise devices.RemoteStartupError('heartbeat expired')
    report = devices.start_capture_devices({}, runner=runner)
    assert report['hosts']['ipc']['ok'] and not report['ok']
    assert report['hosts']['jetson']['error'] == 'heartbeat expired'


def test_real_helper_communication_preserves_stdin_payload_and_waits_for_output():
    payload = b'local source helper fixture'
    result = devices.run_startup([sys.executable, '-c',
        'import sys,time; data=sys.stdin.buffer.read(); time.sleep(.3); sys.stdout.buffer.write(data)'],
        payload=payload, timeout=3)
    assert result.returncode == 0 and result.stdout == payload


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_or_cancel_reaps_only_the_launched_helper(cancel):
    processes = []
    def popen(*args, **options):
        process = subprocess.Popen(*args, **options)
        processes.append(process)
        return process
    checks = []
    def cancelled():
        checks.append(1)
        return cancel and len(checks) >= 3
    started = time.monotonic()
    with pytest.raises(devices.DeviceStartupCancelled if cancel else devices.DeviceStartupError):
        devices.run_startup([sys.executable, '-c', 'import time; time.sleep(20)'],
            cancelled=cancelled, timeout=2 if cancel else .08, popen=popen)
    assert time.monotonic() - started < 3
    assert len(processes) == 1 and processes[0].poll() is not None


def test_oversized_helper_output_fails_with_a_bounded_report():
    with pytest.raises(devices.DeviceStartupError, match='过大'):
        devices.run_startup([sys.executable, '-c',
            f'import sys; sys.stdout.buffer.write(b"x"*{devices.MAX_REPORT_BYTES + 1})'], timeout=3)


def closed_camera_report(*, failed=None):
    keys = ('usb-cameras', 'berxel', 'fisheye', 'berxel-preview', 'robot-cloud-yolo-observer')
    rows = [dict(key=key, state='failed' if key == failed else 'stopped') for key in keys]
    return subprocess.CompletedProcess([], 1 if failed else 0,
        json.dumps(dict(role='jetson', recordingOnly=True, stopCameras=True,
                        dryRun=False, ok=failed is None, devices=rows)).encode(), b'')


@pytest.mark.parametrize('explicit_timeout', [None, 75])
def test_camera_stop_allows_long_legal_uvc_drain_and_preserves_explicit_deadline(explicit_timeout):
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        # Model a 60 s successful remote drain without starting a process,
        # sleeping or touching cameras. The former 45 s default rejected it.
        if options['timeout'] < 60:
            raise devices.RemoteStartupError('相机关闭检查超时')
        return closed_camera_report()
    options = {} if explicit_timeout is None else dict(timeout=explicit_timeout)
    result = devices.stop_capture_cameras({'ARCHIVE_SOURCE_HOST': '192.168.1.50'},
                                         runner=runner, **options)
    assert result['ok'] and len(calls) == 1
    command, arguments = calls[0]
    assert arguments['timeout'] == (180 if explicit_timeout is None else explicit_timeout)
    assert devices.CAMERA_SHUTDOWN_TIMEOUT > 105
    assert command[0] == '/usr/bin/ssh' and command[-2] == 'nvidia@192.168.1.50'
    assert 'StrictHostKeyChecking=yes' in command and 'ClearAllForwardings=yes' in command
    assert command[-1].endswith(' jetson --stop-recording-cameras')
    assert arguments['payload'] == devices.HELPER.read_bytes()


def test_camera_stop_explicit_short_deadline_reports_uncertainty_without_retry_or_false_success():
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        assert options['timeout'] == 30
        raise devices.RemoteStartupError('相机关闭检查超时')
    with pytest.raises(devices.DeviceStartupError, match='相机关闭未获确认.*超时'):
        devices.stop_capture_cameras({}, timeout=30, runner=runner)
    assert len(calls) == 1


def test_camera_stop_longer_budget_still_requires_every_camera_confirmation(capsys):
    result = devices.stop_capture_cameras({}, runner=lambda *_args, **_options:
                                         closed_camera_report(failed='usb-cameras'))
    assert result['ok'] is False
    assert next(row for row in result['devices'] if row['key'] == 'usb-cameras')['state'] == 'failed'
    assert '尚未确认关闭：四路 USB 相机' in capsys.readouterr().out
