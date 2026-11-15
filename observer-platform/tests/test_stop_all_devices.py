"""Shutdown orchestration with simulated services; never touches real devices."""
from datetime import datetime, timezone
import json
import shlex
import subprocess
import threading
from types import SimpleNamespace
import sys

import pytest

from tests.test_start_all_devices import FakeRunner, startup  # noqa: F401


SOURCE_KEYS = {
    'ipc': ('mid360', 'stl19p', 'bluesea'),
    'jetson': ('usb-cameras', 'berxel', 'berxel-preview', 'robot-native-archive',
               'robot-cloud-camera-status-gateway', 'robot-cloud-camera-relay',
               'robot-cloud-yolo-observer'),
}


def stopped_report(role, *, state='stopped'):
    return {'ok': True, 'stop': True, 'role': role, 'dryRun': False,
            'devices': [{'key': key, 'state': state} for key in SOURCE_KEYS[role]]}


class StopRunner(FakeRunner):
    def __init__(self, module):
        super().__init__(module)
        self.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
        self.stop_failures = set()
        self.unit_results = {}
        self.docker_stop_failure = None
        self.docker_retains_group = False
        self.events = []
        self.local_stdout = json.dumps(stopped_report('ipc'))
        self.ssh_stdout = json.dumps(stopped_report('jetson'))

    def run(self, args, **kwargs):
        args = list(args)
        self.events.append(args)
        if args[:3] == ['systemctl', '--user', 'stop']:
            self.calls.append((args, kwargs))
            unit = args[3]
            code = 1 if unit in self.stop_failures else 0
            if not code:
                self.active.discard(unit)
            return subprocess.CompletedProcess(args, code, stdout='', stderr='')
        if args[:2] == ['docker', 'compose'] and 'stop' in args:
            self.calls.append((args, kwargs))
            group = tuple(value for value in args[args.index('stop') + 1:]
                          if value in self.module.WAREHOUSE_SERVICES)
            code = 1 if group == self.docker_stop_failure else 0
            if not code and not self.docker_retains_group:
                self.running_services = [name for name in self.running_services if name not in group]
            return subprocess.CompletedProcess(args, code, stdout='', stderr='')
        result = super().run(args, **kwargs)
        if args[0] == 'ssh' and not result.returncode:
            result.stdout = self.ssh_stdout
        if args[:3] == ['systemctl', '--user', 'show']:
            result.stdout += 'Result=' + self.unit_results.get(args[3], 'success') + '\n'
        return result

    def stopped_units(self):
        return [args[3] for args in self.commands() if args[:3] == ['systemctl', '--user', 'stop']]

    def docker_stop_groups(self):
        return [tuple(value for value in args[args.index('stop') + 1:]
                      if value in self.module.WAREHOUSE_SERVICES)
                for args in self.commands() if args[:2] == ['docker', 'compose'] and 'stop' in args]


@pytest.fixture
def shutdown(startup, monkeypatch):
    module, _, home = startup
    fake = StopRunner(module)
    monkeypatch.setattr(module, 'Runner', lambda: fake)

    def drained(self):
        fake.events.append('archive-drained')

    monkeypatch.setattr(module.Shutdown, 'wait_archive_idle', drained)
    return module, fake, home


def test_stop_dry_run_has_no_commands_lock_or_state_writes(shutdown, capsys):
    module, fake, home = shutdown
    assert module.main(['--stop', '--dry-run']) == 0
    assert fake.calls == []
    assert not (home / '.local/state/robot-cloud').exists()
    assert '关闭预演' in capsys.readouterr().out


def test_stop_clicked_during_startup_waits_then_runs_without_another_click(shutdown, monkeypatch):
    module, fake, _ = shutdown
    waiting = threading.Event()
    results, errors = [], []

    def say(self, message):
        if '等待' in message:
            waiting.set()

    def click_stop():
        try:
            results.append(module.main(['--stop', '--no-warehouse']))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(module.Launcher, 'say', say)
    thread = threading.Thread(target=click_stop, daemon=True)
    try:
        # Separate file descriptors exercise the actual process-shared flock.
        # The held lock represents the startup window still running.
        with module.startup_lock():
            thread.start()
            assert waiting.wait(3), f'Stop did not wait for startup: {errors}'
            assert thread.is_alive()
            assert fake.calls == []
            assert fake.events == []
    finally:
        # Always release the simulated startup lock before joining, including
        # assertion failures, so this test cannot strand a waiting operation.
        if thread.ident is not None:
            thread.join(3)
    assert not thread.is_alive()
    assert errors == []
    assert results == [0]
    assert set(fake.stopped_units()) == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert sum(command[0] == 'ssh' for command in fake.commands()) == 1
    assert sum(command[0] == sys.executable for command in fake.commands()) == 1


