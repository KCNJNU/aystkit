"""Behavioral startup checks. No test launches Docker, SSH, systemd or hardware."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/scripts/start_all_devices.py"


class FakeRunner:
    def __init__(self, module):
        self.module = module
        self.calls = []
        self.active = set()
        self.missing_units = set()
        self.start_failures = set()
        self.existing_services = list(module.WAREHOUSE_SERVICES)
        self.running_services = list(module.WAREHOUSE_SERVICES)
        self.ssh_returncode = 0
        self.local_returncode = 0
        self.local_stdout = '{"devices": []}\n'

    def run(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs))
        code, output = 0, ""
        if args[:2] == ["docker", "compose"]:
            if "start" in args:
                output = "Started\n"
            elif args[-5:] in (["exec", "-T", "web", "nginx", "-t"],
                               ["-T", "web", "nginx", "-s", "reload"]):
                output = "nginx: configuration syntax is ok\n"
            elif args[-3:] == ["ps", "--all", "--services"]:
                output = "\n".join(self.existing_services)
            elif args[-4:] == ["ps", "--status", "running", "--services"]:
                output = "\n".join(self.running_services)
            else:
                raise AssertionError(f"Unexpected Docker operation: {args}")
        elif args[:3] == ["systemctl", "--user", "show"]:
            unit = args[3]
            loaded = "not-found" if unit in self.missing_units else "loaded"
            state = "active" if unit in self.active else "inactive"
            output = f"LoadState={loaded}\nActiveState={state}\n"
        elif args[:3] in (["systemctl", "--user", "start"], ["systemctl", "--user", "restart"]):
            unit = args[3]
            if unit in self.start_failures:
                code = 1
            else:
                self.active.add(unit)
        elif args[:4] == ["systemctl", "--user", "is-active", "--quiet"]:
            code = 0 if args[4] in self.active else 3
        elif args[0] == sys.executable and any(Path(arg).name == "device_sources.py" for arg in args[1:]):
            code, output = self.local_returncode, self.local_stdout
        elif args[0] == "ssh":
            code = self.ssh_returncode
            output = '{"devices": []}\n' if not code else ""
        else:
            raise AssertionError(f"External command was not simulated: {args}")
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")

    def commands(self):
        return [args for args, _ in self.calls]

    def started_units(self):
        return {args[3] for args in self.commands() if args[:3] == ["systemctl", "--user", "start"]}


@pytest.fixture
def startup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("start_all_devices_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("START_ALL_READY_TIMEOUT", raising=False)
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    (warehouse / "docker-compose.yml").write_text("services: {}\n")
    (warehouse / ".env").write_text("# no credentials in fixture\n")
    monkeypatch.setenv("DBPROG_DIR", str(warehouse))
    config = tmp_path / ".config/robot-cloud"
    config.mkdir(parents=True)
    key, known = tmp_path / "fixture-key", tmp_path / "fixture-known-hosts"
    key.write_text("fixture only")
    known.write_text("fixture only")
    (config / "video-tunnel.env").write_text(
        f"VIDEO_SOURCE_HOST=auto\nVIDEO_SOURCE_NAME=test-jetson.local\n"
        f"VIDEO_SOURCE_KEY={key}\nVIDEO_SOURCE_KNOWN_HOSTS={known}\n"
    )
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    (script_dir / "device_sources.py").write_text("# simulated helper; never executed\n")
    monkeypatch.setattr(module, "SCRIPT_DIR", script_dir)
    monkeypatch.setattr(module, "http_ready", lambda url: True)
    fake = FakeRunner(module)
    monkeypatch.setattr(module, "Runner", lambda: fake)
    return module, fake, tmp_path


def test_dry_run_executes_no_commands_or_writes(startup, capsys):
    module, fake, home = startup
    assert module.main(["--all-devices", "--dry-run"]) == 0
    assert fake.calls == []
    assert not (home / ".local/state/robot-cloud").exists()
    output = capsys.readouterr().out
    assert "预演" in output
    assert all(service in output for service in module.WAREHOUSE_SERVICES)
    assert all(unit in output for unit in module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_status_is_read_only_and_passes_status_to_both_sensor_helpers(startup):
    module, fake, home = startup
    fake.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert module.main(["--all-devices", "--status"]) == 0
    commands = fake.commands()
    assert not any("start" in command for command in commands)
    assert not (home / ".local/state/robot-cloud").exists()
    local = next(command for command in commands if command[0] == sys.executable)
    assert local[-1] == "--status"
    ssh = next(command for command in commands if command[0] == "ssh")
    assert shlex.split(ssh[-1]) == ["python3", "-B", "-", "jetson", "--status"]


@pytest.mark.parametrize('status', [False, True])
def test_available_cameras_succeed_with_explicit_partial_summary(startup, capsys, status):
    module, fake, _ = startup
    fake.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    original = fake.run

    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == 'ssh':
            result.stdout = json.dumps({'ok': True, 'devices': [
                {'key': 'usb-cameras', 'state': 'partial',
                 'message': '前、后、右已运行；左相机未连接。'}]})
        return result

    fake.run = run
    assert module.main(['--all-devices', '--status'] if status else ['--all-devices']) == 0
    output = capsys.readouterr().out
    assert '左相机未连接' in output
    assert '仅部分可用' in output
    assert '不会产生采集文件' in output
    assert '状态检查通过。' not in output
    if status:
        assert not any('start' in command for command in fake.commands())


def test_start_uses_only_start_for_six_existing_warehouse_containers(startup):
    module, fake, _ = startup
    assert module.main(["--all-devices"]) == 0
    docker_commands = [command for command in fake.commands() if command[0] == "docker"]
    starts = [command for command in docker_commands if "start" in command]
    assert len(starts) == 1
    assert starts[0][starts[0].index("start") + 1:] == list(module.WAREHOUSE_SERVICES)
    forbidden = {"up", "build", "pull", "down", "rm", "migration", "storage-init"}
    assert not any(forbidden.intersection(command) for command in docker_commands)
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_missing_deployed_container_fails_without_creating_or_starting_containers(startup):
    module, fake, _ = startup
    fake.existing_services.remove("backend")
    assert module.main(["--all-devices"]) == 1
    assert not any("start" in command for command in fake.commands() if command[0] == "docker")
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_missing_service_is_failure_but_other_services_still_start(startup):
    module, fake, _ = startup
    missing = module.LOCAL_UNITS[0]
    fake.missing_units.add(missing)
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS) - {missing}
    assert any(command[0] == "ssh" for command in fake.commands())


def test_failed_service_start_does_not_prevent_remaining_local_services(startup):
    module, fake, _ = startup
    fake.start_failures.add(module.LOCAL_UNITS[0])
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_ssh_failure_is_nonzero_and_preserves_started_local_services(startup):
    module, fake, _ = startup
    fake.ssh_returncode = 255
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_device_failure_is_nonzero_and_still_attempts_remote_sources(startup):
    module, fake, _ = startup
    fake.local_returncode = 1
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert any(command[0] == "ssh" for command in fake.commands())


def test_already_active_units_are_checked_without_restarting(startup):
    module, fake, _ = startup
    fake.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert module.main(["--all-devices", "--no-warehouse"]) == 0
    assert fake.started_units() == set()


def test_no_device_sources_skips_hardware_helpers_but_keeps_archive_and_transport(startup):
    module, fake, _ = startup
    assert module.main(["--all-devices", "--no-warehouse", "--no-device-sources"]) == 0
    assert not any(command[0] == sys.executable for command in fake.commands())
    assert "robot-cloud-chassis-telemetry.service" not in fake.started_units()
    assert "robot-native-archive.service" in fake.started_units()
    ssh = next(command for command in fake.commands() if command[0] == "ssh")
    assert shlex.split(ssh[-1]) == ["python3", "-B", "-", "jetson", "--no-camera-source"]


def test_no_camera_source_passed_to_remote_helper(startup):
    module, fake, _ = startup
    assert module.main(["--all-devices", "--no-warehouse", "--no-camera-source"]) == 0
    ssh = next(command for command in fake.commands() if command[0] == "ssh")
    assert "--no-camera-source" in shlex.split(ssh[-1])
    assert "-R" not in ssh and "-L" not in ssh
    assert "StrictHostKeyChecking=yes" in ssh
    assert "ForwardAgent=no" in ssh


@pytest.mark.parametrize("arguments", [
    ["--unknown"],
    ["--ready-timeout", "0"],
    ["--ready-timeout", "301"],
    ["--ready-timeout", "not-a-number"],
    ["--ready-timeout", "$(touch /tmp/should-not-execute)"],
    ["--status", "--dry-run"],
])
def test_invalid_arguments_execute_nothing(startup, arguments):
    module, fake, home = startup
    with pytest.raises(SystemExit) as error:
        module.main(arguments)
    assert error.value.code == 2
    assert fake.calls == []
    assert not (home / ".local/state/robot-cloud").exists()


def test_invalid_timeout_environment_executes_nothing(startup, monkeypatch):
    module, fake, _ = startup
    monkeypatch.setenv("START_ALL_READY_TIMEOUT", "invalid")
    with pytest.raises(SystemExit) as error:
        module.main(["--all-devices"])
    assert error.value.code == 2
    assert fake.calls == []


def test_configuration_is_parsed_without_shell_evaluation(startup):
    module, fake, home = startup
    env_file = home / ".config/robot-cloud/video-tunnel.env"
    env_file.write_text(env_file.read_text() + "VIDEO_SOURCE_HOST='$(touch unsafe-marker)'\n")
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert not (home / "unsafe-marker").exists()
    assert not any(command[0] == "ssh" for command in fake.commands())
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)


def test_startup_lock_failure_prevents_partial_start(startup, monkeypatch):
    module, fake, _ = startup

    @contextmanager
    def locked():
        raise module.StartupError("fixture lock already held")
        yield

    monkeypatch.setattr(module, "startup_lock", locked)
    assert module.main(["--all-devices"]) == 1
    assert fake.calls == []


def test_startup_reloads_stale_nginx_proxy_once_after_configuration_check(startup, monkeypatch):
    module, fake, _ = startup
    nginx_commands = lambda: [command for command in fake.commands() if "nginx" in command]

    def ready(url):
        if url.endswith("/nest/ready"):
            return any(command[-3:] == ["nginx", "-s", "reload"] for command in nginx_commands())
        return True

    monkeypatch.setattr(module, "http_ready", ready)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.main(["--all-devices"]) == 0
    commands = nginx_commands()
    assert len(commands) == 2
    assert commands[0][-5:] == ["exec", "-T", "web", "nginx", "-t"]
    assert commands[1][-6:] == ["exec", "-T", "web", "nginx", "-s", "reload"]


def test_status_reports_stale_proxy_without_nginx_reload(startup, monkeypatch):
    module, fake, _ = startup
    fake.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    monkeypatch.setattr(module, "http_ready", lambda url: not url.endswith("/nest/ready"))
    assert module.main(["--all-devices", "--status"]) == 1
    assert not any("nginx" in command or "start" in command for command in fake.commands())
    assert any(command[0] == "ssh" for command in fake.commands())


@pytest.mark.parametrize("report", ["{}", "[]", '{"devices": [{}]}',
                                    '{"devices": [{"key": "mid360", "state": "unknown"}]}'])
def test_invalid_helper_report_is_isolated_and_remote_check_continues(startup, report):
    module, fake, _ = startup
    fake.local_stdout = report
    assert module.main(["--all-devices", "--no-warehouse"]) == 1
    assert fake.started_units() == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert any(command[0] == "ssh" for command in fake.commands())


def test_default_start_only_starts_camera_transport_without_warehouse_or_ipc_drivers(startup):
    module, fake, home = startup
    (home / 'warehouse/docker-compose.yml').unlink()
    fake.missing_units.update(set(module.LOCAL_UNITS) - set(module.CAMERA_LOCAL_UNITS))
    assert module.main([]) == 0
    assert fake.started_units() == set(module.CAMERA_LOCAL_UNITS)
    commands = fake.commands()
    assert not any(command[0] in ('docker', sys.executable) for command in commands)
    ssh = next(command for command in commands if command[0] == 'ssh')
    assert shlex.split(ssh[-1]) == ['python3', '-B', '-', 'jetson', '--cameras-only']
    assert not any({'restart', 'stop', 'kill', 'pkill', 'enable'}.intersection(command) for command in commands)


def test_default_camera_dry_run_is_inert_and_excludes_unrelated_devices(startup, capsys):
    module, fake, home = startup
    assert module.main(['--dry-run']) == 0
    assert fake.calls == [] and not (home / '.local/state/robot-cloud').exists()
    output = capsys.readouterr().out
    assert '仅相机' in output and 'YOLO' in output and 'Berxel' in output
    assert all(unit in output for unit in module.CAMERA_LOCAL_UNITS)
    for unrelated in ('MID360', 'FAST-LIO', 'STL-19P', '蓝海', 'postgres',
                      'robot-native-archive.service', 'robot-cloud-chassis-telemetry.service',
                      'robot-cloud-dbprog-tunnel.service'):
        assert unrelated not in output


def test_default_camera_status_never_starts_devices_or_checks_unrelated_services(startup):
    module, fake, home = startup
    fake.active.update(module.CAMERA_LOCAL_UNITS)
    assert module.main(['--status']) == 0
    commands = fake.commands()
    assert fake.started_units() == set()
    assert not any(command[0] in ('docker', sys.executable) for command in commands)
    assert not (home / '.local/state/robot-cloud').exists()
    checked = {command[3] for command in commands if command[:3] == ['systemctl', '--user', 'show']}
    assert checked == set(module.CAMERA_LOCAL_UNITS)
    ssh = next(command for command in commands if command[0] == 'ssh')
    assert shlex.split(ssh[-1]) == ['python3', '-B', '-', 'jetson', '--status', '--cameras-only']


def test_explicit_camera_mode_matches_default_and_keeps_existing_units_running(startup):
    module, fake, _ = startup
    fake.active.update(module.CAMERA_LOCAL_UNITS)
    assert module.main(['--cameras-only']) == 0
    assert not fake.started_units()
    assert not any('stop' in command or 'restart' in command for command in fake.commands())


def test_camera_failure_does_not_start_full_stack_fallback(startup):
    module, fake, _ = startup
    fake.ssh_returncode = 255
    assert module.main([]) == 1
    assert fake.started_units() == set(module.CAMERA_LOCAL_UNITS)
    assert not any(command[0] in ('docker', sys.executable) for command in fake.commands())


def test_conflicting_scope_flags_are_rejected_without_actions(startup):
    module, fake, home = startup
    with pytest.raises(SystemExit) as error:
        module.main(['--cameras-only', '--all-devices'])
    assert error.value.code == 2 and fake.calls == []
    assert not (home / '.local/state/robot-cloud').exists()


@pytest.mark.parametrize('status,ready,expected,restarts', [
    (False, (True,), 0, 0),
    (False, (False, True), 0, 1),
    (False, (False, False), 1, 1),
    (True, (False,), 1, 0),
    (True, (True,), 0, 0),
])
def test_depth_receiver_recovery_only_after_real_source_frames(startup, monkeypatch, status, ready, expected, restarts):
    module, fake, _ = startup
    fake.active.update(module.CAMERA_LOCAL_UNITS)
    original = fake.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == 'ssh':
            result.stdout = json.dumps({'ok': True, 'devices': [
                {'key': 'berxel-frames', 'state': 'reused', 'dataVerified': True}]})
        return result
    fake.run = run
    samples = iter(ready)
    monkeypatch.setattr(module.Launcher, 'preview_receiver_ready', lambda self, seconds: next(samples))
    assert module.main(['--status'] if status else []) == expected
    recovered = [c for c in fake.commands() if c[:3] == ['systemctl', '--user', 'restart']]
    assert recovered == [['systemctl', '--user', 'restart', 'robot-cloud-collector.service']] * restarts
    assert not any('stop' in c or 'kill' in c for c in fake.commands())


def test_other_camera_failure_does_not_skip_depth_receiver_recovery(startup, monkeypatch):
    module, fake, _ = startup
    fake.active.update(module.CAMERA_LOCAL_UNITS)
    original = fake.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == 'ssh':
            result.returncode = 1
            result.stdout = json.dumps({'ok': False, 'devices': [
                {'key': 'usb-cameras', 'state': 'failed'},
                {'key': 'berxel-frames', 'state': 'reused', 'dataVerified': True}]})
        return result
    fake.run = run
    samples = iter((False, True))
    monkeypatch.setattr(module.Launcher, 'preview_receiver_ready', lambda self, seconds: next(samples))
    assert module.main([]) == 1  # Keep the separate USB error visible.
    assert ['systemctl', '--user', 'restart', 'robot-cloud-collector.service'] in fake.commands()


@pytest.mark.parametrize('advances', [True, False])
def test_receiver_requires_both_live_counters_to_advance(startup, monkeypatch, advances):
    module, fake, _ = startup
    original = fake.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[:3] == ['systemctl', '--user', 'show']:
            result.stdout = 'LoadState=loaded\nActiveState=active\nMainPID=123\n'
        return result
    fake.run = run
    clock = [10.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def report(path, pid):
        assert pid == 123
        return {'monotonic': clock[0], 'channels': {
            'color': {'frameSeq': int(clock[0] * 2), 'status': 'AVAILABLE', 'ageS': .2},
            'depth': {'frameSeq': int(clock[0] * 2) if advances else 1,
                      'status': 'AVAILABLE', 'ageS': .2}}}
    monkeypatch.setattr(module, 'read_preview_health', report)
    args = type('Args', (), {})()
    assert module.Launcher(args, fake).preview_receiver_ready(3) is advances
    assert not any('restart' in c for c in fake.commands())


def test_remote_process_alone_cannot_trigger_receiver_restart(startup, monkeypatch):
    module, fake, _ = startup
    original = fake.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == 'ssh':
            result.stdout = json.dumps({'ok': True, 'devices': [
                {'key': 'berxel', 'state': 'reused'},
                {'key': 'berxel-frames', 'state': 'failed', 'dataVerified': False}]})
        return result
    fake.run = run
    def unexpected(self, seconds):
        raise AssertionError('Must not recover when source has no verified frames')
    monkeypatch.setattr(module.Launcher, 'preview_receiver_ready', unexpected)
    module.main([])
    assert not any('restart' in c for c in fake.commands())


def test_advancing_but_delayed_receiver_reports_are_not_live(startup, monkeypatch):
    module, fake, _ = startup
    original = fake.run
    def run(args, **kwargs):
        result = original(args, **kwargs)
        if args[:3] == ['systemctl', '--user', 'show']:
            result.stdout = 'LoadState=loaded\nActiveState=active\nMainPID=123\n'
        return result
    fake.run = run
    clock, observed = [10.0], []
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep',
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def report(path, pid):
        assert pid == 123
        value = {'monotonic': clock[0] - 2.2, 'channels': {
            key: {'frameSeq': int(clock[0] * 2), 'status': 'AVAILABLE', 'ageS': .2}
            for key in ('color', 'depth')}}
        observed.append(value)
        return value
    monkeypatch.setattr(module, 'read_preview_health', report)
    args = type('Args', (), {})()
    assert module.Launcher(args, fake).preview_receiver_ready(3) is False
    # Each report meets the file freshness bound and both counters advance;
    # its effective frame age is 2.4 seconds, so it cannot establish live video.
    assert len(observed) >= 3
    assert observed[-1]['monotonic'] - observed[0]['monotonic'] >= 1
    assert all(observed[-1]['channels'][key]['frameSeq'] >
               observed[0]['channels'][key]['frameSeq'] for key in ('color', 'depth'))
    assert not any('restart' in command for command in fake.commands())
