"""Power handshake unit tests: all subprocess, input, time and UID are mocked."""
import io
import json
from types import SimpleNamespace

import pytest

from collector.robot_shutdown_power import ROOT_HELPER_SOURCE, root_command


NONCE = 'a1' * 32
BOOT = '12345678-1234-1234-1234-123456789abc'
OTHER_BOOT = '87654321-1234-1234-1234-123456789abc'


def wire(phase='commit', *, nonce=NONCE, boot=BOOT, **extra):
    return json.dumps(dict(phase=phase, nonce=nonce, bootId=boot, **extra))


class Fixture:
    def __init__(self):
        namespace = {'__name__': 'fixture_helper'}
        exec(compile(ROOT_HELPER_SOURCE, '<fixed-power-helper>', 'exec'), namespace)
        self.main = namespace['_helper_main']
        self.now = 0.0
        self.uid = 0
        self.boots = [BOOT]
        self.lines = [wire(), wire('release')]
        self.preparing = [False, False, True]
        self.scheduled = ['(st) "" 0']
        self.read_timeouts = []
        self.readonly = []
        self.requests = []
        self.events = []
        self.client_status = None
        self.output = io.StringIO()
        self.run_hook = None
        self.popen_error = None

    @staticmethod
    def consume(values):
        return values.pop(0) if len(values) > 1 else values[0]

    def reader(self, timeout):
        self.read_timeouts.append(timeout)
        self.events.append('read')
        item = self.lines.pop(0) if self.lines else None
        if isinstance(item, Exception):
            self.now += timeout
            raise item
        return item

    def run(self, command, **options):
        assert command[:3] == ['/usr/bin/busctl', '--system', 'get-property']
        assert command[3:6] == ['org.freedesktop.login1', '/org/freedesktop/login1',
                                 'org.freedesktop.login1.Manager']
        assert options['capture_output'] is True
        assert options['text'] is True
        assert 0 < options['timeout'] <= 3
        self.readonly.append(command)
        prop = command[-1]
        self.events.append(prop)
        if self.run_hook:
            result = self.run_hook(prop)
            if result is not None:
                return result
        if prop == 'PreparingForShutdown':
            value = self.consume(self.preparing)
            text = 'b ' + str(value).lower() if type(value) is bool else value
        else:
            assert prop == 'ScheduledShutdown'
            text = self.consume(self.scheduled)
        return SimpleNamespace(returncode=0, stdout=text, stderr='')

    def popen(self, command, **options):
        self.events.append('PowerOff')
        self.requests.append(command)
        assert command == ['/usr/bin/busctl', '--system', 'call',
            'org.freedesktop.login1', '/org/freedesktop/login1',
            'org.freedesktop.login1.Manager', 'PowerOff', 'b', 'false']
        assert options['close_fds'] is True
        if self.popen_error:
            raise self.popen_error
        return SimpleNamespace(poll=lambda: self.client_status)

    def sleep(self, duration):
        self.now += duration

    def execute(self, nonce=NONCE):
        return self.main(nonce, reader=self.reader, output=self.output, run=self.run,
            popen=self.popen, clock=lambda: self.now, sleep=self.sleep,
            boot_reader=lambda: self.consume(self.boots), geteuid=lambda: self.uid)

    @property
    def messages(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]


def test_command_has_fixed_isolated_helper_and_separate_nonce():
    command = root_command(NONCE)
    assert command[:3] == ['/usr/bin/sudo', '-n', '/usr/bin/systemd-inhibit']
    assert '--what=shutdown' in command and '--mode=delay' in command
    assert '--who=robot-safe-shutdown' in command
    assert command[-6:] == ['/usr/bin/python3', '-I', '-u', '-c', ROOT_HELPER_SOURCE, NONCE]
    assert NONCE not in ROOT_HELPER_SOURCE


@pytest.mark.parametrize('nonce', ['', 'a'*63, 'a'*65, 'A'*64, 'g'*64, 'a'*64+'; reboot', None, 42])
def test_invalid_nonce_rejected_without_command(nonce):
    with pytest.raises(ValueError):
        root_command(nonce)


def test_accepted_poweroff_requires_positive_state_before_ack():
    fixture = Fixture()
    fixture.preparing = [False, False, False, False, True]
    assert fixture.execute() == 0
    assert [item['phase'] for item in fixture.messages] == ['ready', 'shutdown_requested']
    assert all(item['nonce'] == NONCE and item['bootId'] == BOOT for item in fixture.messages)
    assert fixture.read_timeouts == [600, 1]
    assert fixture.events.index('PowerOff') > fixture.events.index('read')
    assert len(fixture.requests) == 1
    assert fixture.now >= 0.2


@pytest.mark.parametrize('line', [None, wire('cancel'), TimeoutError('timeout')])
def test_no_commit_never_sends_poweroff(line):
    fixture = Fixture()
    fixture.lines = [line]
    assert fixture.execute() == 0
    assert fixture.requests == []
    assert fixture.messages[-1]['phase'] == 'cancelled'


@pytest.mark.parametrize('line', [wire(nonce='b'*64), wire(boot=OTHER_BOOT), wire('release'),
    wire(extra=True), '{}', '[]', '{bad json', 'x'*4097])