def test_another_start_cannot_run_while_real_operation_lock_is_held(shutdown, capsys):
    module, fake, _ = shutdown
    with module.startup_lock():
        assert module.main(['--no-warehouse']) == 1
        assert fake.calls == []
    assert '正在执行' in capsys.readouterr().err


def test_shutdown_orders_sources_drain_writers_transport_then_storage(shutdown):
    module, fake, _ = shutdown
    assert module.main(['--stop']) == 0
    events = fake.events
    remote = next(i for i, event in enumerate(events) if isinstance(event, list) and event[0] == 'ssh')
    local = next(i for i, event in enumerate(events) if isinstance(event, list) and event[0] == sys.executable)
    telemetry = events.index(['systemctl', '--user', 'stop', 'robot-cloud-chassis-telemetry.service'])
    drain = events.index('archive-drained')
    archive = events.index(['systemctl', '--user', 'stop', 'robot-native-archive.service'])
    sync = events.index(['systemctl', '--user', 'stop', 'robot-native-archive-source-link.service'])
    first_docker_stop = next(i for i, event in enumerate(events)
                             if isinstance(event, list) and event[:2] == ['docker', 'compose'] and 'stop' in event)
    assert max(remote, local, telemetry) < drain < archive < sync < first_docker_stop
    assert set(fake.stopped_units()) == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    for unit in module.TUNNEL_UNITS:
        assert sync < events.index(['systemctl', '--user', 'stop', unit]) < first_docker_stop
    assert fake.docker_stop_groups() == [('web', 'worker', 'backend'), ('minio', 'redis', 'postgres')]
    commands = fake.commands()
    ssh = next(command for command in commands if command[0] == 'ssh')
    assert shlex.split(ssh[-1]) == ['python3', '-B', '-', 'jetson', '--stop']
    helper = next(command for command in commands if command[0] == sys.executable)
    assert helper[-2:] == ['ipc', '--stop']
    forbidden = {'start', 'restart', 'enable', 'disable', 'down', 'rm', 'kill', 'pkill', 'poweroff', 'reboot'}
    assert not any(forbidden.intersection(command) for command in commands)


@pytest.mark.parametrize('failure', ['ssh', 'local-sources', 'telemetry'])
def test_source_failure_keeps_archive_sync_tunnels_and_database(shutdown, failure):
    module, fake, _ = shutdown
    if failure == 'ssh':
        fake.ssh_returncode = 255
    elif failure == 'local-sources':
        fake.local_returncode = 1
        fake.local_stdout = '{"devices": [{"key": "mid360", "state": "external_running"}]}'
    else:
        fake.stop_failures.add('robot-cloud-chassis-telemetry.service')
    assert module.main(['--stop']) == 1
    assert fake.stopped_units() == ['robot-cloud-chassis-telemetry.service']
    assert 'archive-drained' not in fake.events
    assert not any(command[0] == 'docker' for command in fake.commands())
    assert any(command[0] == 'ssh' for command in fake.commands())
    assert any(command[0] == sys.executable for command in fake.commands())


def test_missing_preview_stop_confirmation_preserves_storage(shutdown):
    module, fake, _ = shutdown
    report = stopped_report('jetson')
    report['devices'] = [row for row in report['devices'] if row['key'] != 'berxel-preview']
    fake.ssh_stdout = json.dumps(report)
    assert module.main(['--stop']) == 1
    assert 'archive-drained' not in fake.events
    assert not fake.docker_stop_groups()


@pytest.mark.parametrize('role', ['ipc', 'jetson'])
@pytest.mark.parametrize('problem', [
    'not-ok', 'missing-ok', 'not-stop', 'missing-stop', 'empty', 'missing-key',
    'failed', 'skipped', 'external_running', 'reused',
])
def test_exit_zero_without_complete_stop_confirmation_preserves_data_services(shutdown, role, problem):
    module, fake, _ = shutdown
    report = stopped_report(role)
    if problem == 'not-ok':
        report['ok'] = False
    elif problem == 'missing-ok':
        del report['ok']
    elif problem == 'not-stop':
        report['stop'] = False
    elif problem == 'missing-stop':
        del report['stop']
    elif problem == 'empty':
        report['devices'] = []
    elif problem == 'missing-key':
        report['devices'].pop()
    else:
        report['devices'][0]['state'] = problem
    setattr(fake, 'local_stdout' if role == 'ipc' else 'ssh_stdout', json.dumps(report))
    assert fake.local_returncode == fake.ssh_returncode == 0
    assert module.main(['--stop']) == 1
    assert fake.stopped_units() == ['robot-cloud-chassis-telemetry.service']
    assert 'archive-drained' not in fake.events
    assert not any(command[0] == 'docker' for command in fake.commands())


