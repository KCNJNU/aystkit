"""Isolated lifecycle checks: no real systemd, sensor, network or process action."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.scripts import device_sources as sources


def completed(stdout='', code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr='')


def camera_profile(manager):
    path = manager.camera_transport_profile
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text('<fixture-private-dds-profile/>')
    path.chmod(0o600)
    return path


def process(root, pid, argv):
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / 'cmdline').write_bytes(b'\0'.join(item.encode() for item in argv) + b'\0')


@pytest.fixture(autouse=True)
def forbid_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A test attempted to execute a real system command')
    monkeypatch.setattr(sources.subprocess, 'run', forbidden)
    monkeypatch.setattr(sources.os, 'kill', forbidden)


@pytest.fixture
def manager(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    proc = tmp_path / 'proc'
    proc.mkdir()
    obj = sources.DeviceSources('ipc', home=home, proc_root=proc,
        sleeper=lambda _: None, runner=lambda *_args, **_kwargs:
        completed('LoadState=not-found\nActiveState=inactive\nSubState=dead\n'))
    return obj


@pytest.mark.parametrize('role,no_camera', [('ipc', False), ('jetson', False), ('jetson', True)])
def test_dry_run_has_no_commands_hardware_reads_imports_or_writes(tmp_path, role, no_camera):
    def forbidden(*_args, **_kwargs):
        pytest.fail('dry-run must be completely inert')
    obj = sources.DeviceSources(role, home=tmp_path / 'does-not-exist',
        proc_root=tmp_path / 'absent-proc', runner=forbidden)
    obj.load_camera_owner = forbidden
    report = obj.execute(dry_run=True, no_camera_source=no_camera)
    assert report['ok'] and report['dryRun'] and not report['dataVerified']
    assert not (tmp_path / 'does-not-exist').exists()
    commands = str(report)
    assert 'Restart=no' in commands or no_camera
    assert 'enable' not in commands and 'pkill' not in commands
    assert 'run_system.sh' not in commands and 'run_nav2.sh' not in commands


def test_exact_driver_match_does_not_match_shell_arguments_or_similar_names(manager):
    process(manager.proc_root, 10, ['/bin/bash', 'livox_ros_driver2_node'])
    process(manager.proc_root, 11, ['/tmp/fake_livox_ros_driver2_node'])
    process(manager.proc_root, 12, ['/ws/install/lib/livox_ros_driver2_node', '--ros-args'])
    assert sources.exact_processes(manager.proc_root, 'livox_ros_driver2_node') == [12]


def preview_process(manager, pid=61):
    process(manager.proc_root, pid, ['/usr/bin/python3', '-B', str(manager.preview_script)])
    group = '/user.slice/user-1000.slice/app.slice/' + sources.BERXEL_PREVIEW_UNIT
    (manager.proc_root / str(pid) / 'cgroup').write_text('0::' + group + '\n')
    return group


def preview_state(active=False, pid=0, group=''):
    return ('LoadState=' + ('loaded' if active else 'not-found') + '\nActiveState='
            + ('active' if active else 'inactive') + '\nResult=success\nMainPID='
            + str(pid) + '\nControlGroup=' + group + '\n')


def test_preview_process_match_uses_actual_python_script_operand(manager):
    expected = str(manager.preview_script)
    for pid, argv in enumerate((
        ['/bin/bash', '-c', expected], ['/usr/bin/python3', '-c', 'print(1)', expected],
        ['/usr/bin/python3', '/tmp/jetson_camera_encoder.py'],
        ['/usr/bin/python3', '-B', expected], ['/usr/bin/python3.10', '-u', expected],
        ['/usr/bin/python3', '-m', 'other', expected]), start=1):
        process(manager.proc_root, pid, argv)
    assert sources.exact_python_script_processes(manager.proc_root, manager.preview_script) == [4, 5]


def test_preview_relative_script_invocation_is_not_duplicated(manager):
    manager.preview_script.parent.mkdir(parents=True)
    manager.preview_script.write_text('# fixture encoder\n')
    process(manager.proc_root, 9, ['/usr/bin/python3', '-B', './jetson_camera_encoder.py'])
    (manager.proc_root / '9/cwd').symlink_to(manager.preview_script.parent)
    assert sources.exact_python_script_processes(manager.proc_root, manager.preview_script) == [9]
    manager.runner = lambda *args, **kwargs: completed(preview_state())
    manager.preview_preflight = lambda: pytest.fail('must reuse running relative script')
    assert manager.berxel_preview()['state'] == 'reused'


def test_preview_status_is_read_only_and_identifies_missing_bridge(manager):
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed(preview_state())
    manager.preview_preflight = lambda: pytest.fail('status must not prepare a start')
    report = manager.berxel_preview(status=True)
    assert report['state'] == 'inactive' and '网页无法收到' in report['message']
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_existing_preview_reused_without_restart_or_preflight(manager):
    group = preview_process(manager)
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed(preview_state(True, 61, group))
    manager.preview_preflight = lambda: pytest.fail('existing encoder must be preserved')
    report = manager.berxel_preview()
    assert report['state'] == 'reused' and report['pids'] == [61] and not report['dataVerified']
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_preview_external_process_is_preserved_without_duplicate_start(manager):
    preview_process(manager)
    manager.runner = lambda *args, **kwargs: completed(preview_state())
    manager.preview_preflight = lambda: pytest.fail('must not start another encoder')
    report = manager.berxel_preview()
    assert report['state'] == 'reused'
    stopped = manager.stop_berxel_preview()
    assert stopped['state'] == 'external_running'


def test_preview_unknown_active_unit_is_never_stopped_or_duplicated(manager):
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed(preview_state(True, 77, '/unknown'))
    assert manager.berxel_preview()['state'] == 'failed'
    assert manager.stop_berxel_preview()['state'] == 'failed'
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_preview_start_uses_stable_profile_and_existing_ros_environment(manager):
    calls, active, group = [], False, ''
    def run(command, **kwargs):
        nonlocal active, group
        calls.append(command)
        if command[0] == 'systemd-run':
            active = True
            group = preview_process(manager)
            return completed()
        return completed(preview_state(active, 61 if active else 0, group))
    manager.runner = run
    manager.preview_preflight = lambda: None
    report = manager.berxel_preview()
    assert report['state'] == 'started' and report['dataVerified'] is False
    start = next(command for command in calls if command[0] == 'systemd-run')
    assert '--unit=' + sources.BERXEL_PREVIEW_UNIT in start
    assert start[-3:] == ['/usr/bin/python3', '-B', str(manager.preview_script)]
    assert {item for item in start if item.startswith('--setenv=')} == {
        '--setenv=' + key + '=' + value for key, value in sources.BERXEL_PREVIEW_ENV.items()}
    assert not any('stop' in command or 'restart' in command for command in calls)


def test_preview_failed_start_does_not_touch_running_driver(manager):
    process(manager.proc_root, 12, ['/ws/berxel_camera_ros2'])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return completed(code=1) if command[0] == 'systemd-run' else completed(preview_state())
    manager.runner = run
    manager.preview_preflight = lambda: None
    assert manager.berxel_preview()['state'] == 'failed'
    assert (manager.proc_root / '12').exists()
    assert not any('stop' in command or 'restart' in command for command in calls)


def test_preview_missing_installed_encoder_fails_before_start(manager, tmp_path):
    fake_environment(manager, tmp_path)
    calls = []
    manager.runner = lambda command, **kwargs: calls.append(command) or completed(preview_state())
    report = manager.berxel_preview()
    assert report['state'] == 'failed' and '缺少已部署的深度预览编码器' in report['error']
    assert not any(command[0] == 'systemd-run' for command in calls)


def test_preview_preflight_checks_all_existing_codec_files(manager, tmp_path):
    fake_environment(manager, tmp_path)
    manager.preview_script.parent.mkdir(parents=True)
    for name in ('jetson_camera_encoder.py', 'camera_codec.py', 'point_codec.py'):
        (manager.preview_script.parent / name).write_text('# isolated codec\n')
    camera_profile(manager)
    manager.preview_preflight()
    (manager.preview_script.parent / 'point_codec.py').unlink()
    with pytest.raises(sources.SourceError, match='point_codec'):
        manager.preview_preflight()


def test_preview_stop_only_verified_encoder_unit(manager):
    group = preview_process(manager)
    active, calls = True, []
    def run(command, **kwargs):
        nonlocal active
        calls.append(command)
        if command[:3] == ['systemctl', '--user', 'stop']:
            assert command[3] == sources.BERXEL_PREVIEW_UNIT
            active = False
            for p in (manager.proc_root / '61').iterdir():
                p.unlink()
            (manager.proc_root / '61').rmdir()
            return completed()
        return completed(preview_state(active, 61 if active else 0, group))
    manager.runner = run
    assert manager.stop_berxel_preview()['state'] == 'stopped'
    assert [command for command in calls if 'stop' in command] == [
        ['systemctl', '--user', 'stop', sources.BERXEL_PREVIEW_UNIT]]


def test_jetson_start_includes_preview_after_driver_before_usb(manager):
    manager.role = 'jetson'
    calls = []
    manager.support = lambda name, status: dict(key=name, state='reused')
    manager.source = lambda source, status: calls.append('driver') or dict(key=source.key, state='reused')
    manager.berxel_preview = lambda status: calls.append('preview') or dict(key='berxel-preview', state='started')
    manager.camera = lambda status: calls.append('usb') or dict(key='usb-cameras', state='reused')
    manager.berxel_frames = lambda: calls.append('frames') or dict(key='berxel-frames', state='reused', dataVerified=True)
    assert manager.execute()['ok']
    assert calls == ['driver', 'preview', 'usb', 'frames']


def test_existing_driver_is_reused_without_starting_or_preflight(manager):
    process(manager.proc_root, 12, ['/ws/livox_ros_driver2_node'])
    process(manager.proc_root, 13, ['/ws/fastlio_mapping'])
    manager.preflight = lambda *_: pytest.fail('existing driver must be left alone')
    manager.runner = lambda *_args, **_kwargs: pytest.fail('no command needed to reuse driver')
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'reused' and report['pids'] == [12]
    assert report['dataVerified'] is False


def test_mid360_driver_without_fastlio_reports_partial_without_duplicate(manager):
    process(manager.proc_root, 12, ['/ws/livox_ros_driver2_node'])
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'partial' and 'FAST-LIO' in report['error']


@pytest.mark.parametrize('lidar_running,mapping_count', [(False, 1), (True, 2)])
def test_incomplete_or_duplicate_existing_fastlio_blocks_all_new_commands(
        manager, lidar_running, mapping_count):
    if lidar_running:
        process(manager.proc_root, 12, ['/ws/livox_ros_driver2_node'])
    for index in range(mapping_count):
        process(manager.proc_root, 20 + index, ['/ws/fastlio_mapping'])
    manager.runner = lambda *_args, **_kwargs: pytest.fail('must leave existing FAST-LIO alone')
    manager.preflight = lambda *_: pytest.fail('incomplete graph must not prepare another instance')
    result = manager.source(sources.SOURCES['mid360'])
    assert result['state'] == 'failed' and 'FAST-LIO' in result['error']


def test_fastlio_appearing_during_preflight_is_not_duplicated(manager):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        assert command[:3] == ['systemctl', '--user', 'show']
        return completed('LoadState=not-found\nActiveState=inactive\n')
    manager.runner = runner
    manager.preflight = lambda *_: process(manager.proc_root, 20, ['/ws/fastlio_mapping'])
    result = manager.source(sources.SOURCES['mid360'])
    assert result['state'] == 'failed' and '检查期间 FAST-LIO' in result['error']
    assert len(calls) == 1


def test_existing_bluesea_old_scan_blocks_conflicting_stl_start(manager, tmp_path):
    params = tmp_path / 'old-blue.yaml'
    params.write_text('node:\n  scan_topic: "/scan"\n')
    process(manager.proc_root, 12, ['/ws/bluesea2_node', '--ros-args', '--params-file', str(params)])
    for key in ('stl19p', 'bluesea'):
        report = manager.source(sources.SOURCES[key])
        assert report['state'] == 'failed' and '/bluesea_new/scan' in report['error']


def test_existing_bluesea_correct_topic_is_reused(manager, tmp_path):
    params = tmp_path / 'blue.yaml'
    params.write_text('node:\n  scan_topic: "/bluesea_new/scan"\n')
    process(manager.proc_root, 12, ['/ws/bluesea2_node', '--ros-args', '--params-file', str(params)])
    report = manager.source(sources.SOURCES['bluesea'])
    assert report['state'] == 'reused'


def test_duplicate_driver_refuses_new_instance(manager):
    for pid in (12, 13):
        process(manager.proc_root, pid, ['/ws/livox_ros_driver2_node'])
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'failed' and '多个' in report['error']


def test_status_only_queries_and_does_not_start_or_write(manager):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('LoadState=not-found\nActiveState=inactive\nSubState=dead\n')
    manager.runner = runner
    report = manager.execute(status=True)
    assert not report['ok']
    assert all(item['state'] == 'inactive' for item in report['devices'])
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)
    assert not manager.params.exists()


def test_missing_ros_environment_is_a_failure_without_start(manager):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('LoadState=not-found\nActiveState=inactive\n')
    manager.runner = runner
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'failed' and '缺少运行环境' in report['error']
    assert not any(command[0] == 'systemd-run' for command in calls)


def test_active_unit_without_driver_does_not_launch_duplicate(manager):
    manager.runner = lambda *_args, **_kwargs: completed('LoadState=loaded\nActiveState=active\n')
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'failed' and '同名服务' in report['error']


def test_start_requires_active_unit_and_exact_driver_but_does_not_claim_data(manager):
    calls = []
    active = False
    def runner(command, **kwargs):
        nonlocal active
        calls.append(command)
        if command[0] == 'systemd-run':
            active = True
            process(manager.proc_root, 15, ['/ws/livox_ros_driver2_node'])
            process(manager.proc_root, 16, ['/ws/fastlio_mapping'])
            return completed()
        return completed('LoadState=loaded\nActiveState=' + ('active' if active else 'inactive') + '\n')
    manager.runner = runner
    manager.preflight = lambda *_: None
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'started' and not report['dataVerified']
    launch = next(command for command in calls if command[0] == 'systemd-run')
    assert '--unit=robot-device-mid360.service' in launch
    assert '--collect' in launch and '--property=Restart=no' in launch
    assert str(manager.workspace / 'run_fastlio.sh') in launch
    shell = launch[launch.index('-c') + 1]
    assert 'ROS_DOMAIN_ID=0' in shell and 'ROS_LOCALHOST_ONLY=0' in shell
    assert 'unset ROS_DISCOVERY_SERVER' in shell


def test_start_failure_does_not_kill_or_stop_anything(manager):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == 'systemd-run':
            return completed(code=1)
        return completed('LoadState=not-found\nActiveState=inactive\n')
    manager.runner = runner
    manager.preflight = lambda *_: None
    report = manager.source(sources.SOURCES['mid360'])
    assert report['state'] == 'failed'
    assert not any('stop' in command or 'kill' in command for command in calls)


def fake_environment(manager, tmp_path):
    manager.ros_setup = tmp_path / 'ros-setup.bash'
    for path in (manager.ros_setup, manager.workspace / 'install/setup.bash',
                 manager.workspace / 'env.sh', manager.workspace / 'run_ldrobot_stl19p.sh'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# isolated environment\n')
    manager.package = lambda *_: tmp_path


def test_stl_checks_port_occupancy_without_any_kill_option(manager, tmp_path):
    fake_environment(manager, tmp_path)
    manager.stl_port = tmp_path / 'fixture-port'
    manager.stl_port.write_text('')
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('123', code=0)
    manager.runner = runner
    with pytest.raises(sources.SourceError, match='占用'):
        manager.preflight(sources.SOURCES['stl19p'])
    assert calls == [['fuser', '--', str(manager.stl_port)]]


def test_stl_missing_port_fails_without_commands(manager, tmp_path):
    fake_environment(manager, tmp_path)
    manager.stl_port = tmp_path / 'missing'
    manager.runner = lambda *_args, **_kwargs: pytest.fail('missing hardware must not be started')
    with pytest.raises(sources.SourceError, match='串口缺失'):
        manager.preflight(sources.SOURCES['stl19p'])


def test_missing_ros_package_is_reported_without_launch(manager):
    manager.runner = lambda *_args, **_kwargs: completed(code=1)
    with pytest.raises(sources.SourceError, match='未安装 ROS 包：bluesea2'):
        manager.package('bluesea2', 'bluesea2_node')


def test_berxel_requires_actual_sdk_and_usb_identity(manager, tmp_path):
    fake_environment(manager, tmp_path)
    camera_profile(manager)
    manager.berxel_sdk = tmp_path / 'berxel-sdk'
    manager.berxel_sdk.mkdir()
    manager.usb_root = tmp_path / 'usb'
    device = manager.usb_root / '1-1'
    device.mkdir(parents=True)
    (device / 'idVendor').write_text('0bda\n')
    (device / 'idProduct').write_text('5187\n')
    with pytest.raises(sources.SourceError, match='0603:0009'):
        manager.preflight(sources.SOURCES['berxel'])
    (device / 'idVendor').write_text('0603\n')
    (device / 'idProduct').write_text('0009\n')
    manager.preflight(sources.SOURCES['berxel'])


def test_bluesea_private_copy_changes_only_scan_topic_and_never_source(manager):
    original = 'bluesea_node:\n  ros__parameters:\n    scan_topic: "/scan"\n    lidar_port: 6543\n'
    manager.private_params(original)
    assert manager.params.read_text() == original.replace('"/scan"', '"/bluesea_new/scan"')
    assert manager.params.stat().st_mode & 0o777 == 0o600
    assert manager.params.parent.stat().st_mode & 0o777 == 0o700
    assert original.endswith('    lidar_port: 6543\n')
    assert not list(manager.params.parent.glob('*.tmp'))


@pytest.mark.parametrize('malformed', ['', 'scan_topic: /scan\n',
    '  scan_topic: /scan\n  scan_topic: /other\n'])
def test_bluesea_ambiguous_parameter_copy_fails(malformed):
    with pytest.raises(sources.SourceError):
        sources.bluesea_parameters(malformed)


def test_bluesea_copy_refuses_link_target_and_keeps_outside_bytes(manager):
    manager.params.parent.mkdir(parents=True, mode=0o700)
    outside = manager.home / 'outside'
    outside.write_text('unchanged')
    manager.params.symlink_to(outside)
    with pytest.raises(sources.SourceError):
        manager.private_params('node:\n  scan_topic: "/scan"\n')
    assert outside.read_text() == 'unchanged'


class FakeOwner:
    SHARED_CONFIG = '/fixed/config.json'
    SHARED_UNIT = 'robot-cloud-shared-camera-preview.service'
    CAPTURE_UNITS = (SHARED_UNIT, 'other-preview.service')
    Unit = staticmethod(lambda name: SimpleNamespace(active=False))

    def __init__(self, *, active=False, healthy=False, health_error=False):
        self.commands, self.lock_count, self.validate_count, self.wait_count = [], 0, 0, 0
        self.current = SimpleNamespace(units={self.SHARED_UNIT: SimpleNamespace(active=active)},
                                       healthy=healthy)
        self.health_error = health_error
        self.settings = SimpleNamespace(capture_config=self.SHARED_CONFIG)
        self.preflight_error = False

    def SystemOps(self):
        return self

    def Engine(self, ops):
        return self

    def preflight(self, current, web):
        assert web
        if self.preflight_error:
            raise RuntimeError('unknown device holder')

    def snapshot(self):
        return self.current

    @contextmanager
    def lock(self):
        self.lock_count += 1
        yield

    def validate_start_target(self):
        self.validate_count += 1

    def command(self, command):
        self.commands.append(command)
        return completed()

    def wait_healthy(self):
        self.wait_count += 1
        if self.health_error:
            raise RuntimeError('no valid source frames')
        return SimpleNamespace(units={}, healthy=True)


def test_camera_reuses_active_healthy_owner_without_stop_or_start(manager):
    owner = FakeOwner(active=True, healthy=True)
    manager.owner_module = owner
    result = manager.camera()
    assert result['state'] == 'reused' and result['dataVerified']
    assert owner.lock_count == 1 and owner.commands == []


def test_camera_active_unhealthy_owner_is_left_running(manager):
    owner = FakeOwner(active=True, healthy=False)
    manager.owner_module = owner
    result = manager.camera()
    assert result['state'] == 'failed' and owner.commands == []
    assert owner.validate_count == 0


def test_camera_new_start_uses_existing_shared_unit_and_never_switch_engine(manager):
    owner = FakeOwner()
    manager.owner_module = owner
    report = manager.camera()
    assert report['state'] == 'started'
    assert owner.validate_count == owner.wait_count == 1
    assert owner.commands == [['systemctl', '--user', 'start', owner.SHARED_UNIT]]


def test_camera_failed_health_does_not_stop_new_or_old_capture(manager):
    owner = FakeOwner(health_error=True)
    manager.owner_module = owner
    result = manager.camera()
    assert result['state'] == 'failed'
    assert owner.commands == [['systemctl', '--user', 'start', owner.SHARED_UNIT]]


def test_camera_status_is_read_only_without_lock_or_validation(manager):
    owner = FakeOwner(active=True, healthy=True)
    manager.owner_module = owner
    report = manager.camera(status=True)
    assert report['state'] == 'reused' and owner.lock_count == 0
    assert owner.commands == [] and owner.validate_count == 0


def test_camera_partial_healthy_status_is_explicit_and_read_only(manager):
    owner = FakeOwner(active=True, healthy=True)
    owner.current.available = ('camera1', 'camera2', 'camera4')
    owner.current.missing_cameras = ('camera3',)
    manager.owner_module = owner
    report = manager.camera(status=True)
    assert report['state'] == 'partial' and report['dataVerified']
    assert report['dataVerifiedFor'] == ['camera1', 'camera2', 'camera4']
    assert report['missingCameras'] == ['camera3']
    assert '未连接：右' in report['message'] and '关闭再重新开启' in report['message']
    assert owner.commands == [] and owner.lock_count == 0


@pytest.mark.parametrize('camera_id,direction', [
    ('camera1', '前'), ('camera2', '后'), ('camera3', '右'), ('camera4', '左')])
def test_partial_camera_direction_follows_canonical_camera_identity(manager, camera_id, direction):
    config = json.loads((Path(__file__).resolve().parents[1] /
                         'deploy/cameras/jetson-current.json').read_text())
    configured = next(camera for camera in config['cameras'] if camera['id'] == camera_id)
    assert configured['label'] == direction
    assert configured['direction'] == {'前': 'front', '后': 'rear', '右': 'right', '左': 'left'}[direction]
    owner = FakeOwner(active=True, healthy=True)
    owner.current.available = tuple(key for key in ('camera1', 'camera2', 'camera3', 'camera4')
                                    if key != camera_id)
    owner.current.missing_cameras = (camera_id,)
    manager.owner_module = owner
    report = manager.camera(status=True)
    assert report['missingCameras'] == [camera_id]
    assert ('未连接：' + direction) in report['message']
    assert owner.commands == [] and owner.lock_count == 0


def test_camera_reconnected_placeholder_requires_explicit_restart_without_stopping_live_capture(manager):
    owner = FakeOwner(active=True, healthy=False)
    owner.current.reconnect_required = ('camera3',)
    manager.owner_module = owner
    report = manager.camera()
    assert report['state'] == 'failed' and '相机已重新接入' in report['error']
    assert owner.commands == [] and owner.validate_count == 0


@pytest.mark.parametrize('key,partial_ok', [('usb-cameras', True), ('berxel', False)])
def test_execute_accepts_only_verified_partial_usb_result(manager, key, partial_ok):
    manager.role = 'jetson'
    manager.support = lambda name, status: dict(key=name, state='reused')
    partial = dict(key=key, state='partial', dataVerified=True,
                   dataVerifiedFor=['camera1', 'camera2', 'camera4'], missingCameras=['camera3'])
    manager.source = lambda source, status: partial if key == 'berxel' else dict(key='berxel', state='reused')
    manager.berxel_preview = lambda status: dict(key='berxel-preview', state='reused')
    manager.camera = lambda status: partial if key == 'usb-cameras' else dict(key='usb-cameras', state='reused')
    manager.berxel_frames = lambda: dict(key='berxel-frames', state='reused', dataVerified=True)
    report = manager.execute(status=True)
    assert report['ok'] is partial_ok


def test_camera_unknown_holder_blocks_start(manager):
    owner = FakeOwner()
    owner.preflight_error = True
    manager.owner_module = owner
    assert manager.camera()['state'] == 'failed'
    assert owner.commands == []


def recording_camera_manager(manager, monkeypatch, *, active=True, available=('camera1', 'camera2')):
    import json
    from datetime import datetime, timezone
    obj = sources.RecordingDeviceSources('jetson', home=manager.home, proc_root=manager.proc_root,
        sleeper=lambda _: None, runner=manager.runner)
    owner = FakeOwner(active=active, healthy=active)
    owner.current.available = available
    if active:
        owner.current.missing_cameras = ('camera3', 'camera4')
    obj.owner_module = owner
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    path = obj.home / '.local/state/robot-cloud/garage-control/camera-status.json'
    path.parent.mkdir(parents=True)
    report = {'updatedAt': datetime.fromtimestamp(1000, timezone.utc).isoformat(),
        'store': {'enabled': False, 'active': {}},
        'sink': {'enabled': False, 'writerRunning': True, 'errors': {}}}
    path.write_text(json.dumps(report))
    metadata = {'cameras': [{'id': identity, 'archive_tap': {
        'submitted': 0, 'rejected': 30, 'sampled': 0, 'error': ''}} for identity in available]}
    obj.camera_archive_http_status = lambda: metadata
    return obj, owner, path, report, metadata


def test_recording_camera_requires_attached_tap_and_live_writer_before_gate(manager, monkeypatch):
    obj, owner, path, report, metadata = recording_camera_manager(manager, monkeypatch)
    result = obj.camera(status=True)
    assert result['state'] == 'partial' and result['archiveReady'] is True
    assert result['archiveCameraIds'] == ['camera1', 'camera2']
    assert result['dataVerified'] is True
    assert not owner.commands and owner.lock_count == 0
    assert report['store']['enabled'] is False  # no recording toggle needed


@pytest.mark.parametrize('failure', ['stale', 'future', 'dead_writer', 'writer_error',
    'missing_tap', 'tap_error', 'bad_counter', 'missing_status', 'symlink_status', 'resource_pending'])
def test_recording_camera_reports_preview_without_recording_instead_of_success(
        manager, monkeypatch, failure):
    import json
    from datetime import datetime, timezone
    obj, owner, path, report, metadata = recording_camera_manager(manager, monkeypatch)
    if failure in ('stale', 'future'):
        report['updatedAt'] = datetime.fromtimestamp(980 if failure == 'stale' else 1003, timezone.utc).isoformat()
    elif failure == 'dead_writer': report['sink']['writerRunning'] = False
    elif failure == 'writer_error': report['sink']['errors'] = {'writer': 'OSError'}
    elif failure == 'missing_tap': metadata['cameras'][0].pop('archive_tap')
    elif failure == 'tap_error': metadata['cameras'][0]['archive_tap']['error'] = 'RuntimeError'
    elif failure == 'bad_counter': metadata['cameras'][0]['archive_tap']['submitted'] = True
    elif failure == 'resource_pending': report['store']['resourceMonitor'] = {'ready': False, 'state': 'PENDING'}
    path.write_text(json.dumps(report))
    if failure == 'missing_status': path.unlink()
    elif failure == 'symlink_status':
        saved = path.with_suffix('.saved'); path.rename(saved); path.symlink_to(saved)
    result = obj.camera(status=True)
    assert result['state'] == 'failed' and result['archiveReady'] is False
    assert result['dataVerified'] is True  # source receipt remains separately true
    assert result['errorCode'] == 'CAMERA_ARCHIVE_NOT_READY'
    assert not owner.commands and owner.lock_count == 0


def test_recording_camera_rechecks_startup_status_without_restarting_live_owner(manager, monkeypatch):
    obj, owner, path, report, metadata = recording_camera_manager(manager, monkeypatch)
    calls = []
    real = obj.camera_archive_ready
    def ready(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3: raise sources.SourceError('first status not yet published')
        return real(*args, **kwargs)
    obj.camera_archive_ready = ready
    result = obj.camera()
    assert result['archiveReady'] is True and len(calls) == 3
    assert not owner.commands


def test_recording_tap_probe_reads_original_process_not_filtered_gateway(manager, monkeypatch):
    calls = []
    class Response:
        status = 200
        def getheader(self, name, default=None): return 'application/json' if name == 'Content-Type' else default
        def read(self, size):
            calls.append(('read', size)); return b'{"cameras":[]}'
    class Connection:
        def __init__(self, host, port, timeout): calls.append(('origin', host, port, timeout))
        def request(self, method, path, headers): calls.append(('request', method, path))
        def getresponse(self): return Response()
        def close(self): calls.append(('close',))
    monkeypatch.setattr(sources.http.client, 'HTTPConnection', Connection)
    obj = sources.RecordingDeviceSources('jetson', home=manager.home)
    assert obj.camera_archive_http_status() == {'cameras': []}
    assert calls[0][:3] == ('origin', '127.0.0.1', 8080)
    assert ('request', 'GET', '/status') in calls and ('read', 262145) in calls
    assert calls[-1] == ('close',)


def test_no_camera_source_skips_all_device_access_but_requires_support(manager):
    manager.role = 'jetson'
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('LoadState=loaded\nActiveState=active\n')
    manager.runner = runner
    manager.camera = lambda *_: pytest.fail('camera sources explicitly disabled')
    manager.source = lambda *_: pytest.fail('camera sources explicitly disabled')
    report = manager.execute(no_camera_source=True)
    assert report['ok']
    assert len(calls) == len(sources.JETSON_SUPPORT)
    assert [row['state'] for row in report['devices']][-2:] == ['skipped', 'skipped']


def test_missing_deployed_support_is_a_failure_not_success(manager):
    report = manager.support(sources.JETSON_SUPPORT[0])
    assert report['state'] == 'failed' and '缺少已部署服务' in report['error']


def test_stop_dry_run_is_inert_for_both_roles(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('stop dry-run must not inspect hardware or run commands')
    for role in ('ipc', 'jetson'):
        obj = sources.DeviceSources(role, home=tmp_path / 'absent', runner=forbidden)
        obj.load_camera_owner = forbidden
        report = obj.execute(stop=True, dry_run=True)
        assert report['ok'] and report['stop'] and report['dryRun']
        assert all(row['state'] == 'planned' for row in report['devices'])
        assert not (tmp_path / 'absent').exists()


def test_stop_source_inactive_unit_with_manual_driver_keeps_process(manager):
    process(manager.proc_root, 12, ['/ws/ldlidar_stl_ros2_node'])
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('LoadState=not-found\nActiveState=inactive\n')
    manager.runner = runner
    report = manager.stop_source(sources.SOURCES['stl19p'])
    assert report['state'] == 'external_running'
    assert (manager.proc_root / '12/cmdline').exists()
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_stop_source_uses_exact_unit_and_verifies_exit(manager):
    calls, active = [], True
    def runner(command, **kwargs):
        nonlocal active
        calls.append((command, kwargs))
        if command[:3] == ['systemctl', '--user', 'stop']:
            assert command[3] == sources.SOURCES['stl19p'].unit
            assert kwargs['timeout'] == 45
            active = False
            return completed()
        return completed('LoadState=loaded\nActiveState=' + ('active' if active else 'inactive') + '\nResult=success\n')
    manager.runner = runner
    first = manager.stop_source(sources.SOURCES['stl19p'])
    second = manager.stop_source(sources.SOURCES['stl19p'])
    assert first['state'] == 'stopped'
    assert second['state'] == 'already_stopped'
    assert sum(command[:3] == ['systemctl', '--user', 'stop'] for command, _ in calls) == 1


def test_stop_source_reports_failed_stop_without_claiming_stopped(manager):
    def runner(command, **kwargs):
        if command[:3] == ['systemctl', '--user', 'stop']:
            return completed(code=1)
        return completed('LoadState=loaded\nActiveState=active\n')
    manager.runner = runner
    assert manager.stop_source(sources.SOURCES['mid360'])['state'] == 'failed'


def test_stop_source_reports_timeout_exit_as_failure(manager):
    active = True
    def runner(command, **kwargs):
        nonlocal active
        if command[:3] == ['systemctl', '--user', 'stop']:
            active = False
            return completed()
        return completed('LoadState=loaded\nActiveState=' + ('active' if active else 'failed') + '\nResult=timeout\n')
    manager.runner = runner
    report = manager.stop_source(sources.SOURCES['berxel'])
    assert report['state'] == 'failed' and '退出异常' in report['error']


def archive_status(manager, *, timestamp=995, queue=0, active=None):
    import json
    from datetime import datetime, timezone
    path = manager.home / '.local/state/robot-cloud/garage-control/status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'updatedAt': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        'ros': {'queueMessages': queue, 'queueBytes': 0}, 'store': {'active': {} if active is None else active}}))
    return path


def test_archive_drain_requires_fresh_empty_queue_and_finalized_segments(manager, monkeypatch):
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    archive_status(manager)
    assert manager.drain_archive(after=990, attempts=1)


@pytest.mark.parametrize('pending,running', [(1, False), (0, True), (False, False), (-1, False)])
def test_archive_drain_does_not_stop_before_video_finalization(manager, monkeypatch, pending, running):
    import json
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    path = archive_status(manager)
    report = json.loads(path.read_text())
    report['store'].update(videoPendingSegments=pending, videoFinalizing=running)
    path.write_text(json.dumps(report))
    assert not manager.drain_archive(after=990, attempts=1)


@pytest.mark.parametrize('timestamp,queue,active', [
    (980, 0, {}), (995, 1, {}), (995, False, {}), (995, 0, {'cam': {}}),
    (1005, 0, {}), (990, 0, {}),
])
def test_archive_drain_rejects_stale_busy_active_or_untrusted_status(manager, monkeypatch, timestamp, queue, active):
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    archive_status(manager, timestamp=timestamp, queue=queue, active=active)
    assert not manager.drain_archive(after=990, attempts=1)


def test_archive_timeout_keeps_recorder_running(manager):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return completed('LoadState=loaded\nActiveState=active\n')
    manager.runner = runner
    manager.drain_archive = lambda **kwargs: False
    report = manager.stop_support('robot-native-archive.service', after=1)
    assert report['state'] == 'failed'
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


class StopOwner(FakeOwner):
    def __init__(self, *, active=True, verified=True, external=False,
                 native_tap=False, environment='', environment_code=0, exit_result='success', load_state='loaded'):
        super().__init__(active=active)
        self.native_tap, self.exit_result = native_tap, exit_result
        self.environment, self.environment_code = environment, environment_code
        self.current = SimpleNamespace(units={
            self.SHARED_UNIT: SimpleNamespace(active=active, verified=verified, load_state=load_state),
            'other-preview.service': SimpleNamespace(active=external, verified=True),
        }, healthy=False, scan_errors=[], holders=())

    def command(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ['systemctl', '--user', 'stop']:
            assert command[3] == self.SHARED_UNIT
            assert kwargs['timeout'] == 105
            self.current.units[self.SHARED_UNIT].active = False
            return completed()
        assert command[:3] == ['systemctl', '--user', 'show']
        if 'ExecStart' in command:
            return completed('/usr/bin/python3 camera_native_web.py' +
                (' --archive-config /private/archive.json' if self.native_tap else ''))
        if 'Environment' in command:
            return completed(self.environment, code=self.environment_code)
        return completed(self.exit_result + '\n')


def test_stop_camera_only_stops_verified_shared_unit_under_existing_lock(manager):
    owner = StopOwner()
    manager.owner_module = owner
    report = manager.stop_camera()
    assert report['state'] == 'stopped' and owner.lock_count == 1
    assert ['systemctl', '--user', 'stop', owner.SHARED_UNIT] in owner.commands
    assert not any('other-preview.service' in command for command in owner.commands)


def test_stop_camera_preserves_other_capture_owner_and_reports_it(manager):
    owner = StopOwner(active=False, external=True)
    manager.owner_module = owner
    report = manager.stop_camera()
    assert report['state'] == 'external_running'
    assert owner.commands == [['systemctl', '--user', 'show', owner.SHARED_UNIT, '-p', 'Result', '--value']]
    assert owner.current.units['other-preview.service'].active


def test_stop_camera_refuses_unverified_shared_unit(manager):
    owner = StopOwner(verified=False)
    manager.owner_module = owner
    assert manager.stop_camera()['state'] == 'failed'
    assert owner.commands == []


def test_jetson_stop_orders_sources_then_archive_then_helpers(manager):
    manager.role = 'jetson'
    calls = []
    manager.stop_berxel_preview = lambda: calls.append('preview') or dict(key='berxel-preview', state='stopped')
    manager.stop_camera = lambda: calls.append('usb') or dict(key='usb-cameras', state='stopped')
    manager.stop_source = lambda source: calls.append(source.key) or dict(key=source.key, state='stopped')
    manager.stop_support = lambda name, **kwargs: calls.append(name) or dict(key=name, state='stopped')
    report = manager.execute(stop=True)
    assert report['ok']
    assert calls == ['preview', 'usb', 'berxel', *sources.JETSON_SUPPORT, sources.YOLO_UNIT]


def test_jetson_source_failure_keeps_all_support_services(manager):
    manager.role = 'jetson'
    manager.stop_berxel_preview = lambda: dict(key='berxel-preview', state='stopped')
    manager.stop_camera = lambda: dict(key='usb-cameras', state='external_running')
    manager.stop_source = lambda source: dict(key=source.key, state='stopped')
    manager.stop_support = lambda *_args, **_kwargs: pytest.fail('support must remain running')
    report = manager.execute(stop=True)
    assert not report['ok']
    assert any(row['key'] == 'robot-native-archive' and row['state'] == 'failed' for row in report['devices'])


def test_jetson_archive_failure_keeps_gateway_and_relay(manager):
    manager.role = 'jetson'
    manager.stop_berxel_preview = lambda: dict(key='berxel-preview', state='stopped')
    manager.stop_camera = lambda: dict(key='usb-cameras', state='stopped')
    manager.stop_source = lambda source: dict(key=source.key, state='stopped')
    calls = []
    def stop(name, **kwargs):
        calls.append(name)
        return dict(key=name, state='failed')
    manager.stop_support = stop
    report = manager.execute(stop=True)
    assert not report['ok']
    assert calls == ['robot-native-archive.service']


def test_preview_close_failure_cannot_claim_all_stopped_or_stop_archive(manager):
    manager.role = 'jetson'
    manager.stop_berxel_preview = lambda: dict(key='berxel-preview', state='failed', error='unknown unit identity')
    manager.stop_camera = lambda: dict(key='usb-cameras', state='stopped')
    manager.stop_source = lambda source: dict(key=source.key, state='stopped')
    manager.stop_support = lambda *args, **kwargs: pytest.fail('archive helpers must stay running')
    report = manager.execute(stop=True)
    assert not report['ok']
    assert any(row['key'] == 'robot-native-archive' and row['state'] == 'failed' for row in report['devices'])


@pytest.mark.parametrize('dry_run', [False, True])
def test_no_camera_stop_preserves_camera_and_support_without_reads(manager, dry_run):
    manager.role = 'jetson'
    manager.load_camera_owner = lambda: pytest.fail('excluded camera must not be inspected')
    manager.runner = lambda *_args, **_kwargs: pytest.fail('excluded support must not be inspected')
    report = manager.execute(stop=True, dry_run=dry_run, no_camera_source=True)
    assert report['ok'] and report['dryRun'] == dry_run
    assert all(row['state'] == 'skipped' for row in report['devices'])


@pytest.mark.parametrize('result', ['timeout', 'signal', 'exit-code'])
def test_camera_unclean_service_result_never_claims_stopped(manager, result):
    manager.owner_module = StopOwner(exit_result=result)
    report = manager.stop_camera()
    assert report['state'] == 'failed' and '退出异常' in report['error']


def camera_status(manager, *, timestamp=995, queue=0, active=None, writer=False):
    import json
    from datetime import datetime, timezone
    path = manager.home / '.local/state/robot-cloud/garage-control/camera-status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'updatedAt': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        'sink': {'queueFrames': queue, 'queueBytes': 0, 'writerRunning': writer},
        'store': {'active': {} if active is None else active}}))
    return path


@pytest.mark.parametrize('timestamp,queue,active,writer,expected', [
    (995, 0, {}, False, True), (980, 0, {}, False, False),
    (995, 1, {}, False, False), (995, 0, {'camera1': {}}, False, False),
    (995, 0, {}, True, False), (995, False, {}, False, False),
])
def test_camera_final_status_confirms_writer_and_segments_closed(
        manager, monkeypatch, timestamp, queue, active, writer, expected):
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    camera_status(manager, timestamp=timestamp, queue=queue, active=active, writer=writer)
    assert manager.camera_archive_finalized(after=990) is expected


def test_native_tap_missing_final_status_is_failure(manager):
    manager.owner_module = StopOwner(native_tap=True)
    report = manager.stop_camera()
    assert report['state'] == 'failed' and '收尾未获确认' in report['error']


def test_native_tap_checks_final_status_after_shared_service_stop(manager):
    owner = StopOwner(native_tap=True)
    manager.owner_module = owner
    def finalized(**kwargs):
        assert not owner.current.units[owner.SHARED_UNIT].active
        assert kwargs['after'] > 0
        return True
    manager.camera_archive_finalized = finalized
    assert manager.stop_camera()['state'] == 'stopped'


@pytest.mark.parametrize('environment,enabled', [
    ('DISPLAY=:0 NATIVE_CAMERA_ARCHIVE_CONFIG=/private/archive.json', True),
    ('"NATIVE_CAMERA_ARCHIVE_CONFIG=/private/archive with spaces.json" DISPLAY=:0', True),
    ('NATIVE_CAMERA_ARCHIVE_CONFIG=', False),
    ('OTHER_NATIVE_CAMERA_ARCHIVE_CONFIG=/private/archive.json', False),
])
def test_environment_enabled_native_tap_requires_confirmed_archive_close(manager, environment, enabled):
    owner = StopOwner(environment=environment);manager.owner_module = owner
    calls = []
    manager.camera_archive_finalized = lambda **kwargs: calls.append(kwargs) or False
    result = manager.stop_camera()
    assert (result['state'] == 'failed') is enabled
    assert len(calls) == int(enabled)
    assert not owner.current.units[owner.SHARED_UNIT].active


@pytest.mark.parametrize('environment,code', [('NATIVE_CAMERA_ARCHIVE_CONFIG="unterminated', 0), ('', 1)])
def test_unreadable_or_invalid_unit_environment_keeps_capture_running(manager, environment, code):
    owner = StopOwner(environment=environment, environment_code=code);manager.owner_module = owner
    assert manager.stop_camera()['state'] == 'failed'
    assert owner.current.units[owner.SHARED_UNIT].active
    assert not any(command[:3] == ['systemctl', '--user', 'stop'] for command in owner.commands)


def test_environment_tap_checks_final_status_only_after_shared_unit_is_stopped(manager):
    owner = StopOwner(environment='NATIVE_CAMERA_ARCHIVE_CONFIG=/private/archive.json')
    manager.owner_module = owner
    def finalized(**kwargs):
        assert not owner.current.units[owner.SHARED_UNIT].active
        assert kwargs['after'] > 0
        return True
    manager.camera_archive_finalized = finalized
    assert manager.stop_camera()['state'] == 'stopped'


def test_repeated_stop_cannot_hide_previous_archive_exit_failure(manager):
    owner = StopOwner(exit_result='exit-code');manager.owner_module = owner
    assert manager.stop_camera()['state'] == 'failed'
    assert not owner.current.units[owner.SHARED_UNIT].active
    commands_before = len(owner.commands)
    assert manager.stop_camera()['state'] == 'failed'
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in owner.commands[commands_before:])


def test_never_loaded_camera_unit_is_already_stopped_without_commands(manager):
    owner = StopOwner(active=False, load_state='not-found');manager.owner_module = owner
    assert manager.stop_camera()['state'] == 'already_stopped'
    assert owner.commands == []


@pytest.mark.parametrize('kind,section,field,value', [
    ('ros', 'ros', 'tickError', 'OSError'),
    ('ros', 'ros', 'gateError', 'PermissionError'),
    ('ros', 'ros', 'tickError', False),
    ('ros', 'ros', 'queueBytes', False),
    ('ros', 'ros', 'inFlightMessages', 1),
    ('ros', 'ros', 'inFlightMessages', False),
    ('ros', 'store', 'errors', {'lidar': 'finalize failed'}),
    ('ros', 'store', 'errors', {'storage': '', 'sourceTransfer': 'offline'}),
    ('ros', 'store', 'errors', []),
    ('camera', 'sink', 'errors', {'writer': 'OSError'}),
    ('camera', 'sink', 'errors', []),
    ('camera', 'sink', 'queueBytes', False),
    ('camera', 'store', 'errors', {'camera1': 'finalize failed'}),
    ('camera', 'store', 'errors', None),
])
def test_finalization_rejects_disk_errors_and_malformed_fields(
        manager, monkeypatch, kind, section, field, value):
    import json
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    path = archive_status(manager) if kind == 'ros' else camera_status(manager)
    report = json.loads(path.read_text())
    report[section][field] = value
    path.write_text(json.dumps(report))
    if kind == 'ros':
        assert not manager.drain_archive(after=990, attempts=1)
    else:
        assert not manager.camera_archive_finalized(after=990)


@pytest.mark.parametrize('kind', ['ros', 'camera'])
def test_finalization_ignores_pending_network_transfer_but_requires_timezone(manager, monkeypatch, kind):
    import json
    monkeypatch.setattr(sources.time, 'time', lambda: 1000)
    path = archive_status(manager) if kind == 'ros' else camera_status(manager)
    report = json.loads(path.read_text())
    report['store']['errors'] = {'sourceTransfer': 'hotspot offline'}
    path.write_text(json.dumps(report))
    check = ((lambda: manager.drain_archive(after=990, attempts=1)) if kind == 'ros'
             else (lambda: manager.camera_archive_finalized(after=990)))
    assert check()
    report['updatedAt'] = report['updatedAt'].removesuffix('+00:00')
    path.write_text(json.dumps(report))
    assert not check()


def test_native_sink_normal_close_reports_writer_stopped_without_hardware():
    from collector.archive_camera import NativeCameraSink
    class MemoryStore:
        camera_names = {}
        def enabled(self):
            return True
        def tick(self):
            pass
        def close(self):
            self.closed = True
        def note_loss(self, *_):
            pass
    store = MemoryStore()
    sink = NativeCameraSink(store)
    sink.start()
    try:
        assert sink.close(timeout=1)
        report = sink.snapshot()
        assert report['writerRunning'] is False and store.closed
        assert report['queueFrames'] == report['queueBytes'] == 0
        assert report['errors'] == {}
    finally:
        sink.close(timeout=1)


@pytest.mark.parametrize('status', [False, True])
def test_camera_only_sources_skip_archive_and_check_yolo_after_both_captures(manager, status):
    manager.role = 'jetson'
    calls = []
    def support(name, check_status):
        assert check_status is status
        calls.append(name)
        return dict(key=name.removesuffix('.service'), state='reused')
    manager.support = support
    manager.source = lambda source, check_status: calls.append(source.key) or dict(key=source.key, state='reused')
    manager.berxel_preview = lambda check_status: calls.append('preview') or dict(key='berxel-preview', state='reused')
    manager.camera = lambda check_status: calls.append('usb') or dict(key='usb-cameras', state='reused')
    manager.berxel_frames = lambda: calls.append('frames') or dict(key='berxel-frames', state='reused', dataVerified=True)
    report = manager.execute(status=status, cameras_only=True)
    assert report['ok']
    assert calls == [*sources.CAMERA_SUPPORT, 'berxel', 'preview', 'usb', sources.YOLO_UNIT, 'frames']
    assert 'robot-native-archive.service' not in calls
    assert not any(key in str(report) for key in ('mid360', 'stl19p', 'bluesea'))


def test_camera_only_dry_run_plans_no_archive_or_radar_and_never_reads_hardware(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run must not run commands or inspect hardware')
    obj = sources.DeviceSources('jetson', home=tmp_path / 'absent', runner=forbidden)
    obj.load_camera_owner = forbidden
    report = obj.execute(dry_run=True, cameras_only=True)
    assert report['ok'] and report['dryRun']
    keys = [row['key'] for row in report['devices']]
    assert keys == [*sources.CAMERA_SUPPORT, 'berxel', 'berxel-preview', 'usb-cameras',
                    'robot-cloud-yolo-observer', 'berxel-frames']
    assert 'robot-native-archive' not in str(report)
    assert not (tmp_path / 'absent').exists()


def test_camera_only_stop_preserves_archive_even_when_enabled(manager):
    manager.role = 'jetson'
    calls = []
    manager.stop_berxel_preview = lambda: calls.append('preview') or dict(key='berxel-preview', state='stopped')
    manager.stop_camera = lambda: calls.append('usb') or dict(key='usb-cameras', state='stopped')
    manager.stop_source = lambda source: calls.append(source.key) or dict(key=source.key, state='stopped')
    manager.stop_support = lambda name, **kwargs: calls.append(name) or dict(key=name, state='stopped')
    manager.drain_archive = lambda **kwargs: pytest.fail('camera-only must not manipulate shared archive')
    report = manager.execute(stop=True, cameras_only=True)
    assert report['ok']
    assert calls == ['preview', 'usb', 'berxel', *sources.CAMERA_SUPPORT, sources.YOLO_UNIT]


def test_camera_only_status_reads_existing_yolo_unit_without_starting(manager):
    manager.role = 'jetson'
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return completed('LoadState=loaded\nActiveState=inactive\n')
    manager.runner = run
    manager.source = lambda source, status: dict(key=source.key, state='inactive')
    manager.berxel_preview = lambda status: dict(key='berxel-preview', state='inactive')
    manager.camera = lambda status: dict(key='usb-cameras', state='inactive')
    report = manager.execute(status=True, cameras_only=True)
    assert not report['ok']
    assert [command[3] for command in calls] == [*sources.CAMERA_SUPPORT, sources.YOLO_UNIT]
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in calls)


def test_camera_only_rejects_ipc_role_before_hardware_access(manager):
    manager.runner = lambda *args, **kwargs: pytest.fail('must reject before commands')
    with pytest.raises(sources.SourceError, match='Jetson'):
        manager.execute(cameras_only=True)


def test_yolo_stop_uses_exact_managed_unit_and_rejects_unknown_units(manager):
    calls = []
    active = [True]

    def runner(command, **kwargs):
        calls.append(command)
        if command[:3] == ['systemctl', '--user', 'show']:
            assert command[3] == sources.YOLO_UNIT
            return completed('LoadState=loaded\nActiveState=' + ('active' if active[0] else 'inactive') + '\nResult=success\n')
        assert command == ['systemctl', '--user', 'stop', sources.YOLO_UNIT]
        active[0] = False
        return completed()

    manager.runner = runner
    report = manager.stop_support(sources.YOLO_UNIT)
    assert report['state'] == 'stopped' and not active[0]
    assert [command[2] for command in calls] == ['show', 'stop', 'show']
    count = len(calls)
    assert manager.stop_support('unrelated.service')['state'] == 'failed'
    assert len(calls) == count


def verified_depth_frames():
    return dict(ok=True, dataVerified=True, state='ready', sampleWindowSeconds=3.0, reports=4,
        channels={key: dict(receivedFrames=120, publishedFrames=20, receivedDelta=90,
            publishedDelta=15, sourceAgeS=.1, previewAgeS=.2) for key in ('color', 'depth')})


def test_depth_frame_check_uses_only_a_bounded_read_only_status_probe(manager):
    import json
    calls = []
    manager.preview_preflight = lambda: None
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(json.dumps(verified_depth_frames()))
    manager.runner = run
    result = manager.berxel_frames()
    assert result['state'] == 'reused' and result['dataVerified']
    assert result['channels']['depth']['receivedDelta'] == 90
    assert len(calls) == 1 and calls[0][1]['timeout'] == 20
    command = calls[0][0]
    assert command[-6:] == ['/usr/bin/python3', '-B', str(manager.preview_script),
        '--health-probe', '--health-timeout', '15']
    assert not any(action in command for action in ('start', 'stop', 'restart', 'systemd-run'))


@pytest.mark.parametrize('issue', ['helper-failed', 'short-window', 'no-channel', 'counter-zero',
                                   'stale-source', 'not-verified', 'empty-output'])
def test_depth_frame_check_cannot_claim_ready_from_pid_or_invalid_helper_evidence(manager, issue):
    import json
    report, code = verified_depth_frames(), 0
    if issue == 'helper-failed':
        report = dict(ok=False, dataVerified=False, error='color 原始帧已过期')
        code = 1
    elif issue == 'short-window': report['sampleWindowSeconds'] = 1
    elif issue == 'no-channel': del report['channels']['depth']
    elif issue == 'counter-zero': report['channels']['depth']['publishedDelta'] = 0
    elif issue == 'stale-source': report['channels']['depth']['sourceAgeS'] = 2
    elif issue == 'not-verified': report['dataVerified'] = False
    calls = []
    manager.preview_preflight = lambda: None
    manager.runner = lambda command, **kwargs: calls.append(command) or completed(
        '' if issue == 'empty-output' else json.dumps(report), code)
    result = manager.berxel_frames()
    assert result['state'] == 'failed' and result['dataVerified'] is False
    assert len(calls) == 1
    assert not any(action in calls[0] for action in ('start', 'stop', 'restart', 'systemd-run'))


@pytest.mark.parametrize('status', [False, True])
def test_live_depth_processes_with_no_continuous_frames_fail_start_and_status_without_restarting(manager, status):
    manager.role = 'jetson'
    manager.support = lambda name, check_status: dict(key=name, state='reused')
    manager.source = lambda source, check_status: dict(key=source.key, state='reused', dataVerified=False)
    manager.berxel_preview = lambda check_status: dict(key='berxel-preview', state='reused', dataVerified=False)
    manager.camera = lambda check_status: dict(key='usb-cameras', state='reused')
    called = []
    manager.berxel_frames = lambda: called.append('probe') or dict(key='berxel-frames',
        state='failed', dataVerified=False, error='depth 原始帧已过期')
    report = manager.execute(status=status, cameras_only=True)
    assert not report['ok'] and called == ['probe']
    assert report['devices'][-1]['error'] == 'depth 原始帧已过期'


def test_inactive_depth_driver_or_preview_never_starts_probe_from_status(manager):
    manager.role = 'jetson'
    manager.support = lambda name, status: dict(key=name, state='reused')
    manager.source = lambda source, status: dict(key=source.key, state='inactive')
    manager.berxel_preview = lambda status: dict(key='berxel-preview', state='reused')
    manager.camera = lambda status: dict(key='usb-cameras', state='reused')
    manager.berxel_frames = lambda: pytest.fail('inactive driver cannot pass a true frame probe')
    report = manager.execute(status=True, cameras_only=True)
    assert not report['ok']
    assert report['devices'][-1]['key'] == 'berxel-frames'


def test_depth_probe_timeout_is_a_failure_and_never_restarts_any_service(manager):
    calls = []
    manager.preview_preflight = lambda: None
    def timeout(command, **kwargs):
        calls.append(command)
        raise sources.subprocess.TimeoutExpired(command, kwargs['timeout'])
    manager.runner = timeout
    result = manager.berxel_frames()
    assert result['state'] == 'failed' and result['dataVerified'] is False
    assert result['errorCode'] == 'BERXEL_FRAME_PROBE_TIMEOUT'
    assert result['timeoutSeconds'] == 20 and '真实帧检查超时' in result['error']
    assert 'ROS/DDS' in result['error']
    assert len(calls) == 1
    assert not any(action in calls[0] for action in ('start', 'stop', 'restart', 'systemd-run'))


def test_camera_transport_preflight_reads_existing_private_profile_without_writes(manager):
    path = camera_profile(manager)
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
    manager.camera_transport_preflight()
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before


def test_camera_transport_preflight_accepts_nonwritable_0755_directory(manager):
    path = camera_profile(manager)
    path.parent.chmod(0o755)
    manager.camera_transport_preflight()
    assert path.parent.stat().st_mode & 0o777 == 0o755


def test_camera_transport_preflight_missing_profile_never_creates_parent_directories(manager):
    parent = manager.camera_transport_profile.parent
    assert not parent.exists()
    with pytest.raises(sources.SourceError, match='DDS 配置'):
        manager.camera_transport_preflight()
    assert not parent.exists()


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o620, 0o602, 0o666])
def test_camera_transport_preflight_rejects_nonprivate_profile_without_repair(manager, mode):
    path = camera_profile(manager)
    path.chmod(mode)
    with pytest.raises(sources.SourceError, match='0600'):
        manager.camera_transport_preflight()
    assert path.stat().st_mode & 0o777 == mode


def test_camera_transport_preflight_rejects_profile_and_directory_symlinks(manager, tmp_path):
    path = camera_profile(manager)
    real = path.with_name('real-profile.xml')
    path.rename(real)
    path.symlink_to(real)
    with pytest.raises(sources.SourceError, match='非符号链接'):
        manager.camera_transport_preflight()
    path.unlink()
    real.rename(path)
    directory = path.parent
    moved = directory.with_name('real-camera-directory')
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(sources.SourceError, match='配置目录'):
        manager.camera_transport_preflight()


def test_camera_transport_preflight_rejects_foreign_owner_and_writable_directory(manager, monkeypatch):
    path = camera_profile(manager)
    real = sources.os.getuid()
    monkeypatch.setattr(sources.os, 'getuid', lambda: real + 100)
    with pytest.raises(sources.SourceError, match='当前用户'):
        manager.camera_transport_preflight()
    monkeypatch.setattr(sources.os, 'getuid', lambda: real)
    path.parent.chmod(0o770)
    with pytest.raises(sources.SourceError, match='不可被组'):
        manager.camera_transport_preflight()
    assert path.parent.stat().st_mode & 0o777 == 0o770
