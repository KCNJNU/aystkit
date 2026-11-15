"""Minimal recording startup, with all real commands and devices forbidden."""
import json

import pytest

from deploy.scripts import device_sources as sources
from tests.test_device_sources import FakeOwner, completed, process, fake_environment


def lidar_process(manager,pid=12,*,parameters=None,environment=None,remaps=(),driver='livox_ros_driver2_node'):
    config=manager.home/'lidar-config.json'
    config.write_text(json.dumps(dict(lidar_configs=[dict(ip='192.168.1.135')],
        Mid360s=dict(host_net_info=[dict(host_ip='192.168.1.100',point_data_port=56301)]))))
    values=dict(xfer_format=1,multi_topic=0,user_config_path=str(config)) if parameters is None else parameters
    params=manager.home/f'params-{pid}.yaml'
    params.write_text(json.dumps({'/**':{'ros__parameters':values}}))
    args=['/ws/'+driver,'--ros-args','--params-file',str(params)]
    for source,target in remaps:
        args.extend(['-r',source+':='+target])
    process(manager.proc_root,pid,args)
    root=manager.proc_root/str(pid)
    (root/'stat').write_text(str(pid)+' (fixture) '+' '.join(['S']+['0']*18+[str(1000+pid)]))
    env=environment or dict(ROS_DOMAIN_ID='0',ROS_LOCALHOST_ONLY='0',RMW_IMPLEMENTATION='rmw_fastrtps_cpp')
    (root/'environ').write_bytes(b'\0'.join((key+'='+value).encode() for key,value in env.items())+b'\0')
    return root,params,config