def test_archive_drain_timeout_keeps_writers_and_database(shutdown, monkeypatch):
    module, fake, _ = shutdown

    def not_drained(self):
        raise module.StartupError('fixture writer still active')

    monkeypatch.setattr(module.Shutdown, 'wait_archive_idle', not_drained)
    assert module.main(['--stop']) == 1
    assert fake.stopped_units() == ['robot-cloud-chassis-telemetry.service']
    assert not any(command[0] == 'docker' for command in fake.commands())


@pytest.mark.parametrize('unit,result', [
    ('robot-native-archive.service', 'failure'),
    ('robot-native-archive.service', 'timeout'),
    ('robot-native-archive-source-link.service', 'failure'),
])
def test_failed_archive_or_sync_stop_preserves_remaining_services(shutdown, unit, result):
    module, fake, _ = shutdown
    if result == 'failure':
        fake.stop_failures.add(unit)
    else:
        fake.unit_results[unit] = result
    assert module.main(['--stop']) == 1
    assert unit in fake.stopped_units()
    assert 'robot-cloud-collector.service' not in fake.stopped_units()
    assert not any(name in fake.stopped_units() for name in module.TUNNEL_UNITS)
    assert not any(command[0] == 'docker' for command in fake.commands())


@pytest.mark.parametrize('failure', ['command', 'still-running'])
def test_application_container_stop_failure_preserves_storage_containers(shutdown, failure):
    module, fake, _ = shutdown
    if failure == 'command':
        fake.docker_stop_failure = ('web', 'worker', 'backend')
    else:
        fake.docker_retains_group = True
    assert module.main(['--stop']) == 1
    assert fake.docker_stop_groups() == [('web', 'worker', 'backend')]
    assert {'postgres', 'redis', 'minio'}.issubset(fake.running_services)


def test_no_warehouse_leaves_all_container_services_alone(shutdown):
    module, fake, _ = shutdown
    assert module.main(['--stop', '--no-warehouse']) == 0
    assert set(fake.stopped_units()) == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert not any(command[0] == 'docker' for command in fake.commands())


def test_shutdown_is_repeatable_for_inactive_or_missing_managed_units(shutdown):
    module, fake, _ = shutdown
    fake.missing_units.add('robot-cloud-collector.service')
    fake.active.discard('robot-cloud-collector.service')
    assert module.main(['--stop', '--no-warehouse']) == 0
    count = len(fake.stopped_units())
    fake.local_stdout = json.dumps(stopped_report('ipc', state='already_stopped'))
    fake.ssh_stdout = json.dumps(stopped_report('jetson', state='already_stopped'))
    assert module.main(['--stop', '--no-warehouse']) == 0
    assert len(fake.stopped_units()) == count
    assert 'robot-cloud-collector.service' not in fake.stopped_units()


@pytest.mark.parametrize('arguments', [
    ['--stop', '--status'],
    ['--stop', '--no-camera-source'],
    ['--stop', '--no-device-sources'],
    ['--stop', '--ready-timeout', 'invalid'],
])
def test_invalid_shutdown_options_are_rejected_before_any_commands(shutdown, arguments):
    module, fake, home = shutdown
    with pytest.raises(SystemExit) as error:
        module.main(arguments)
    assert error.value.code == 2
    assert fake.calls == []
    assert not (home / '.local/state/robot-cloud').exists()


def archive_case(startup, monkeypatch, *, report):
    module, _, home = startup
    fake = StopRunner(module)
    args = SimpleNamespace(status=False, stop=True, no_warehouse=False, dry_run=False)
    launcher = module.Shutdown(args, runner=fake)
    directory = home / 'warehouse/.runtime/garage-archive-control'
    directory.mkdir(parents=True)
    gate = directory / 'garage.json'
    gate.write_text('{"scope":"garage","enabled":true}')
    status = directory / 'status.json'
    clock = SimpleNamespace(elapsed=0.0, epoch=1_800_000_000.0)
    monkeypatch.setattr(module.time, 'time', lambda: clock.epoch + clock.elapsed)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.elapsed)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'elapsed', clock.elapsed + seconds))
    status.write_text(json.dumps(report(clock.epoch)))
    return module, launcher, fake, status, gate, clock