def test_wrong_or_malformed_commit_never_sends_poweroff(line):
    fixture = Fixture()
    fixture.lines = [line]
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.messages[-1]['phase'] == 'error'


@pytest.mark.parametrize('phase', ['initial', 'commit'])
def test_already_preparing_refuses_request(phase):
    fixture = Fixture()
    fixture.preparing = [True] if phase == 'initial' else [False, True]
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.messages[-1]['error'] == 'shutdown_already_in_progress'


@pytest.mark.parametrize('scheduled', ['(st) "poweroff" 1234', '(st) "reboot" 1234',
    '(st) "" 100', '(st) "poweroff" 0', 'garbage', '(st) "" -1'])
def test_scheduled_shutdown_refuses_request(scheduled):
    fixture = Fixture()
    fixture.scheduled = [scheduled]
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.messages[-1]['error'] == 'shutdown_already_scheduled'


def test_new_schedule_at_commit_refuses_request():
    fixture = Fixture()
    fixture.scheduled = ['(st) "" 0', '(st) "poweroff" 999']
    assert fixture.execute() == 1
    assert fixture.requests == []


def test_changed_boot_refuses_request():
    fixture = Fixture()
    fixture.boots = [BOOT, OTHER_BOOT]
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.messages[-1]['error'] == 'boot_identity_changed'


def test_zero_client_status_without_positive_state_is_not_ack():
    fixture = Fixture()
    fixture.preparing = [False]
    fixture.client_status = 0
    assert fixture.execute() == 1
    assert fixture.now >= 20
    assert [item['phase'] for item in fixture.messages] == ['ready', 'error']
    assert fixture.messages[-1]['error'] == 'shutdown_not_confirmed'
    assert fixture.messages[-1]['requestSubmitted'] is True


def test_rejected_client_is_not_ack():
    fixture = Fixture()
    fixture.client_status = 1
    assert fixture.execute() == 1
    assert [item['phase'] for item in fixture.messages] == ['ready', 'error']
    assert fixture.messages[-1]['error'] == 'poweroff_request_rejected'


@pytest.mark.parametrize('prop', ['PreparingForShutdown', 'ScheduledShutdown'])
def test_state_query_failure_never_commits(prop):
    fixture = Fixture()
    fixture.run_hook = lambda current: SimpleNamespace(returncode=1, stdout='', stderr='private') if current == prop else None
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert 'private' not in fixture.output.getvalue()


@pytest.mark.parametrize('value', ['', 'garbage', 'b 1', 'b true false', 'b false\nprivate'])
def test_invalid_preparing_is_fail_closed(value):
    fixture = Fixture()
    fixture.preparing = [value]
    assert fixture.execute() == 1
    assert fixture.requests == []


@pytest.mark.parametrize('boot', ['', BOOT.upper(), 'not-a-boot', 'a'*64])
def test_invalid_boot_is_fail_closed(boot):
    fixture = Fixture()
    fixture.boots = [boot]
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.readonly == []


def test_unprivileged_helper_never_requests_power():
    fixture = Fixture()
    fixture.uid = 1000
    assert fixture.execute() == 1
    assert fixture.requests == []
    assert fixture.readonly == []
    assert fixture.messages[-1]['error'] == 'root_required'


@pytest.mark.parametrize('last', [None, TimeoutError('release timeout')])
def test_ack_release_eof_or_timeout_exits_without_second_request(last):
    fixture = Fixture()
    fixture.lines = [wire(), last]
    assert fixture.execute() == 0
    assert len(fixture.requests) == 1
    assert fixture.messages[-1]['phase'] == 'shutdown_requested'


def test_popen_failure_reports_request_not_submitted():
    fixture = Fixture()
    fixture.popen_error = OSError('private syscall details')
    assert fixture.execute() == 1
    assert fixture.messages[-1]['requestSubmitted'] is False
    assert fixture.messages[-1]['error'] == 'OSError'
    assert 'private' not in fixture.output.getvalue()


def test_line_reader_reassembles_bounded_frames_and_eof(monkeypatch):
    namespace = {'__name__': 'fixture_helper'}
    exec(compile(ROOT_HELPER_SOURCE, '<fixed-power-helper>', 'exec'), namespace)
    chunks = [b'one', b'\ntwo\n', b'']
    monkeypatch.setattr(namespace['select'], 'select', lambda *args: ([17], [], []))
    monkeypatch.setattr(namespace['os'], 'read', lambda *_: chunks.pop(0))
    reader = namespace['_LineReader'](SimpleNamespace(fileno=lambda: 17), lambda: 0)
    assert reader(1) == 'one'
    assert reader(1) == 'two'
    assert reader(1) is None


def test_line_reader_rejects_large_frame_without_blocking(monkeypatch):
    namespace = {'__name__': 'fixture_helper'}
    exec(compile(ROOT_HELPER_SOURCE, '<fixed-power-helper>', 'exec'), namespace)
    monkeypatch.setattr(namespace['select'], 'select', lambda *args: ([17], [], []))
    monkeypatch.setattr(namespace['os'], 'read', lambda *_: b'x'*4097)
    reader = namespace['_LineReader'](SimpleNamespace(fileno=lambda: 17), lambda: 0)
    with pytest.raises(ValueError, match='input_too_large'):
        reader(1)