@pytest.fixture(autouse=True)
def forbid_real_operations(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('recording startup test attempted a real system operation')
    monkeypatch.setattr(sources.subprocess, 'run', forbidden)
    monkeypatch.setattr(sources.os, 'kill', forbidden)


@pytest.fixture
def manager(tmp_path):
    home, proc = tmp_path / 'home', tmp_path / 'proc'
    home.mkdir()
    proc.mkdir()
    return sources.RecordingDeviceSources('ipc', home=home, proc_root=proc,
        sleeper=lambda _: None, runner=lambda *_args, **_kwargs:
        completed('LoadState=not-found\nActiveState=inactive\n'))


@pytest.mark.parametrize('role', ['ipc', 'jetson'])
def test_recording_dry_run_is_inert_and_has_no_algorithms_or_support_chains(tmp_path, role):
    def forbidden(*_args, **_kwargs):
        pytest.fail('dry run touched hardware or invoked a command')
    manager = sources.RecordingDeviceSources(role, home=tmp_path / 'absent', runner=forbidden)
    manager.load_camera_owner = forbidden
    report = manager.recording_only(dry_run=True)
    assert report['ok'] and report['recordingOnly'] and report['dryRun']
    commands = json.dumps(report)
    assert not (tmp_path / 'absent').exists()
    assert 'run_fastlio.sh' not in commands
    assert 'yolo' not in commands and 'berxel-preview' not in commands
    assert 'relay' not in commands and 'gateway' not in commands
    assert 'restore_jetson_archive_policy' not in commands
    if role == 'ipc':
        assert 'msg_MID360s_launch.py' in commands
        assert {row['key'] for row in report['devices']} == {'mid360', 'stl19p', 'bluesea'}


def test_running_mid360_without_fastlio_is_reused_without_any_commands(manager):
    lidar_process(manager)
    manager.runner = lambda *_args, **_kwargs: pytest.fail('existing driver was not reused')
    report = manager.recording_source(sources.RECORDING_MID360)
    assert report['state'] == 'reused' and report['pids'] == [12]
    assert report['dataVerified'] is False
    assert report['xferFormat']==1 and report['actualTopics']==['/livox/lidar','/livox/imu']
    assert report['dds']['domain']=='0' and report['processStartTicks']=='1012'
    assert sources.exact_processes(manager.proc_root, 'fastlio_mapping') == []


def test_missing_mid360_starts_driver_only_and_does_not_require_mapping_process(manager):
    calls, active = [], False
    def runner(command, **kwargs):
        nonlocal active
        calls.append(command)
        if command[0] == 'systemd-run':
            active = True
            lidar_process(manager)
            return completed()
        return completed('LoadState=loaded\nActiveState=' + ('active' if active else 'inactive') + '\n')
    manager.runner = runner
    manager.preflight = lambda _: None
    report = manager.recording_source(sources.RECORDING_MID360)
    assert report['state'] == 'started'
    launch = next(command for command in calls if command[0] == 'systemd-run')
    assert launch[-4:] == ['ros2', 'launch', 'livox_ros_driver2', 'msg_MID360s_launch.py']
    assert '--unit=robot-device-mid360.service' in launch
    assert not any('stop' in command or 'restart' in command or 'fastlio' in str(command) for command in calls)


def test_mid360_preflight_needs_only_driver_package_and_no_algorithm_script(manager, tmp_path):
    fake_environment(manager, tmp_path)
    prefix = tmp_path / 'livox'
    share = prefix / 'share/livox_ros_driver2'
    (share / 'launch_ROS2').mkdir(parents=True)
    (share / 'config').mkdir()
    (share / 'launch_ROS2/msg_MID360s_launch.py').write_text('# fixture guarded launch')
    (share / 'launch_ROS2/livox_driver_guard.py').write_text('# fixture shared ownership guard')
    original=json.dumps(dict(lidar_configs=[dict(ip='192.168.1.135')],
        Mid360s=dict(host_net_info=[dict(host_ip='192.168.1.50',point_data_port=56301)])))
    (share / 'config/MID360s_config.json').write_text(original)
    packages = []
    manager.package = lambda package, binary: packages.append((package, binary)) or prefix
    def runner(command,**_kwargs):
        if command[:5]==['ip','-j','-4','route','get']:
            return completed('[{"dev":"lidar0","prefsrc":"192.168.1.100"}]')
        if command[:4]==['ip','-j','-4','address']:
            return completed('[{"ifname":"lidar0","addr_info":[{"local":"192.168.1.100","prefixlen":24}]}]')
        if command[0]=='ss':return completed()
        pytest.fail('unexpected command '+repr(command))
    manager.runner=runner
    manager.preflight(sources.RECORDING_MID360)
    assert packages == [('livox_ros_driver2', 'livox_ros_driver2_node')]
    assert not (manager.workspace / 'run_fastlio.sh').exists()
    assert (share/'config/MID360s_config.json').read_text()==original
    assert json.loads(manager.mid360_config.read_text())['Mid360s']['host_net_info'][0]['host_ip']=='192.168.1.100'
    assert manager.mid360_config.stat().st_mode&0o777==0o600
    assert 'user_config_path:='+str(manager.mid360_config) in manager.source_command(sources.RECORDING_MID360)


def test_multiple_existing_drivers_are_reported_without_duplicate_or_stop(manager):
    process(manager.proc_root, 12, ['/ws/livox_ros_driver2_node'])
    process(manager.proc_root, 13, ['/ws/livox_ros_driver2_node'])
    manager.runner = lambda *_args, **_kwargs: pytest.fail('duplicate state triggered a system command')
    report = manager.recording_source(sources.RECORDING_MID360)
    assert report['state'] == 'failed'


def test_jetson_start_reuses_existing_berxel_uvc_and_archive_without_other_services(manager):
    manager.role = 'jetson'
    calls = []
    manager.support = lambda unit, status: calls.append(unit) or dict(key='robot-native-archive', state='reused')
    manager.recording_source = lambda source, status: calls.append(source.key) or dict(key=source.key, state='reused')
    manager.owner_module = FakeOwner(active=True, healthy=True)
    manager.fisheye = lambda status: dict(key='fisheye', state='skipped', message='未连接')
    manager.restore_jetson_archive_policy = lambda: pytest.fail('recording changed archive policy')
    manager.berxel_preview = lambda *_: pytest.fail('recording started a preview')
    report = manager.recording_only()
    assert report['recordingOnly'] and report['partial'] and not report['ok']
    assert calls == ['robot-native-archive.service', 'berxel']
    assert manager.owner_module.commands == []
    assert [row['key'] for row in report['devices']] == ['robot-native-archive', 'berxel', 'usb-cameras', 'fisheye']


def preview_sources(manager, *, driver_state='reused', preview_state='reused', frame_state='reused'):
    manager.role = 'jetson'
    calls = []
    def row(key, state, status=None, **details):
        calls.append((key, status))
        return dict(key=key, state=state, **details)
    manager.support = lambda unit, status: row(unit.removesuffix('.service'), 'reused', status)
    manager.recording_source = lambda source, status: row(source.key, driver_state, status, dataVerified=False)
    manager.berxel_preview = lambda status: row('berxel-preview', preview_state, status, dataVerified=False)
    manager.berxel_frames = lambda: row('berxel-frames', frame_state, dataVerified=frame_state == 'reused')
    manager.camera_yolo = lambda status: row('robot-cloud-yolo-observer', 'reused', status, dataVerified=False)
    manager.camera = lambda status, **_kwargs: row('usb-cameras', 'reused', status, dataVerified=True)
    manager.fisheye = lambda status: row('fisheye', 'reused', status)
    manager.restore_jetson_archive_policy = lambda: pytest.fail('preview changed archive policy')
    return calls


@pytest.mark.parametrize('status', [False, True])
def test_opt_in_preview_checks_driver_then_read_only_preview_frames_and_preserves_raw(manager, status):
    calls = preview_sources(manager)
    report = manager.recording_only(status=status, with_camera_preview=True)
    assert report['ok']
    assert calls == [('robot-native-archive', status), ('berxel', status),
                     ('berxel-preview', status), ('berxel-frames', None),
                     ('usb-cameras', status), ('fisheye', status)]
    assert report['devices'][1]['dataVerified'] is False
    assert report['devices'][3]['dataVerified'] is True


@pytest.mark.parametrize('driver_state', ['failed', 'inactive'])
def test_preview_is_never_started_for_unconfirmed_berxel_and_other_cameras_continue(manager, driver_state):
    calls = preview_sources(manager, driver_state=driver_state)
    report = manager.recording_only(with_camera_preview=True)
    assert not report['ok'] and report['partial']
    assert calls == [('robot-native-archive', False), ('berxel', False),
                     ('usb-cameras', False), ('fisheye', False)]
    preview, frames = report['devices'][2:4]
    assert preview['state'] == frames['state'] == 'skipped'
    assert '未启动网页预览' in preview['message']


@pytest.mark.parametrize('failure', ['preview', 'frames'])
def test_preview_failure_never_changes_raw_driver_or_stops_other_recording_sources(manager, failure):
    calls = preview_sources(manager, preview_state='failed' if failure == 'preview' else 'reused',
                            frame_state='failed' if failure == 'frames' else 'reused')
    report = manager.recording_only(with_camera_preview=True)
    assert not report['ok'] and report['partial']
    assert report['devices'][1] == dict(key='berxel', state='reused', dataVerified=False)
    assert 'berxel' in report['available'] and 'usb-cameras' in report['available']
    assert ('berxel-frames', None) in calls if failure == 'frames' else ('berxel-frames', None) not in calls
    assert calls[-2:] == [('usb-cameras', False), ('fisheye', False)]
    assert '预览失败不停止原始录制' in report['message']


def test_preview_dry_run_lists_existing_low_load_config_without_commands_or_writes(tmp_path):
    manager = sources.RecordingDeviceSources('jetson', home=tmp_path / 'absent',
        runner=lambda *_args, **_kwargs: pytest.fail('dry run executed a command'))
    report = manager.recording_only(dry_run=True, with_camera_preview=True)
    assert report['ok'] and report['dryRun'] and not (tmp_path / 'absent').exists()
    preview = next(row for row in report['devices'] if row['key'] == 'berxel-preview')
    for key, value in sources.BERXEL_PREVIEW_ENV.items():
        assert '--setenv=' + key + '=' + value in preview['command']
    assert any(row['key'] == 'berxel-frames' for row in report['devices'])
    for forbidden in ('yolo', 'aruco', 'run_fastlio', 'relay.service', 'gateway.service'):
        assert forbidden not in json.dumps(report).lower()


def test_cancel_during_preview_prevents_frame_probe_and_remaining_device_starts(manager):
    calls = preview_sources(manager)
    stop = False
    manager.cancelled = lambda: stop
    def preview(_status):
        nonlocal stop
        stop = True
        return dict(key='berxel-preview', state='started', dataVerified=False)
    manager.berxel_preview = preview
    report = manager.recording_only(with_camera_preview=True)
    assert report['cancelled'] and not report['ok']
    assert calls == [('robot-native-archive', False), ('berxel', False)]
    assert report['devices'][2]['state'] == 'started'
    assert {row['key'] for row in report['devices'] if row['state'] == 'failed'} == {
        'berxel-frames', 'usb-cameras', 'fisheye'}


@pytest.mark.parametrize('arguments', [
    ['ipc', '--recording-only', '--with-camera-preview'],
    ['jetson', '--with-camera-preview'],
    ['jetson', '--recording-only', '--no-camera-source', '--with-camera-preview'],
    ['jetson', '--stop-recording-cameras', '--with-camera-preview'],
])
def test_preview_cli_rejects_wrong_scope_before_any_device_operations(monkeypatch, arguments):
    monkeypatch.setattr(sources, 'RecordingDeviceSources',
                        lambda *_args: pytest.fail('invalid CLI instantiated device manager'))
    with pytest.raises(SystemExit) as error:
        sources.main(arguments)
    assert error.value.code == 2


def test_preview_cli_forwards_explicit_scope_and_status(manager, monkeypatch, capsys):
    calls = preview_sources(manager)
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    assert sources.main(['jetson', '--recording-only', '--with-camera-preview', '--status']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] and report['ok']
    assert ('berxel-preview', True) in calls


@pytest.mark.parametrize('status', [False, True])
def test_opt_in_yolo_follows_healthy_uvc_and_can_be_combined_with_preview(manager, status):
    calls = preview_sources(manager)
    report = manager.recording_only(status=status, with_camera_preview=True, with_camera_yolo=True)
    assert report['ok']
    assert calls == [('robot-native-archive', status), ('berxel', status),
                     ('berxel-preview', status), ('berxel-frames', None), ('usb-cameras', status),
                     ('robot-cloud-yolo-observer', status), ('fisheye', status)]
    assert '识别失败不停止原始录制' in report['message']


@pytest.mark.parametrize('uvc', [
    dict(state='failed'), dict(state='inactive'), dict(state='partial'),
    dict(state='partial', dataVerified=True, dataVerifiedFor=[]),
])
def test_yolo_is_not_started_for_unavailable_or_unverified_shared_camera(manager, uvc):
    calls = preview_sources(manager)
    manager.camera = lambda *_args, **_kwargs: dict(key='usb-cameras', **uvc)
    report = manager.recording_only(with_camera_yolo=True)
    yolo = next(row for row in report['devices'] if row['key'] == 'robot-cloud-yolo-observer')
    assert yolo['state'] == 'skipped' and '共享相机未就绪' in yolo['message']
    assert ('robot-cloud-yolo-observer', False) not in calls
    assert calls[-1] == ('fisheye', False)
    assert 'berxel' in report['available']


def test_yolo_can_reuse_verified_partial_shared_camera_without_reopening_devices(manager):
    calls = preview_sources(manager)
    manager.camera = lambda *_args, **_kwargs: dict(key='usb-cameras', state='partial',
        dataVerified=True, dataVerifiedFor=['camera2'], missingCameras=['camera1', 'camera3', 'camera4'])
    report = manager.recording_only(with_camera_yolo=True)
    assert ('robot-cloud-yolo-observer', False) in calls
    assert 'robot-cloud-yolo-observer' in report['available']


@pytest.mark.parametrize('status', [False, True])
@pytest.mark.parametrize('active', [False, True])
def test_recording_yolo_uses_only_existing_managed_unit_and_preserves_model_config(manager, status, active):
    preview_sources(manager)
    manager.camera_yolo = sources.RecordingDeviceSources.camera_yolo.__get__(manager)
    commands = []
    initially_active = active
    group = '/user.slice/user-1000.slice/app.slice/' + sources.YOLO_UNIT
    def create_owner():
        process(manager.proc_root, 61, ['/home/fixture/venv/bin/python', '-B', str(manager.yolo_script)])
        (manager.proc_root / '61/cgroup').write_text('0::' + group + '\n')
    if active:
        create_owner()
    def runner(command, **_kwargs):
        nonlocal active
        commands.append(command)
        if command[:3] == ['systemctl', '--user', 'show']:
            assert command[3] == sources.YOLO_UNIT
            return completed('LoadState=loaded\nActiveState=' + ('active' if active else 'inactive')
                + '\nMainPID=' + ('61' if active else '0') + '\nControlGroup=' + (group if active else '') + '\n')
        assert command == ['systemctl', '--user', 'start', sources.YOLO_UNIT]
        active = True
        create_owner()
        return completed()
    manager.runner = runner
    report = manager.recording_only(status=status, with_camera_yolo=True)
    yolo = next(row for row in report['devices'] if row['key'] == 'robot-cloud-yolo-observer')
    assert yolo['state'] == ('reused' if initially_active else 'inactive' if status else 'started')
    if status:
        assert all(command[:3] == ['systemctl', '--user', 'show'] for command in commands)
    # A new worker also receives one read-only invocation identity probe.
    assert len(commands) <= (4 if not initially_active and not status else 3)
    assert all(command[3] == sources.YOLO_UNIT for command in commands)


def test_yolo_start_failure_is_independent_of_raw_capture_and_fisheye(manager):
    calls = preview_sources(manager)
    manager.camera_yolo = lambda status: dict(key='robot-cloud-yolo-observer', state='failed',
        dataVerified=False, error='existing model unavailable')
    report = manager.recording_only(with_camera_yolo=True)
    assert not report['ok'] and report['partial']
    assert {'berxel', 'usb-cameras', 'fisheye'}.issubset(report['available'])
    assert calls[-1] == ('fisheye', False)
    assert '识别失败不停止原始录制' in report['message']


def test_cancel_after_uvc_never_starts_yolo_or_remaining_devices(manager):
    calls = preview_sources(manager)
    stop = False
    manager.cancelled = lambda: stop
    def camera(*_args, **_kwargs):
        nonlocal stop
        stop = True
        return dict(key='usb-cameras', state='reused', dataVerified=True)
    manager.camera = camera
    report = manager.recording_only(with_camera_yolo=True)
    assert report['cancelled'] and not report['ok']
    assert calls == [('robot-native-archive', False), ('berxel', False)]
    assert {row['key'] for row in report['devices'] if row['state'] == 'failed'} == {
        'robot-cloud-yolo-observer', 'fisheye'}


def test_yolo_dry_run_plans_only_existing_unit_without_loading_model_or_hardware(manager):
    report = manager.recording_only(dry_run=True)  # IPC default remains radar-only.
    assert 'robot-cloud-yolo-observer' not in json.dumps(report)
    manager.role = 'jetson'
    manager.runner = lambda *_args, **_kwargs: pytest.fail('dry run executed a command')
    report = manager.recording_only(dry_run=True, with_camera_yolo=True)
    keys = [row['key'] for row in report['devices']]
    assert keys.index('usb-cameras') < keys.index('robot-cloud-yolo-observer')
    yolo = next(row for row in report['devices'] if row['key'] == 'robot-cloud-yolo-observer')
    assert yolo['command'] == ['systemctl', '--user', 'start', sources.YOLO_UNIT]
    assert 'berxel-preview' not in keys and 'berxel-frames' not in keys
    assert 'model' not in json.dumps(yolo['command']) and 'download' not in json.dumps(yolo['command'])


@pytest.mark.parametrize('arguments', [
    ['ipc', '--recording-only', '--with-camera-yolo'],
    ['jetson', '--with-camera-yolo'],
    ['jetson', '--recording-only', '--no-camera-source', '--with-camera-yolo'],
    ['jetson', '--stop-recording-cameras', '--with-camera-yolo'],
])
def test_yolo_cli_rejects_wrong_scope_before_any_device_operations(monkeypatch, arguments):
    monkeypatch.setattr(sources, 'RecordingDeviceSources',
                        lambda *_args: pytest.fail('invalid CLI instantiated device manager'))
    with pytest.raises(SystemExit) as error:
        sources.main(arguments)
    assert error.value.code == 2


def test_yolo_cli_forwards_explicit_scope_and_read_only_status(manager, monkeypatch, capsys):
    calls = preview_sources(manager)
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    assert sources.main(['jetson', '--recording-only', '--with-camera-yolo', '--status']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] and report['ok']
    assert ('robot-cloud-yolo-observer', True) in calls
    assert ('berxel-preview', True) not in calls


@pytest.mark.parametrize('interpreter', ['/home/fixture/venv/bin/python', '/usr/bin/python3', '/usr/bin/python3.10'])
def test_external_yolo_script_owner_is_preserved_without_second_gpu_model(manager, interpreter):
    process(manager.proc_root, 51, [interpreter, '-B', str(manager.yolo_script)])
    calls = []
    manager.runner = lambda command, **_kwargs: calls.append(command) or completed(
        'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n')
    report = manager.camera_yolo()
    assert report['state'] == 'failed' and '其他入口' in report['error']
    assert (manager.proc_root / '51').exists()
    assert len(calls) == 1 and calls[0][:3] == ['systemctl', '--user', 'show']


@pytest.mark.parametrize('issue', ['wrong-pid', 'wrong-cgroup', 'wrong-script', 'missing-process'])
def test_unverified_active_yolo_unit_is_never_claimed_restarted_or_duplicated(manager, issue):
    group = '/user.slice/user-1000.slice/app.slice/' + sources.YOLO_UNIT
    if issue != 'missing-process':
        process(manager.proc_root, 51, ['/usr/bin/python3', '-B',
            '/unrelated/yolo_observer.py' if issue == 'wrong-script' else str(manager.yolo_script)])
        (manager.proc_root / '51/cgroup').write_text('0::' + ('/unrelated.service' if issue == 'wrong-cgroup' else group) + '\n')
    calls = []
    manager.runner = lambda command, **_kwargs: calls.append(command) or completed(
        'LoadState=loaded\nActiveState=active\nMainPID=' + ('52' if issue == 'wrong-pid' else '51')
        + '\nControlGroup=' + group + '\n')
    report = manager.camera_yolo()
    assert report['state'] == 'failed' and '身份无法确认' in report['error']
    assert len(calls) == 1 and calls[0][:3] == ['systemctl', '--user', 'show']


def test_multiple_yolo_owners_fail_before_any_service_command(manager):
    for pid in (51, 52):
        process(manager.proc_root, pid, ['/usr/bin/python3', '-B', str(manager.yolo_script)])
    manager.runner = lambda *_args, **_kwargs: pytest.fail('duplicate state ran a service command')
    report = manager.camera_yolo()
    assert report['state'] == 'failed' and '多个' in report['error']


def test_external_yolo_appearing_during_service_check_prevents_late_start(manager):
    calls = []
    def runner(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            process(manager.proc_root, 51, ['/home/fixture/venv/bin/python', '-B', str(manager.yolo_script)])
        return completed('LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n')
    manager.runner = runner
    report = manager.camera_yolo()
    assert report['state'] == 'failed' and '其他入口' in report['error']
    assert len(calls) == 2
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_failed_existing_yolo_start_has_no_model_download_config_rewrite_or_fallback(manager):
    calls = []
    def runner(command, **_kwargs):
        calls.append(command)
        if command[:3] == ['systemctl', '--user', 'show']:
            return completed('LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n')
        assert command == ['systemctl', '--user', 'start', sources.YOLO_UNIT]
        return completed(code=1)
    manager.runner = runner
    before = set(manager.home.rglob('*'))
    report = manager.camera_yolo()
    assert report['state'] == 'failed' and '本地模型' in report['error']
    assert set(manager.home.rglob('*')) == before
    assert calls[-1] == ['systemctl', '--user', 'start', sources.YOLO_UNIT]
    assert len(calls) == 2


def test_offline_source_does_not_prevent_other_sources_being_started(manager):
    calls = []
    def source(row, status):
        calls.append(row.key)
        return dict(key=row.key, state='failed' if row.key == 'mid360' else 'started')
    manager.recording_source = source
    report = manager.recording_only()
    assert calls == ['mid360', 'stl19p', 'bluesea']
    assert not report['ok'] and report['partial']
    assert report['available'] == ['stl19p', 'bluesea']


def test_cancelled_during_preflight_prevents_late_systemd_start(manager):
    stop = False
    manager.cancelled = lambda: stop
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed('LoadState=loaded\nActiveState=inactive\n')
    def preflight(_):
        nonlocal stop
        stop = True
    manager.preflight = preflight
    report = manager.recording_only()
    assert report['cancelled'] and not report['ok']
    assert all(row['state'] == 'failed' for row in report['devices'])
    assert not any(command[0] == 'systemd-run' for command in calls)


def test_cancelled_between_sources_preserves_completed_source_and_skips_the_rest(manager):
    stop = False
    manager.cancelled = lambda: stop
    def first(source, status):
        nonlocal stop
        stop = True
        return dict(key=source.key, state='reused')
    manager.recording_source = first
    report = manager.recording_only()
    assert report['cancelled'] and report['partial']
    assert [row['state'] for row in report['devices']] == ['reused', 'failed', 'failed']


def test_uvc_cancellation_after_validation_never_submits_a_start(manager):
    manager.role = 'jetson'
    owner = FakeOwner()
    manager.owner_module = owner
    stop = False
    manager.cancelled = lambda: stop
    def cancel_after_validation():
        nonlocal stop
        stop = True
    owner.validate_start_target = cancel_after_validation
    with pytest.raises(sources.RecordingStartCancelled):
        manager.camera(before_start=manager.check_cancelled)
    assert owner.commands == []


def test_missing_fixed_fisheye_mapping_is_skipped_without_guessing_or_commands(manager):
    manager.fisheye_identity = lambda: (_ for _ in ()).throw(FileNotFoundError())
    manager.runner = lambda *_args, **_kwargs: pytest.fail('absent fish camera caused command')
    manager.load_camera_owner = lambda: pytest.fail('absent fish camera loaded owner')
    report = manager.fisheye()
    assert report['state'] == 'skipped' and 'by-id' in report['message']


def test_fisheye_mapping_overlapping_shared_uvc_is_refused_under_owner_lock(manager):
    owner = FakeOwner()
    owner.camera_nodes = lambda: ({123: '/dev/video0'}, [])
    manager.owner_module = owner
    manager.fisheye_identity = lambda: 123
    manager.runner = lambda *_args, **_kwargs: pytest.fail('overlapping fish camera caused command')
    report = manager.fisheye()
    assert report['state'] == 'failed' and '重叠' in report['error']
    assert owner.lock_count == 1 and not owner.commands


def test_active_unconfirmed_device_unit_never_restarted_or_duplicated(manager):
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed('LoadState=loaded\nActiveState=active\n')
    report = manager.recording_source(sources.SOURCES['berxel'])
    assert report['state'] == 'failed'
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_new_berxel_launch_retains_async_nonblocking_transport(manager):
    command = manager.start_command(sources.SOURCES['berxel'])
    assert 'RMW_FASTRTPS_PUBLICATION_MODE=ASYNCHRONOUS' in command
    assert 'FASTRTPS_DEFAULT_PROFILES_FILE=' + str(manager.camera_transport_profile) in command
    assert 'unset ROS_DISCOVERY_SERVER' not in sources.RECORDING_ROS_SHELL
    assert 'MID360_NAV2_DDS_SERVER=off' not in sources.RECORDING_ROS_SHELL


def test_archive_dds_settings_are_explicit_in_service_command_and_never_source_secrets(manager, monkeypatch):
    keys = ('ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION', 'ROS_DISCOVERY_SERVER',
            'CYCLONEDDS_URI', 'FASTRTPS_DEFAULT_PROFILES_FILE', 'RMW_FASTRTPS_USE_QOS_FROM_XML',
            'RMW_FASTRTPS_PUBLICATION_MODE', 'MID360_NAV2_DDS_SERVER')
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    config = manager.home / '.config/robot-cloud/archive.env'
    config.parent.mkdir(parents=True)
    config.write_text("ROS_DOMAIN_ID=7\nROS_DISCOVERY_SERVER='10.0.0.3:11811'\n"
                      "ARCHIVE_DBPROG_PASSWORD='never-print-private-password'\n"
                      "FASTRTPS_DEFAULT_PROFILES_FILE='/home/profile with spaces.xml'\n")
    command = manager.start_command(sources.SOURCES['berxel'])
    assert 'ROS_DOMAIN_ID=7' in command and 'ROS_DISCOVERY_SERVER=10.0.0.3:11811' in command
    assert 'FASTRTPS_DEFAULT_PROFILES_FILE=/home/profile with spaces.xml' in command
    assert 'never-print-private-password' not in str(command)
    monkeypatch.setenv('ROS_DOMAIN_ID', '8')
    assert 'ROS_DOMAIN_ID=8' in manager.start_command(sources.RECORDING_MID360)
    assert str(manager.camera_transport_profile) == '/home/profile with spaces.xml'


def test_missing_driver_guard_refuses_starting_an_unguarded_mid360(manager, tmp_path):
    fake_environment(manager, tmp_path)
    prefix = tmp_path / 'livox'
    launch = prefix / 'share/livox_ros_driver2/launch_ROS2/msg_MID360s_launch.py'
    launch.parent.mkdir(parents=True)
    launch.write_text('# fixture unguarded launch')
    manager.package = lambda *_: prefix
    manager.ping = lambda *_: pytest.fail('must reject missing guard before touching network')
    with pytest.raises(sources.SourceError, match='共享占用保护'):
        manager.preflight(sources.RECORDING_MID360)


def test_recording_cli_rejects_stop_before_instantiating_sources(monkeypatch):
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda *_: pytest.fail('invalid CLI instantiated sources'))
    with pytest.raises(SystemExit) as error:
        sources.main(['ipc', '--recording-only', '--stop'])
    assert error.value.code == 2


@pytest.mark.parametrize('arguments', [
    ['ipc', '--read-only-sources'],
    ['jetson', '--recording-only', '--read-only-sources'],
    *[['ipc', '--recording-only', '--read-only-sources', flag]
      for flag in ('--stop', '--stop-recording-cameras', '--stop-owned-recording-cameras',
                   '--dry-run', '--no-camera-source', '--cameras-only',
                   '--with-camera-preview', '--with-camera-yolo')],
    ['ipc', '--recording-only', '--read-only-sources', '--camera-ownership-json', '{}'],
])
def test_read_only_source_cli_rejects_other_scopes_before_any_operations(monkeypatch, arguments):
    monkeypatch.setattr(sources, 'RecordingDeviceSources',
        lambda *_args: pytest.fail('invalid read-only CLI instantiated a manager'))
    monkeypatch.setattr(sources, 'DeviceSources',
        lambda *_args: pytest.fail('invalid read-only CLI reached all-device operations'))
    with pytest.raises(SystemExit) as error:
        sources.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('status_flag', [[], ['--status']])
def test_read_only_source_cli_always_forwards_status_and_marks_no_control(
        manager, monkeypatch, capsys, status_flag):
    checked = []
    def inspect(source, status):
        checked.append((source.key, status))
        return dict(key=source.key, state='reused')
    manager.recording_source = inspect
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    assert sources.main(['ipc', '--recording-only', '--read-only-sources', *status_flag]) == 0
    assert checked == [(key, True) for key in ('mid360', 'stl19p', 'bluesea')]
    report = json.loads(capsys.readouterr().out)
    assert report['readOnlySources'] is True and report['sourceControl'] is False
    assert report['status'] is True and report['recordingOnly'] is True


def test_read_only_inspection_of_offline_radars_only_queries_exact_units(manager):
    commands = []
    def runner(command, **_options):
        commands.append(command)
        assert command[:3] == ['systemctl', '--user', 'show']
        return completed('LoadState=not-found\nActiveState=inactive\n')
    manager.runner = runner
    manager.preflight = lambda *_args: pytest.fail('read-only inspection ran device preflight')
    manager.start_command = lambda *_args: pytest.fail('read-only inspection prepared source startup')
    report = manager.inspect_recording_sources()
    assert len(commands) == 3
    assert {command[3] for command in commands} == {
        sources.RECORDING_MID360.unit, sources.SOURCES['stl19p'].unit, sources.SOURCES['bluesea'].unit}
    assert {row['state'] for row in report['devices']} == {'inactive'}
    assert report['readOnlySources'] is True and report['sourceControl'] is False
    assert report['ok'] is False


@pytest.mark.parametrize('command', [
    ['systemctl', '--user', 'start', 'robot-device-mid360.service'],
    ['systemctl', '--user', 'stop', 'robot-device-mid360.service'],
    ['systemctl', '--user', 'restart', 'robot-native-archive.service'],
    ['systemctl', '--user', 'show', 'robot-device-berxel.service'],
    ['systemd-run', '--user', '--unit=robot-device-mid360.service'],
    ['ros2', 'launch', 'livox_ros_driver2', 'msg_MID360s_launch.py'],
])
def test_read_only_manager_blocks_non_status_commands_before_runner(manager, command):
    manager.inspect_recording_sources()
    manager.runner = lambda *_args, **_kwargs: pytest.fail('read-only guard dispatched a forbidden command')
    with pytest.raises(sources.SourceError, match='只读雷达检查'):
        manager.run(command)


def test_read_only_inspection_retains_unverifiable_running_driver_without_commands(manager):
    process(manager.proc_root, 12, ['/ws/livox_ros_driver2_node'])
    manager.runner = lambda command, **_kwargs: (
        pytest.fail('attempted driver control') if command[:3] != ['systemctl', '--user', 'show']
        else completed('LoadState=not-found\nActiveState=inactive\n'))
    report = manager.inspect_recording_sources()
    row = next(row for row in report['devices'] if row['key'] == 'mid360')
    assert row['state'] == 'failed' and row.get('error')
    assert sources.exact_processes(manager.proc_root, sources.RECORDING_MID360.driver) == [12]
    assert report['sourceControl'] is False


def test_real_sigterm_during_start_returns_cancelled_json_and_restores_handler(manager, monkeypatch, capsys):
    previous = sources.signal.getsignal(sources.signal.SIGTERM)
    def interrupted_source(_source, _status):
        sources.signal.raise_signal(sources.signal.SIGTERM)
        pytest.fail('start continued after SIGTERM')
    manager.recording_source = interrupted_source
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    assert sources.main(['ipc', '--recording-only']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['cancelled'] and not report['ok'] and report['recordingOnly']
    assert all(row['state'] == 'failed' for row in report['devices'])
    assert sources.signal.getsignal(sources.signal.SIGTERM) == previous


def test_ros_setup_cannot_silently_change_the_recording_dds_graph(manager, tmp_path, monkeypatch):
    # Execute only a temporary shell and a JSON-printing Python child. None of
    # the fixtures contain ROS commands, device access or service operations.
    import os
    import subprocess
    for key in manager.dds_environment():
        monkeypatch.delenv(key, raising=False)
    for key in ('FASTRTPS_DEFAULT_PROFILES_FILE', 'MID360_NAV2_DDS_SERVER'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ROS_DOMAIN_ID', '7')
    monkeypatch.setenv('ROS_DISCOVERY_SERVER', '10.0.0.3:11811')
    manager.ros_setup = tmp_path / 'ros-setup.sh'
    manager.ros_setup.write_text('export ROS_DOMAIN_ID=1\n')
    (manager.workspace / 'install').mkdir(parents=True)
    (manager.workspace / 'install/setup.bash').write_text('export ROS_DOMAIN_ID=2\n')
    (manager.workspace / 'env.sh').write_text('unset ROS_DISCOVERY_SERVER\n'
        'export FASTRTPS_DEFAULT_PROFILES_FILE=/incorrect-from-workspace.xml\n')
    code = 'import json,os;print(json.dumps({key:os.environ.get(key) for key in '+repr([
        'ROS_DOMAIN_ID', 'ROS_DISCOVERY_SERVER', 'FASTRTPS_DEFAULT_PROFILES_FILE'])+'}))'
    child = subprocess.Popen(manager.ros_command(['/usr/bin/python3', '-c', code]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    output, error = child.communicate(timeout=3)
    assert child.returncode == 0, error
    assert json.loads(output) == {'ROS_DOMAIN_ID': '7', 'ROS_DISCOVERY_SERVER': '10.0.0.3:11811',
                                 'FASTRTPS_DEFAULT_PROFILES_FILE': None}


@pytest.mark.parametrize('status', [False, True])
def test_preview_only_covers_all_sources_without_archive_or_gate_writes(manager, status):
    calls = preview_sources(manager)
    manager.recording_only = lambda **_: pytest.fail('camera mode entered recording startup')
    gates = manager.home / '.local/state/robot-cloud/garage-control'
    gates.mkdir(parents=True)
    for name in ('garage.json', 'desktop-lease.json'):
        (gates / name).write_text('{"schemaVersion":1,"scope":"garage","enabled":false}')
    before = {path: path.read_bytes() for path in gates.iterdir()}
    files_before = set(manager.home.rglob('*'))
    report = manager.preview_only(status=status)
    assert report['ok'] and report['camerasOnly'] and report['status'] is status
    assert not report['dryRun'] and not report['dataVerified'] and not report['partial']
    assert calls == [(unit.removesuffix('.service'), status) for unit in sources.CAMERA_SUPPORT] + [
        ('berxel', status), ('berxel-preview', status), ('berxel-frames', None),
        ('usb-cameras', status), ('robot-cloud-yolo-observer', status), ('fisheye', status)]
    assert {path: path.read_bytes() for path in gates.iterdir()} == before
    assert set(manager.home.rglob('*')) == files_before
    assert all(row['key'] not in ('mid360', 'stl19p', 'bluesea', 'robot-native-archive')
               for row in report['devices'])


def test_preview_only_dry_run_never_reads_devices_or_loads_helpers_or_writes(tmp_path):
    def forbidden(*_args, **_kwargs):
        pytest.fail('camera-only dry run touched a device or executed an operation')
    manager = sources.RecordingDeviceSources('jetson', home=tmp_path / 'absent-home',
        proc_root=tmp_path / 'absent-proc', usb_root=tmp_path / 'absent-usb', runner=forbidden)
    for name in ('load_camera_owner', 'recording_source', 'berxel_preview', 'berxel_frames',
                 'camera', 'camera_yolo', 'fisheye', 'restore_jetson_archive_policy', 'support'):
        setattr(manager, name, forbidden)
    report = manager.preview_only(dry_run=True)
    assert report['ok'] and report['dryRun'] and report['camerasOnly']
    assert not (tmp_path / 'absent-home').exists()
    keys = [row['key'] for row in report['devices']]
    assert keys == [unit.removesuffix('.service') for unit in sources.CAMERA_SUPPORT] + [
        'berxel', 'berxel-preview', 'berxel-frames', 'usb-cameras', 'robot-cloud-yolo-observer', 'fisheye']
    commands = json.dumps(report)
    for forbidden_text in ('robot-native-archive', 'run_fastlio', 'livox', 'stl19p', 'bluesea', 'garage.json'):
        assert forbidden_text not in commands


@pytest.mark.parametrize('failure', ['berxel', 'preview', 'frames', 'gateway', 'yolo'])
def test_preview_only_failure_preserves_running_sources_and_checks_remaining_cameras(manager, failure):
    calls = preview_sources(manager, driver_state='failed' if failure == 'berxel' else 'reused',
                            preview_state='failed' if failure == 'preview' else 'reused',
                            frame_state='failed' if failure == 'frames' else 'reused')
    if failure == 'gateway':
        manager.support = lambda unit, status: dict(key=unit.removesuffix('.service'), state='failed')
    if failure == 'yolo':
        manager.camera_yolo = lambda status: dict(key='robot-cloud-yolo-observer', state='failed')
    manager.stop_recording_cameras = lambda **_: pytest.fail('camera failure stopped hardware')
    report = manager.preview_only()
    assert not report['ok'] and report['partial'] and not report['cancelled']
    assert 'usb-cameras' in report['available'] and calls[-1] == ('fisheye', False)
    if failure == 'berxel':
        assert ('berxel-preview', False) not in calls and ('berxel-frames', None) not in calls
    if failure == 'preview':
        assert ('berxel-frames', None) not in calls
    if failure != 'berxel':
        assert 'berxel' in report['available']


@pytest.mark.parametrize('verified', [False, True])
def test_preview_only_partial_uvc_needs_actual_verified_channels_before_yolo(manager, verified):
    calls = preview_sources(manager)
    manager.camera = lambda *_args, **_kwargs: dict(key='usb-cameras', state='partial',
        dataVerified=verified, dataVerifiedFor=['camera2'] if verified else [],
        missingCameras=['camera1', 'camera3', 'camera4'])
    manager.fisheye = lambda status: dict(key='fisheye', state='skipped', message='固定鱼眼未连接')
    report = manager.preview_only()
    assert report['partial'] and report['ok'] is verified
    assert (('robot-cloud-yolo-observer', False) in calls) is verified
    uvc = next(row for row in report['devices'] if row['key'] == 'usb-cameras')
    assert uvc['missingCameras'] == ['camera1', 'camera3', 'camera4']
    assert report['devices'][-1]['state'] == 'skipped'


def test_preview_only_status_uses_existing_helpers_without_start_or_new_files(manager):
    manager.role = 'jetson'
    commands = []
    def runner(command, **_kwargs):
        commands.append(command)
        assert command[:3] == ['systemctl', '--user', 'show']
        assert command[3] in (*sources.CAMERA_SUPPORT, sources.SOURCES['berxel'].unit, sources.YOLO_UNIT)
        return completed('LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n')
    manager.runner = runner
    owner = manager.owner_module = FakeOwner(active=True, healthy=True)
    manager.fisheye_identity = lambda: (_ for _ in ()).throw(FileNotFoundError())
    before = set(manager.home.rglob('*'))
    report = manager.preview_only(status=True)
    assert not report['ok'] and report['status'] and report['partial']
    assert owner.commands == [] and owner.lock_count == 0
    assert len(commands) == 4 and set(manager.home.rglob('*')) == before
    assert report['devices'][-1]['state'] == 'skipped'


@pytest.mark.parametrize('status', [False, True])
def test_preview_only_healthy_camera_does_not_require_or_probe_archival(manager, status):
    preview_sources(manager)
    del manager.camera  # exercise the real RecordingDeviceSources camera method
    owner = manager.owner_module = FakeOwner(active=True, healthy=True)
    manager.camera_archive_ready = lambda *_args, **_kwargs: pytest.fail(
        'preview-only must not require an archive configuration or writer')
    manager.camera_archive_http_status = lambda: pytest.fail('preview-only probed the archive tap')
    before = set(manager.home.rglob('*'))
    report = manager.preview_only(status=status)
    assert report['ok'] is True and report['partial'] is False
    usb = next(row for row in report['devices'] if row['key'] == 'usb-cameras')
    assert usb['state'] == 'reused' and usb['dataVerified'] is True
    assert 'archiveReady' not in usb
    assert not owner.commands and set(manager.home.rglob('*')) == before


def test_preview_only_cancellation_keeps_completed_devices_and_prevents_late_starts(manager):
    calls = preview_sources(manager)
    stopped = False
    manager.cancelled = lambda: stopped
    def camera(*_args, **_kwargs):
        nonlocal stopped
        stopped = True
        return dict(key='usb-cameras', state='reused', dataVerified=True)
    manager.camera = camera
    report = manager.preview_only()
    assert report['cancelled'] and report['partial'] and not report['ok']
    assert ('robot-cloud-yolo-observer', False) not in calls and ('fisheye', False) not in calls
    assert {'berxel', 'berxel-preview', 'usb-cameras'}.issubset(report['available'])
    assert {row['key'] for row in report['devices'] if row['state'] == 'failed'} == {
        'robot-cloud-yolo-observer', 'fisheye'}


@pytest.mark.parametrize('mode', [[], ['--status'], ['--dry-run']])
def test_camera_only_cli_routes_to_preview_only_without_legacy_start(manager, monkeypatch, capsys, mode):
    calls = []
    def preview_only(**options):
        calls.append(options)
        return dict(ok=True, camerasOnly=True, role='jetson', **options)
    manager.preview_only = preview_only
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    monkeypatch.setattr(sources, 'DeviceSources', lambda *_: pytest.fail('camera-only used legacy starter'))
    assert sources.main(['jetson', '--cameras-only', *mode]) == 0
    assert calls == [dict(status='--status' in mode, dry_run='--dry-run' in mode)]
    assert json.loads(capsys.readouterr().out)['camerasOnly']


@pytest.mark.parametrize('arguments', [
    ['jetson', '--cameras-only', '--stop'],
    ['jetson', '--cameras-only', '--no-camera-source'],
    ['jetson', '--cameras-only', '--no-camera-source', '--status'],
])
def test_legacy_camera_stop_and_explicit_source_skip_keep_original_route(manager, monkeypatch, capsys, arguments):
    calls = []
    manager.execute = lambda **options: calls.append(options) or dict(ok=True)
    monkeypatch.setattr(sources, 'DeviceSources', lambda _role: manager)
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda *_: pytest.fail('legacy mode used preview-only'))
    assert sources.main(arguments) == 0
    assert calls == [dict(status='--status' in arguments, dry_run=False,
        no_camera_source='--no-camera-source' in arguments, stop='--stop' in arguments, cameras_only=True)]
    capsys.readouterr()


def test_preview_only_cli_cancellation_restores_signals_and_reports_partial_result(manager, monkeypatch, capsys):
    calls = preview_sources(manager)
    previous = sources.signal.getsignal(sources.signal.SIGTERM)
    def cancelled(_source, _status):
        sources.signal.raise_signal(sources.signal.SIGTERM)
        pytest.fail('camera CLI continued after cancellation')
    manager.recording_source = cancelled
    monkeypatch.setattr(sources, 'RecordingDeviceSources', lambda _role: manager)
    assert sources.main(['jetson', '--cameras-only']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['cancelled'] and report['camerasOnly'] and report['partial']
    assert calls == [(unit.removesuffix('.service'), False) for unit in sources.CAMERA_SUPPORT]
    assert sources.signal.getsignal(sources.signal.SIGTERM) == previous


def test_preview_only_rejects_ipc_before_touching_devices(manager):
    manager.runner = lambda *_args, **_kwargs: pytest.fail('wrong role accessed system state')
    with pytest.raises(sources.SourceError, match='Jetson'):
        manager.preview_only()
