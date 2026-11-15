"""Coordinator protocol tests: every host, command and power helper is fake."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector import robot_shutdown as shutdown


NONCE = 'a' * 64
LOCAL_BOOT = '11111111-1111-1111-1111-111111111111'
REMOTE_BOOT = '22222222-2222-2222-2222-222222222222'


@pytest.fixture(autouse=True)
def prohibit_real_commands_and_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('shutdown tests must never execute real commands or sockets')
    monkeypatch.setattr(shutdown.subprocess, 'run', denied)
    monkeypatch.setattr(shutdown.subprocess, 'Popen', denied)
    monkeypatch.setattr(shutdown, 'hold_until_poweroff', denied)
    import socket
    monkeypatch.setattr(socket, 'socket', denied)


class Peer:
    def __init__(self, name, events, replies):
        self.name = name
        self.events = events
        self.replies = list(replies)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)
        self.events.append((self.name, 'send', value['phase']))

    def receive(self, timeout=30):
        self.events.append((self.name, 'receive'))
        if not self.replies:
            raise shutdown.ShutdownError('connection closed without ACK')
        value = self.replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True
        self.events.append((self.name, 'close'))


class Host:
    def __init__(self, events):
        self.events = events
        self.values = {}
        self.boot_id = LOCAL_BOOT
        self.state = Path('/never-created-by-tests')
        self.fail_finish = False

    def lock_capture(self, stack):
        self.events.append(('local', 'lock'))

    def close_gates(self):
        self.events.append(('local', 'gates_closed'))

    def stop_unit(self, unit, *, timeout):
        self.events.append(('local', 'stop_unit', unit))
        assert unit == shutdown.SOURCE_LINK

    def finish_native(self):
        self.events.append(('local', 'locally_saved'))
        if self.fail_finish:
            raise shutdown.ShutdownError('local writer did not drain')
        return {'state': 'locally_saved', 'pendingUpload': 12}

    def snapshot(self):
        self.events.append(('local', 'readonly_snapshot'))
        return {'bootId': self.boot_id, 'pendingUpload': 12}


def message(phase, boot=REMOTE_BOOT, nonce=NONCE, **fields):
    return dict(phase=phase, nonce=nonce, bootId=boot, **fields)


def setup_order(*, remote_ack=None, local_ack=None, movements=('UNKNOWN', 'UNKNOWN')):
    events = []
    local = Host(events)
    remote = Peer('remote', events, [message('sealed', saved={'state': 'locally_saved'}),
                                     remote_ack or message('shutdown_requested')])
    power = Peer('ipc_power', events, [local_ack or message('shutdown_requested', LOCAL_BOOT)])
    states = iter(movements)
    def motion():
        state = next(states)
        events.append(('motion', state))
        return {'state': state}
    def record(**fields):
        events.append(('record', fields['state']))
    def execute():
        with ExitStack() as stack:
            shutdown.execute_order(local, remote, power, nonce=NONCE, local_boot=LOCAL_BOOT,
                                   remote_boot=REMOTE_BOOT, stack=stack, motion=motion, record=record)
    return events, local, remote, power, execute


def test_files_are_saved_before_jetson_commit_and_its_ack_precedes_ipc_commit():
    events, _, remote, power, execute = setup_order()
    execute()
    assert events.index(('local', 'gates_closed')) < events.index(('remote', 'send', 'seal'))
    assert events.index(('local', 'stop_unit', shutdown.SOURCE_LINK)) < events.index(('remote', 'send', 'seal'))
    assert events.index(('local', 'locally_saved')) < events.index(('remote', 'send', 'commit'))
    assert events.index(('record', 'locally_saved')) < events.index(('remote', 'send', 'commit'))
    assert events.index(('remote', 'send', 'commit')) < events.index(('record', 'jetson_shutdown_requested'))
    assert events.index(('record', 'jetson_shutdown_requested')) < events.index(('ipc_power', 'send', 'commit'))
    assert events.index(('ipc_power', 'send', 'commit')) < events.index(('record', 'both_shutdown_requested'))
    assert [item['phase'] for item in remote.sent] == ['seal', 'commit', 'release']
    assert [item['phase'] for item in power.sent] == ['commit', 'release']
    assert all(item['nonce'] == NONCE and item['bootId'] == REMOTE_BOOT for item in remote.sent)
    assert all(item['nonce'] == NONCE and item['bootId'] == LOCAL_BOOT for item in power.sent)


@pytest.mark.parametrize('ack', [
    message('error', reason='logind rejected shutdown'),
    message('shutdown_requested', nonce='b' * 64),
    message('shutdown_requested', boot=LOCAL_BOOT),
    message('shutdown_requested', boot='not-a-boot-id'),
    message('authorized'),
    shutdown.ShutdownError('EOF before shutdown ACK'),
    BrokenPipeError('SSH disconnected'),
])
def test_remote_missing_or_invalid_shutdown_ack_never_commits_ipc(ack):
    events, _, _, power, execute = setup_order(remote_ack=ack)
    with pytest.raises((shutdown.ShutdownError, BrokenPipeError)):
        execute()
    assert ('remote', 'send', 'commit') in events
    assert power.sent == []
    assert ('record', 'jetson_shutdown_requested') not in events


def test_ssh_exit_zero_with_eof_is_not_a_shutdown_ack(monkeypatch):
    events, _, remote, power, execute = setup_order()
    original_receive = remote.receive
    peer = shutdown.JsonPeer(SimpleNamespace(returncode=0, stdout=SimpleNamespace(fileno=lambda: 999)))
    monkeypatch.setattr(shutdown.select, 'select', lambda *args: ([999], [], []))
    monkeypatch.setattr(shutdown.os, 'read', lambda *args: b'')
    def receive(timeout=30):
        if len(remote.replies) == 2:
            return original_receive(timeout)
        return peer.receive(timeout)
    remote.receive = receive
    with pytest.raises(shutdown.ShutdownError, match='连接已断开'):
        execute()
    assert power.sent == []
    assert ('record', 'jetson_shutdown_requested') not in events


def test_unconfirmed_remote_save_never_commits_either_host():
    _, _, remote, power, execute = setup_order()
    remote.replies[0] = message('error', reason='camera writer still active')
    with pytest.raises(shutdown.ShutdownError):
        execute()
    assert [item['phase'] for item in remote.sent] == ['seal']
    assert power.sent == []


def test_failed_local_save_never_commits_either_host():
    _, local, remote, power, execute = setup_order()
    local.fail_finish = True
    with pytest.raises(shutdown.ShutdownError, match='did not drain'):
        execute()
    assert [item['phase'] for item in remote.sent] == ['seal']
    assert power.sent == []


def test_detected_motion_before_sealing_has_no_mutating_side_effects():
    events, _, remote, power, execute = setup_order(movements=('MOVING',))
    with pytest.raises(shutdown.ShutdownError, match='仍在运动'):
        execute()
    assert events == [('motion', 'MOVING')]
    assert remote.sent == power.sent == []


def test_detected_motion_after_saving_never_commits_power():
    events, _, remote, power, execute = setup_order(movements=('STATIONARY', 'MOVING'))
    with pytest.raises(shutdown.ShutdownError, match='未提交关机'):
        execute()
    assert ('local', 'locally_saved') in events
    assert [item['phase'] for item in remote.sent] == ['seal']
    assert power.sent == []


def test_invalid_ipc_ack_does_not_release_ipc_or_claim_both_shutdown_requested():
    events, _, _, power, execute = setup_order(local_ack=message('error', LOCAL_BOOT))
    with pytest.raises(shutdown.ShutdownError):
        execute()
    assert [item['phase'] for item in power.sent] == ['commit']
    assert ('record', 'both_shutdown_requested') not in events


def test_ipc_ack_marks_commit_before_log_error_and_holds_before_unlock(monkeypatch):
    events, local, remote, power, _ = setup_order()
    lifecycle = {}
    held = []

    @contextmanager
    def capture_lock():
        held.append(True)
        events.append(('local', 'lock_enter'))
        try:
            yield
        finally:
            held.pop()
            events.append(('local', 'lock_release'))

    def lock_capture(stack):
        stack.enter_context(capture_lock())

    def hold():
        assert held, 'startup/capture locks must still be held after shutdown ACK'
        assert lifecycle.get('localCommitted') is True
        events.append(('local', 'hold_until_poweroff'))

    def record(**fields):
        events.append(('record', fields['state']))
        if fields['state'] == 'both_shutdown_requested':
            assert lifecycle.get('localCommitted') is True
            raise OSError('shutdown record disk write failed')

    local.lock_capture = lock_capture
    monkeypatch.setattr(shutdown, 'hold_until_poweroff', hold)
    with pytest.raises(OSError, match='disk write failed'):
        with ExitStack() as stack:
            shutdown.execute_order(local, remote, power, nonce=NONCE,
                                   local_boot=LOCAL_BOOT, remote_boot=REMOTE_BOOT,
                                   stack=stack, record=record,
                                   motion=lambda: {'state': 'UNKNOWN'}, lifecycle=lifecycle)
    assert lifecycle['localCommitted'] is True
    assert events.index(('record', 'both_shutdown_requested')) < events.index(('local', 'hold_until_poweroff'))
    assert events.index(('local', 'hold_until_poweroff')) < events.index(('local', 'lock_release'))
    assert [item['phase'] for item in power.sent] == ['commit']


def test_remote_release_broken_pipe_after_positive_ack_still_commits_ipc():
    events, _, remote, power, execute = setup_order()
    original_send = remote.send
    def send(value):
        original_send(value)
        if value['phase'] == 'release':
            raise BrokenPipeError('Jetson stopped SSH after its accepted shutdown')
    remote.send = send
    execute()
    assert [item['phase'] for item in power.sent] == ['commit', 'release']
    assert events.index(('record', 'jetson_shutdown_requested')) < events.index(('ipc_power', 'send', 'commit'))
    assert ('record', 'both_shutdown_requested') in events


def test_ipc_release_broken_pipe_after_positive_ack_does_not_report_failure():
    events, _, _, power, execute = setup_order()
    original_send = power.send
    def send(value):
        original_send(value)
        if value['phase'] == 'release':
            raise BrokenPipeError('IPC helper exited after its accepted shutdown')
    power.send = send
    execute()
    assert [item['phase'] for item in power.sent] == ['commit', 'release']
    assert ('record', 'both_shutdown_requested') in events


def test_failed_ipc_ack_does_not_hold_or_mark_committed(monkeypatch):
    _, local, remote, power, _ = setup_order(local_ack=message('error', LOCAL_BOOT))
    lifecycle = {}
    holds = []
    monkeypatch.setattr(shutdown, 'hold_until_poweroff', lambda: holds.append(True))
    with pytest.raises(shutdown.ShutdownError):
        with ExitStack() as stack:
            shutdown.execute_order(local, remote, power, nonce=NONCE,
                                   local_boot=LOCAL_BOOT, remote_boot=REMOTE_BOOT,
                                   stack=stack, motion=lambda: {'state': 'UNKNOWN'},
                                   lifecycle=lifecycle)
    assert not lifecycle.get('localCommitted')
    assert holds == []


def main_environment(monkeypatch, *, movement='UNKNOWN', tty=True):
    events = []
    host = Host(events)
    remote = Peer('remote', events, [message('hello', snapshot={'bootId': REMOTE_BOOT}),
                                     message('checked')])
    monkeypatch.setattr(shutdown.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(shutdown.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(shutdown.sys.stdin, 'isatty', lambda: tty)
    monkeypatch.setattr(shutdown.sys.stdout, 'isatty', lambda: tty)
    monkeypatch.setattr(shutdown, 'Host', lambda: host)
    monkeypatch.setattr(shutdown, 'uuid4', lambda: SimpleNamespace(hex='a' * 32))
    monkeypatch.setattr(shutdown, 'ssh_options', lambda values: (['fake-ssh'], 'private-jetson'))
    def spawn(command, stderr):
        events.append(('spawn', command[0]))
        assert command[0] == 'fake-ssh', 'must not authorize a root helper before confirmation'
        return remote
    monkeypatch.setattr(shutdown, 'spawn_peer', spawn)
    monkeypatch.setattr(shutdown, 'probe_motion', lambda: {'state': movement})
    return events, host, remote


@pytest.mark.parametrize('movement', ['UNKNOWN', 'STATIONARY'])
def test_cancelled_physical_confirmation_does_not_authorize_or_stop_anything(monkeypatch, movement):
    events, _, remote = main_environment(monkeypatch, movement=movement)
    prompts = []
    def cancelled(prompt):
        prompts.append(prompt)
        return '取消'
    monkeypatch.setattr('builtins.input', cancelled)
    assert shutdown.main([]) == 0
    assert prompts and '已停稳并关机' in prompts[0]
    assert remote.sent == []
    assert events == [('spawn', 'fake-ssh'), ('remote', 'receive'), ('remote', 'close')]


@pytest.mark.parametrize('exception,exit_code', [(EOFError, 130), (KeyboardInterrupt, 130)])
def test_confirmation_eof_or_interrupt_has_no_mutating_side_effects(monkeypatch, exception, exit_code):
    events, _, remote = main_environment(monkeypatch)
    def cancelled(prompt):
        raise exception()
    monkeypatch.setattr('builtins.input', cancelled)
    assert shutdown.main([]) == exit_code
    assert remote.sent == []
    assert events == [('spawn', 'fake-ssh'), ('remote', 'receive'), ('remote', 'close')]


def test_moving_cannot_be_overridden_by_a_confirmation(monkeypatch):
    events, _, remote = main_environment(monkeypatch, movement='MOVING')
    def forbidden_prompt(prompt):
        raise AssertionError('moving must be rejected before asking for confirmation')
    monkeypatch.setattr('builtins.input', forbidden_prompt)
    assert shutdown.main([]) == 2
    assert remote.sent == []
    assert events == [('spawn', 'fake-ssh'), ('remote', 'receive'), ('remote', 'close')]


def test_non_tty_execution_does_not_connect_authorize_or_touch_state(monkeypatch):
    events, _, remote = main_environment(monkeypatch, tty=False)
    def forbidden_host():
        raise AssertionError('noninteractive execution must fail before host inspection')
    monkeypatch.setattr(shutdown, 'Host', forbidden_host)
    assert shutdown.main([]) == 2
    assert events == []
    assert not remote.closed


def test_dry_run_does_not_inspect_hosts_or_execute_any_command(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('dry-run must have no host/file/network/command side effects')
    for name in ('Host', 'spawn_peer', 'probe_motion', 'atomic_json', 'safe_directory', 'file_lock'):
        monkeypatch.setattr(shutdown, name, forbidden)
    monkeypatch.setattr(shutdown.os, 'geteuid', forbidden)
    monkeypatch.setattr(shutdown.platform, 'machine', forbidden)
    assert shutdown.main(['--dry-run']) == 0


def test_readonly_check_never_authorizes_or_seals(monkeypatch):
    events, _, remote = main_environment(monkeypatch, tty=False)
    assert shutdown.main(['--check']) == 0
    assert [item['phase'] for item in remote.sent] == ['check']
    assert ('local', 'readonly_snapshot') in events
    assert all(item[0] != 'record' for item in events)
    assert remote.closed