def idle_report(timestamp):
    return {'updatedAt': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            'ros': {'queueMessages': 0, 'queueBytes': 0, 'tickError': None, 'gateError': None},
            'store': {'active': {}, 'errors': {}}}


def test_archive_drain_requires_fresh_status_and_preserves_garage_choice(startup, monkeypatch):
    module, launcher, fake, status, gate, clock = archive_case(
        startup, monkeypatch, report=lambda now: idle_report(now - 1))
    before = gate.read_bytes()

    def next_status(seconds):
        clock.elapsed += seconds
        status.write_text(json.dumps(idle_report(clock.epoch + clock.elapsed)))

    monkeypatch.setattr(module.time, 'sleep', next_status)
    launcher.wait_archive_idle()
    assert clock.elapsed == 1.0  # The pre-shutdown status was not accepted.
    assert gate.read_bytes() == before
    assert all(command[:3] == ['systemctl', '--user', 'show'] for command in fake.commands())


@pytest.mark.parametrize('problem', ['queued', 'in-flight', 'active-segment', 'writer-error', 'stale',
                                   'video-pending', 'video-running'])
def test_archive_drain_never_treats_queued_unclosed_or_failed_data_as_complete(
        startup, monkeypatch, problem):
    def report(now):
        value = idle_report(now if problem != 'stale' else now - 60)
        if problem == 'queued':
            value['ros']['queueMessages'] = 1
        elif problem == 'in-flight':
            value['ros']['inFlightMessages'] = 1
        elif problem == 'active-segment':
            value['store']['active'] = {'lidar': {'messages': 1}}
        elif problem == 'writer-error':
            value['store']['errors'] = {'lidar': 'write failed'}
        elif problem == 'video-pending':
            value['store']['videoPendingSegments'] = 1
        elif problem == 'video-running':
            value['store']['videoFinalizing'] = True
        return value

    module, launcher, fake, _, gate, _ = archive_case(startup, monkeypatch, report=report)
    before = gate.read_bytes()
    with pytest.raises(module.StartupError, match='收尾'):
        launcher.wait_archive_idle()
    assert fake.stopped_units() == []
    assert gate.read_bytes() == before


def test_explicit_camera_stop_preserves_warehouse_archives_collector_and_other_devices(shutdown):
    module, fake, _ = shutdown
    report = stopped_report('jetson')
    report['devices'] = [row for row in report['devices'] if row['key'] != 'robot-native-archive']
    fake.ssh_stdout = json.dumps(report)
    assert module.main(['--cameras-only', '--stop']) == 0
    assert set(fake.stopped_units()) == set(module.TUNNEL_UNITS)
    assert set(module.LOCAL_UNITS).issubset(fake.active)
    assert not any(command[0] in ('docker', sys.executable) for command in fake.commands())
    assert 'archive-drained' not in fake.events
    ssh = next(command for command in fake.commands() if command[0] == 'ssh')
    assert shlex.split(ssh[-1]) == ['python3', '-B', '-', 'jetson', '--stop', '--cameras-only']


def test_camera_stop_requires_yolo_confirmation_before_closing_transport(shutdown):
    module, fake, _ = shutdown
    report = stopped_report('jetson')
    report['devices'] = [row for row in report['devices'] if row['key'] != 'robot-cloud-yolo-observer']
    fake.ssh_stdout = json.dumps(report)
    assert module.main(['--cameras-only', '--stop']) == 1
    assert fake.stopped_units() == [] and fake.docker_stop_groups() == []


def test_explicit_all_devices_stop_keeps_legacy_full_shutdown(shutdown):
    module, fake, _ = shutdown
    assert module.main(['--all-devices', '--stop']) == 0
    assert set(fake.stopped_units()) == set(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    assert fake.docker_stop_groups() == [('web', 'worker', 'backend'), ('minio', 'redis', 'postgres')]


def test_camera_stop_dry_run_has_no_actions(shutdown, capsys):
    module, fake, home = shutdown
    assert module.main(['--cameras-only', '--stop', '--dry-run']) == 0
    assert fake.calls == [] and not (home / '.local/state/robot-cloud').exists()
    assert '相机关闭预演' in capsys.readouterr().out
