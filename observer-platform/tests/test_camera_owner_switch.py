"""Camera ownership switching tests; no device, real unit or PID is touched.

All actions use FakeOps. The only operating-system action tested separately is
flock in pytest's private temporary directory, never a production state path.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from deploy.scripts import camera_owner as owner


def snapshot(*, active=(), holders=(), healthy=False, missing=(), errors=(), unverified=()):
    names = (*owner.CAPTURE_UNITS, owner.RELAY_UNIT)
    return owner.Snapshot(units={name: owner.Unit(name=name, active=name in active,
        main_pid=300 + index if name in active else 0, verified=name not in unverified)
        for index, name in enumerate(names)}, holders=tuple(holders), missing=tuple(missing),
        healthy=healthy, scan_errors=tuple(errors), partial_visibility=True)


def known_holder(unit=None, *, pid=301, uid=1000):
    return owner.Holder(pid=pid, uid=uid, nodes=('/dev/video0',), unit=unit or owner.NEW_WEB_UNIT)


class FakeOps:
    uid = 1000

    def __init__(self, initial=None):
        self.current = initial or snapshot()
        self.reads, self.mutations, self.lock_calls = [], [], 0
        self.locked, self.lock_available = False, True
        self.after_lock = None
        self.stopped_ok, self.start_error, self.health_error = True, None, None
        self.receipt = owner.StartReceipt(unit=owner.NEW_WEB_UNIT, run_id='ISOLATED_TEST_RUN')
        self.ready = snapshot(active=(owner.NEW_WEB_UNIT,), holders=(known_holder(),), healthy=True)

    def snapshot(self):
        self.reads.append('snapshot')
        return self.after_lock if self.locked and self.after_lock is not None else self.current

    @contextmanager
    def lock(self):
        self.lock_calls += 1
        if not self.lock_available:
            raise owner.OwnerError('ISOLATED_LOCK_BUSY')
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def stop_unit(self, name):
        assert self.locked
        assert name in (*owner.CAPTURE_UNITS, owner.RELAY_UNIT)
        self.mutations.append(('stop_unit', name))
        units = dict(self.current.units)
        if name in units:
            units[name] = replace(units[name], active=False, main_pid=0)
        self.current = replace(self.current, units=units,
                               holders=tuple(holder for holder in self.current.holders if holder.unit != name))

    def wait_stopped(self, names):
        assert self.locked
        assert set(names) <= set((*owner.CAPTURE_UNITS, owner.RELAY_UNIT))
        self.reads.append(('wait_stopped', tuple(names)))
        return self.stopped_ok

    def start_web(self):
        assert self.locked
        self.mutations.append(('start_web', self.receipt))
        if self.start_error:
            raise self.start_error
        return self.receipt

    def wait_healthy(self):
        assert self.locked
        self.reads.append('wait_healthy')
        if self.health_error:
            raise self.health_error
        return self.ready

    def stop_new(self, receipt):
        assert self.locked
        assert receipt == self.receipt
        self.mutations.append(('stop_new', receipt))

    def save_state(self, action, current):
        assert self.locked
        self.mutations.append(('save_state', action))


@pytest.fixture(autouse=True)
def forbid_real_process_actions(monkeypatch):
    """A mistaken fallback to actual process operations must fail this test."""
    def forbidden(*args, **kwargs):
        pytest.fail('Real process signalling or subprocess execution is forbidden in ownership tests')
    monkeypatch.setattr(owner.os, 'kill', forbidden)
    monkeypatch.setattr(owner.subprocess, 'run', forbidden)
    monkeypatch.setattr(owner.subprocess, 'Popen', forbidden)


@pytest.mark.parametrize('action,dry_run', [('status', False), ('status', True), ('web', True), ('peer', True)])
def test_status_and_dry_run_are_read_only_without_even_acquiring_a_lock(action, dry_run):
    ops = FakeOps(snapshot(active=owner.CAPTURE_UNITS))
    result = owner.Engine(ops).execute(action, dry_run=dry_run)
    assert result['ok'] and result['action'] == action
    assert result['dryRun'] == dry_run
    assert ops.mutations == [] and ops.lock_calls == 0
    assert ops.reads and all(item == 'snapshot' for item in ops.reads)


@pytest.mark.parametrize('holder', [
    owner.Holder(pid=999, uid=1000, nodes=('/dev/video0',), unit=None),
    owner.Holder(pid=1, uid=0, nodes=('/dev/video0',), unit=None),
    owner.Holder(pid=42, uid=1000, nodes=('/dev/video0',), unit='calibration-gui.service'),
    owner.Holder(pid=999, uid=1000, nodes=('/dev/video0',), unit=None, unreadable=True),
])
def test_unknown_system_gui_or_unreadable_owner_blocks_web_without_stopping_anyone(holder):
    ops = FakeOps(snapshot(active=(owner.CAPTURE_UNITS[1],), holders=(holder,)))
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.mutations == []


def test_wrong_uid_is_not_trusted_even_when_holder_claims_a_capture_unit():
    ops = FakeOps(snapshot(active=(owner.NEW_WEB_UNIT,), holders=(known_holder(uid=0),)))
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.mutations == []


@pytest.mark.parametrize('fields', [dict(missing=('camera4',)), dict(errors=('ISOLATED_PROC_PERMISSION_ERROR',)),
                                    dict(active=(owner.NEW_WEB_UNIT,), unverified=(owner.NEW_WEB_UNIT,))])
def test_missing_devices_failed_scans_or_unverified_capture_refuse_before_mutation(fields):
    ops = FakeOps(snapshot(**fields))
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.mutations == []


def test_partial_verified_cameras_start_and_report_only_the_connected_identities():
    initial = replace(snapshot(missing=('2.2.1',)), available=('camera1', 'camera2', 'camera4'),
                      missing_cameras=('camera3',))
    ops = FakeOps(initial)
    ops.ready = replace(ops.ready, available=initial.available, missing=initial.missing,
                        missing_cameras=initial.missing_cameras)
    result = owner.Engine(ops).execute('web')
    assert result['ok'] and result['partial']
    assert result['dataVerifiedFor'] == ['camera1', 'camera2', 'camera4']
    assert result['snapshot']['missing_cameras'] == ('camera3',)


def test_partial_available_cameras_do_not_bypass_unknown_holder_check():
    current = replace(snapshot(missing=('2.2.1',), holders=(known_holder(unit='unknown.service'),)),
                      available=('camera1', 'camera2', 'camera4'), missing_cameras=('camera3',))
    ops = FakeOps(current)
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.mutations == []


def test_peer_stops_only_capture_and_relay_whitelist_and_preserves_calibration_gui():
    calibration = owner.Holder(pid=991, uid=1000, nodes=('/dev/video0',), unit=None)
    initial = snapshot(active=(*owner.CAPTURE_UNITS, owner.RELAY_UNIT),
                       holders=(known_holder(unit=owner.CAPTURE_UNITS[0]), calibration))
    ops = FakeOps(initial)
    result = owner.Engine(ops).execute('peer')
    assert result['ok'] and result['action'] == 'peer'
    stopped = [name for action, name in ops.mutations if action == 'stop_unit']
    assert set(stopped) == set((*owner.CAPTURE_UNITS, owner.RELAY_UNIT))
    assert not any(action in ('start_web', 'stop_new') for action, _ in ops.mutations)
    assert ops.current.holders == (calibration,)
    assert result['snapshot']['holders'], 'Remaining camera holders must be reported to the caller'


def test_healthy_source_is_reused_without_launch_or_cleanup_or_state_overwrite():
    ops = FakeOps(snapshot(active=(owner.CAPTURE_UNITS[2],),
                           holders=(known_holder(unit=owner.CAPTURE_UNITS[2]),), healthy=True))
    result = owner.Engine(ops).execute('web')
    assert result['ok'] and result['reused']
    assert ops.mutations == []
    assert 'wait_healthy' not in ops.reads


def test_invalid_shared_replacement_is_checked_before_existing_owner_is_stopped():
    ops = FakeOps(snapshot(active=(owner.CAPTURE_UNITS[1],)))
    def unavailable():
        raise owner.OwnerError('Shared target is not installed')
    ops.validate_start_target = unavailable
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.mutations == []


def test_locked_recheck_catches_new_gui_owner_before_any_stop_or_start():
    ops = FakeOps(snapshot())
    ops.after_lock = snapshot(holders=(owner.Holder(pid=993, uid=1000, nodes=('/dev/video0',), unit=None),))
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert ops.lock_calls == 1 and ops.mutations == []


def test_lock_contention_does_not_stop_or_start_and_releases_no_foreign_process():
    ops = FakeOps(snapshot(active=owner.CAPTURE_UNITS))
    ops.lock_available = False
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('peer')
    assert ops.lock_calls == 1 and ops.mutations == []


def test_stopping_timeout_never_launches_a_second_capture():
    ops = FakeOps(snapshot(active=(owner.CAPTURE_UNITS[1],),
                           holders=(known_holder(unit=owner.CAPTURE_UNITS[1]),)))
    ops.stopped_ok = False
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert not any(action in ('start_web', 'stop_new', 'save_state') for action, _ in ops.mutations)


def test_health_timeout_cleans_only_this_attempt_receipt_and_does_not_save_success():
    ops = FakeOps()
    ops.health_error = owner.OwnerError('ISOLATED_HEALTH_TIMEOUT')
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert [item for item in ops.mutations if item[0] == 'stop_new'] == [('stop_new', ops.receipt)]
    assert not any(item[0] == 'save_state' for item in ops.mutations)


@pytest.mark.parametrize('partial_start', [False, True])
def test_partial_start_failure_cleans_receipt_only_when_this_attempt_created_one(partial_start):
    ops = FakeOps()
    ops.start_error = owner.StartFailure('ISOLATED_START_FAILURE', receipt=ops.receipt if partial_start else None)
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    cleanups = [item for item in ops.mutations if item[0] == 'stop_new']
    assert cleanups == ([('stop_new', ops.receipt)] if partial_start else [])
    assert not any(item[0] == 'save_state' for item in ops.mutations)


def test_successful_new_capture_saves_state_after_health_without_unrelated_commands():
    ops = FakeOps()
    result = owner.Engine(ops).execute('web')
    assert result['ok'] and not result['reused']
    assert [action for action, _ in ops.mutations] == ['start_web', 'save_state']
    assert ops.mutations[-1] == ('save_state', 'web')
    assert 'wait_healthy' in ops.reads


def test_invalid_actions_cannot_execute_arbitrary_units_or_shell_commands():
    ops = FakeOps()
    with pytest.raises((owner.OwnerError, ValueError)):
        owner.Engine(ops).execute('ros2 navigation serial; unsafe')
    assert ops.mutations == []


def test_real_flock_is_mutually_exclusive_and_can_be_reacquired_after_release(tmp_path):
    settings = owner.Settings(private_dir=tmp_path / 'owner')
    first, second = owner.SystemOps(settings), owner.SystemOps(settings)
    with first.lock():
        with pytest.raises(owner.OwnerError):
            with second.lock():
                pytest.fail('A second helper must not enter the ownership transition')
    with second.lock():
        pass
    assert stat.S_IMODE(settings.private_dir.stat().st_mode) == 0o700
    files = list(settings.private_dir.iterdir())
    assert files and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def test_real_lock_refuses_symlink_directory_without_changing_its_target(tmp_path):
    target = tmp_path / 'other-owner'
    target.mkdir(mode=0o755)
    private = tmp_path / 'owner'
    private.symlink_to(target, target_is_directory=True)
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    before = stat.S_IMODE(target.stat().st_mode)
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('A symlink must never select another owner state directory')
    assert stat.S_IMODE(target.stat().st_mode) == before and list(target.iterdir()) == []


def test_real_lock_refuses_insecure_existing_directory_without_chmod(tmp_path):
    private = tmp_path / 'owner'
    private.mkdir(mode=0o755)
    before = stat.S_IMODE(private.stat().st_mode)
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('An existing public directory must not be silently repaired')
    assert stat.S_IMODE(private.stat().st_mode) == before and list(private.iterdir()) == []


def test_real_lock_refuses_hardlink_without_changing_or_unlinking_existing_files(tmp_path):
    private = tmp_path / 'owner'
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    with ops.lock():
        pass
    files = list(private.iterdir())
    assert len(files) == 1
    lock = files[0]
    linked = tmp_path / 'foreign-lock-link'
    os.link(lock, linked)
    before = lock.read_bytes(), stat.S_IMODE(lock.stat().st_mode), lock.stat().st_ino
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('A multiply linked lock file must not be accepted')
    assert lock.exists() and linked.exists()
    assert (lock.read_bytes(), stat.S_IMODE(lock.stat().st_mode), lock.stat().st_ino) == before


def unit_output(*, description, active=True, verified=True):
    return '\n'.join(['LoadState=loaded', 'ActiveState=' + ('active' if active else 'inactive'),
        'MainPID=0', 'ControlGroup=/isolated/' + owner.NEW_WEB_UNIT,
        'ExecStart=' + ('{ path=/bin/bash ; argv[]=/bin/bash ' + owner.LAUNCHER +
            ' --host 127.0.0.1 --allow-client 127.0.0.1 ; ignore_errors=no ; }'
            if verified else '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /isolated/unrelated-calibration.py ; }'),
        'Description=' + description])


def test_failed_systemd_start_does_not_return_cleanup_receipt_for_preexisting_other_preview(tmp_path, monkeypatch):
    ops, commands = owner.SystemOps(owner.Settings(private_dir=tmp_path / 'owner')), []
    def command(arguments, timeout=None):
        commands.append(tuple(arguments))
        if arguments[0] == 'systemd-run':
            return SimpleNamespace(returncode=1, stdout='', stderr='ISOLATED_EXISTING_UNIT')
        return SimpleNamespace(returncode=0, stdout=unit_output(description='Robot camera web owner OTHER_RUN'), stderr='')
    monkeypatch.setattr(ops, 'command', command)
    with pytest.raises(owner.StartFailure) as error:
        ops.start_web()
    assert error.value.receipt is None
    assert not any('stop' in arguments for arguments in commands)


def test_systemd_timeout_returns_receipt_only_for_this_attempt_run_description(tmp_path, monkeypatch):
    ops, commands = owner.SystemOps(owner.Settings(private_dir=tmp_path / 'owner')), []
    description = [None]
    def command(arguments, timeout=None):
        commands.append(tuple(arguments))
        if arguments[0] == 'systemd-run':
            description[0] = next(item.split('=', 1)[1] for item in arguments if item.startswith('--description='))
            raise owner.subprocess.TimeoutExpired(arguments, timeout=1)
        return SimpleNamespace(returncode=0, stdout=unit_output(description=description[0]), stderr='')
    monkeypatch.setattr(ops, 'command', command)
    with pytest.raises(owner.StartFailure) as error:
        ops.start_web()
    assert error.value.receipt is not None and error.value.receipt.unit == owner.NEW_WEB_UNIT
    assert description[0] == 'Robot camera web owner ' + error.value.receipt.run_id
    assert not any('stop' in arguments for arguments in commands)


@pytest.mark.parametrize('receipt', [owner.StartReceipt(unit=owner.RELAY_UNIT, run_id='WRONG_UNIT'),
                                    owner.StartReceipt(unit=owner.NEW_WEB_UNIT, run_id='')])
def test_cleanup_without_exact_new_unit_receipt_cannot_issue_any_command(tmp_path, monkeypatch, receipt):
    ops = owner.SystemOps(owner.Settings(private_dir=tmp_path / 'owner'))
    monkeypatch.setattr(ops, 'command', lambda *args, **kwargs: pytest.fail('Invalid cleanup receipt must not reach systemd'))
    with pytest.raises(owner.OwnerError):
        ops.stop_new(receipt)


def test_cleanup_refuses_replaced_run_and_never_stops_other_preview(tmp_path, monkeypatch):
    ops, commands = owner.SystemOps(owner.Settings(private_dir=tmp_path / 'owner')), []
    def command(arguments, timeout=None):
        commands.append(tuple(arguments))
        return SimpleNamespace(returncode=0, stdout=unit_output(description='Robot camera web owner NEW_OTHER_RUN'), stderr='')
    monkeypatch.setattr(ops, 'command', command)
    with pytest.raises(owner.OwnerError):
        ops.stop_new(owner.StartReceipt(run_id='OLD_ATTEMPT'))
    assert commands and not any('stop' in arguments for arguments in commands)


def test_unknown_holder_appearing_during_start_cleans_only_new_unit(tmp_path):
    ops = FakeOps()
    ops.ready = snapshot(active=(owner.NEW_WEB_UNIT,), healthy=True,
        holders=(owner.Holder(pid=994, uid=1000, nodes=('/dev/video0',), unit=None),))
    with pytest.raises(owner.OwnerError):
        owner.Engine(ops).execute('web')
    assert [item for item in ops.mutations if item[0] == 'stop_new'] == [('stop_new', ops.receipt)]
    assert not any(item[0] == 'save_state' for item in ops.mutations)


def test_operator_only_commands_never_expose_process_signal_or_ros_navigation_serial_actions():
    source = Path(owner.__file__).read_text()
    assert 'os.kill' not in source and 'killpg' not in source
    assert "['fuser', '--'," in source and "'fuser', '-k'" not in source
    assert 'create_publisher' not in source and 'create_client' not in source and 'ActionClient' not in source
    assert 'rclpy' not in source and 'serial.Serial' not in source


def exec_record(executable, arguments):
    return '{ path=' + executable + ' ; argv[]=' + ' '.join(arguments) + ' ; ignore_errors=no ; start_time=[n/a] ; }'


@pytest.mark.parametrize('name,executable,arguments', [
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '127.0.0.1', '--allow-client', '127.0.0.1']),
    ('yolo-native-direct-preview.service', '/usr/bin/bash', ['/usr/bin/bash', owner.LAUNCHER, '--host', '127.0.0.1', '--allow-client', '127.0.0.1']),
    ('yolo-native-direct-preview.service', '/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '10.48.106.37', '--allow-client', '10.48.106.134']),
    ('robot-cloud-lowres-preview.service', '/usr/bin/python3', ['/usr/bin/python3', '-B', '/home/nvidia/.local/share/robot-cloud/usb_camera_preview.py']),
    ('robot-cloud-lowres-preview.service', '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/usb_camera_preview.py']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '-B', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '-B', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--keep-alive']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--preview-size', '320x240', '--keep-alive']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--keep-alive', '--preview-size', '640x480']),
])
def test_trusted_exec_start_accepts_only_fixed_program_and_preview_arguments(name, executable, arguments):
    assert owner.trusted_exec_start(name, exec_record(executable, arguments))


@pytest.mark.parametrize('name,executable,arguments', [
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', '-c', 'echo', owner.LAUNCHER]),
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', '/isolated/calibration.sh', '--template', owner.LAUNCHER]),
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '0.0.0.0', '--allow-client', '127.0.0.1']),
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '127.0.0.1', '--host', '127.0.0.1', '--allow-client', '127.0.0.1']),
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '127.0.0.1', '--allow-client', '127.0.0.1', ';', 'unrelated']),
    (owner.NEW_WEB_UNIT, '/bin/bash', ['/bin/bash', owner.LAUNCHER + '.backup', '--host', '127.0.0.1', '--allow-client', '127.0.0.1']),
    ('robot-cloud-lowres-preview.service', '/usr/bin/python3', ['/usr/bin/python3', '-c', 'print("camera")', '/home/nvidia/.local/share/robot-cloud/usb_camera_preview.py']),
    ('robot-cloud-lowres-preview.service', '/usr/bin/python3', ['/usr/bin/python3', '/isolated/calibration.py', '/home/nvidia/.local/share/robot-cloud/usb_camera_preview.py']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--preview-size', '1920x1080']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--keep-alive', '--keep-alive']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--preview-size', '320x240', '--preview-size', '640x480']),
    (owner.RELAY_UNIT, '/usr/bin/python3', ['/usr/bin/python3', '/home/nvidia/.local/share/robot-cloud/http_camera_relay.py', '--url', 'http://unsafe.invalid']),
])
def test_trusted_exec_start_rejects_shell_injection_calibration_and_nonfixed_arguments(name, executable, arguments):
    assert not owner.trusted_exec_start(name, exec_record(executable, arguments))


def test_trusted_exec_start_rejects_multiple_commands_weak_path_and_non_string_input():
    raw = exec_record('/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', '127.0.0.1', '--allow-client', '127.0.0.1'])
    assert not owner.trusted_exec_start(owner.NEW_WEB_UNIT, raw + ' ' + raw)
    assert not owner.trusted_exec_start(owner.NEW_WEB_UNIT, raw.replace('path=/bin/bash', 'path=/usr/bin/python3'))
    assert not owner.trusted_exec_start('unrelated-calibration.service', raw)
    assert not owner.trusted_exec_start(owner.NEW_WEB_UNIT, None)
    assert not owner.trusted_exec_start(owner.NEW_WEB_UNIT, raw.replace('; ignore_errors=no', '; arbitrary_command'))


@pytest.mark.parametrize('address', ['unsafe.invalid', '127.0.0.1;unsafe', '999.0.0.1', '10.48.106.37/24',
                                     'fe80::1%eth0', 'http://10.48.106.37', '10.48.106.37:8080'])
def test_old_preview_only_accepts_numeric_ip_without_domain_zone_or_extra_syntax(address):
    raw = exec_record('/bin/bash', ['/bin/bash', owner.LAUNCHER, '--host', address, '--allow-client', '10.48.106.134'])
    assert not owner.trusted_exec_start('yolo-native-direct-preview.service', raw)


def test_real_lock_rejects_symlink_prefix_and_insecure_existing_file(tmp_path):
    target = tmp_path / 'other-owner'
    target.mkdir()
    prefix = tmp_path / 'prefix'
    prefix.symlink_to(target, target_is_directory=True)
    ops = owner.SystemOps(owner.Settings(private_dir=prefix / 'owner'))
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('A symlink prefix must not redirect creation into another owner directory')
    assert list(target.iterdir()) == []
    private = tmp_path / 'safe-owner'
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    with ops.lock():
        pass
    lock = next(private.iterdir())
    lock.chmod(0o644)
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('An existing public lock file must not be silently chmod-ed')
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


def test_real_lock_rejects_symlink_file_without_touching_other_owner_data(tmp_path):
    private = tmp_path / 'owner'
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    with ops.lock():
        pass
    lock = next(private.iterdir())
    lock.unlink()
    foreign = tmp_path / 'calibration-results-placeholder'
    foreign.write_bytes(b'ISOLATED_KEEP_CALIBRATION_RESULTS')
    lock.symlink_to(foreign)
    before = foreign.read_bytes(), stat.S_IMODE(foreign.stat().st_mode)
    with pytest.raises(owner.OwnerError):
        with ops.lock():
            pytest.fail('A symlink lock must not follow into calibration or other data')
    assert foreign.read_bytes() == before[0] and stat.S_IMODE(foreign.stat().st_mode) == before[1]
    assert lock.is_symlink()


def test_state_needs_lock_refuses_symlink_and_atomically_replaces_its_own_valid_file(tmp_path):
    private = tmp_path / 'owner'
    ops = owner.SystemOps(owner.Settings(private_dir=private))
    with pytest.raises(owner.OwnerError):
        ops.save_state('peer', snapshot())
    assert not private.exists()
    with ops.lock():
        foreign = tmp_path / 'unrelated-placeholder'
        foreign.write_bytes(b'ISOLATED_OTHER_OWNER_DATA')
        (private / 'state.json').symlink_to(foreign)
        with pytest.raises(owner.OwnerError):
            ops.save_state('peer', snapshot())
        assert foreign.read_bytes() == b'ISOLATED_OTHER_OWNER_DATA'
        assert (private / 'state.json').is_symlink()
        (private / 'state.json').unlink()
        (private / 'state.json').write_text('{"requestedMode":"web"}')
        (private / 'state.json').chmod(0o600)
        before_inode = (private / 'state.json').stat().st_ino
        ops.save_state('peer', snapshot())
        assert foreign.read_bytes() == b'ISOLATED_OTHER_OWNER_DATA'
        assert not (private / 'state.json').is_symlink()
        assert stat.S_IMODE((private / 'state.json').stat().st_mode) == 0o600
        assert (private / 'state.json').stat().st_ino != before_inode
        assert json.loads((private / 'state.json').read_text())['requestedMode'] == 'peer'
        assert not list(private.glob('state-*.tmp'))


def test_proc_fd_scope_identifies_known_children_and_unknown_gui_without_signals(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    char_paths = set()
    cgroup = '/isolated/' + owner.NEW_WEB_UNIT
    for pid, group in ((301, cgroup + '/encoder'), (401, '/isolated/calibration-gui.service'),
                       (402, cgroup + '-lookalike')):
        process = proc / str(pid)
        (process / 'fd').mkdir(parents=True)
        (process / 'cgroup').write_text('0::' + group + '\n')
        fd = process / 'fd' / '3'
        fd.write_bytes(b'ISOLATED_FAKE_VIDEO_FD')
        char_paths.add(str(fd))
    real_stat = owner.Path.stat
    def fd_stat(path, *args, **kwargs):
        if str(path) in char_paths:
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_rdev=os.makedev(81, 0))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(owner.Path, 'stat', fd_stat)
    ops, commands = owner.SystemOps(owner.Settings(proc_root=str(proc))), []
    monkeypatch.setattr(ops, 'command', lambda arguments, timeout=None:
        commands.append(tuple(arguments)) or SimpleNamespace(returncode=1, stdout='', stderr=''))
    units = {owner.NEW_WEB_UNIT: owner.Unit(owner.NEW_WEB_UNIT, active=True, cgroup=cgroup)}
    holders = ops.holders({os.makedev(81, 0): '/dev/video0'}, units)
    assert {holder.pid for holder in holders} == {301, 401, 402}
    assert next(holder for holder in holders if holder.pid == 301).unit == owner.NEW_WEB_UNIT
    assert all(holder.unit is None for holder in holders if holder.pid != 301)
    assert commands == [('fuser', '--', '/dev/video0')]
    fake = FakeOps(snapshot(active=(owner.NEW_WEB_UNIT,), holders=holders))
    with pytest.raises(owner.OwnerError):
        owner.Engine(fake).execute('web')
    assert fake.mutations == []


def test_unreadable_fuser_evidence_is_unknown_and_blocks_web(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    ops = owner.SystemOps(owner.Settings(proc_root=str(proc)))
    monkeypatch.setattr(ops, 'command', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=' 992 ', stderr=''))
    holders = ops.holders({os.makedev(81, 1): '/dev/video1'}, {})
    assert len(holders) == 1 and holders[0].pid == 992 and holders[0].uid == -1 and holders[0].unreadable
    assert holders[0].unit is None and holders[0].nodes == ('/dev/video1',)
    fake = FakeOps(snapshot(holders=holders))
    with pytest.raises(owner.OwnerError):
        owner.Engine(fake).execute('web')
    assert fake.mutations == []


def test_fake_sysfs_scope_includes_secondary_nodes_only_on_the_four_fixed_usb_devices(tmp_path):
    by_path, devices, sys_video, usb = (tmp_path / name for name in ('by-path', 'devices', 'video-sys', 'usb-sys'))
    for directory in (by_path, devices, sys_video, usb):
        directory.mkdir()
    for index, port in enumerate(owner.PORTS):
        physical = usb / ('1-' + port)
        interface = physical / 'interface-0'
        interface.mkdir(parents=True)
        (physical / 'idVendor').write_text('0bda')
        (physical / 'idProduct').write_text('5187' if index < 2 else 'd584')
        for number in (index * 2, index * 2 + 1):
            video = devices / ('video' + str(number))
            video.write_bytes(b'ISOLATED_FAKE_DEVICE_NOT_OPENED')
            entry = sys_video / video.name
            entry.mkdir()
            (entry / 'device').symlink_to(interface, target_is_directory=True)
            (entry / 'dev').write_text('81:' + str(number) + '\n')
        anchor = by_path / ('platform-3610000.usb-usb-0:' + port + ':1.0-video-index0')
        anchor.symlink_to(devices / ('video' + str(index * 2)))
    unrelated = usb / 'unrelated' / 'interface-0'
    unrelated.mkdir(parents=True)
    entry = sys_video / 'video90'
    entry.mkdir()
    (entry / 'device').symlink_to(unrelated, target_is_directory=True)
    (entry / 'dev').write_text('81:90\n')
    ops = owner.SystemOps(owner.Settings(dev_by_path_root=str(by_path), sys_video_root=str(sys_video)))
    nodes, missing = ops.camera_nodes()
    assert missing == []
    assert nodes == {os.makedev(81, number): '/dev/video' + str(number) for number in range(8)}
    assert os.makedev(81, 90) not in nodes
    (by_path / ('platform-3610000.usb-usb-0:' + owner.PORTS[0] + ':1.0-video-index0')).unlink()
    _, missing = ops.camera_nodes()
    assert missing == [owner.PORTS[0]]
