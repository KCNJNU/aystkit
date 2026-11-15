"""Coordinator wire checks; fake pipes only, never host or power operations."""
import io
import json
from types import SimpleNamespace

import pytest

from collector import robot_shutdown as shutdown


NONCE = 'ca' * 32
BOOT = '12345678-1234-1234-1234-123456789abc'
OTHER_BOOT = 'abcdefab-1234-1234-1234-123456789abc'


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Protocol:
    def __init__(self, monkeypatch, chunks):
        self.chunks = list(chunks)
        self.clock = FakeClock()
        self.fd = 91
        self.process = SimpleNamespace(stdin=io.BytesIO(),
                                       stdout=SimpleNamespace(fileno=lambda: self.fd))
        monkeypatch.setattr(shutdown.time, 'monotonic', self.clock.monotonic)
        monkeypatch.setattr(shutdown.select, 'select', self.select)
        monkeypatch.setattr(shutdown.os, 'read', self.read)
        self.peer = shutdown.JsonPeer(self.process)

    def select(self, readable, writable, errors, timeout):
        assert readable == [self.fd]
        assert writable == errors == []
        assert 0 < timeout <= 1
        if self.chunks:
            return [self.fd], [], []
        self.clock.now += timeout
        return [], [], []

    def read(self, fd, count):
        assert fd == self.fd
        assert 0 < count <= 65536
        chunk = self.chunks.pop(0)
        assert len(chunk) <= count
        return chunk


def encoded(phase='ready', *, nonce=NONCE, boot=BOOT):
    return (json.dumps(dict(phase=phase, nonce=nonce, bootId=boot)) + '\n').encode()


def test_split_json_frame_and_retained_second_frame(monkeypatch):
    first, second = encoded(), encoded('shutdown_requested')
    protocol = Protocol(monkeypatch, [first[:3], first[3:-2], first[-2:] + second])
    assert protocol.peer.receive()['phase'] == 'ready'
    assert protocol.peer.receive()['phase'] == 'shutdown_requested'
    assert not protocol.chunks


def test_eof_is_not_ack_even_after_partial_json(monkeypatch):
    protocol = Protocol(monkeypatch, [encoded('shutdown_requested')[:-1], b''])
    with pytest.raises(shutdown.ShutdownError, match='连接已断开'):
        protocol.peer.receive()


def test_no_bytes_timeout_is_bounded(monkeypatch):
    protocol = Protocol(monkeypatch, [])
    with pytest.raises(shutdown.ShutdownError, match='超时'):
        protocol.peer.receive(timeout=2.5)
    assert protocol.clock.now == 2.5


@pytest.mark.parametrize('line', [b'[]\n', b'null\n', b'42\n', b'"ready"\n', b'{broken}\n', b'\xff\n'])
def test_invalid_json_or_nonobject_is_not_ack(monkeypatch, line):
    protocol = Protocol(monkeypatch, [line])
    with pytest.raises(shutdown.ShutdownError, match='无效响应'):
        protocol.peer.receive()


def test_complete_oversize_json_line_cannot_bypass_limit(monkeypatch):
    payload = (json.dumps({'text': 'x' * 262144}) + '\n').encode()
    chunks = [payload[start:start+65536] for start in range(0, len(payload), 65536)]
    protocol = Protocol(monkeypatch, chunks)
    with pytest.raises(shutdown.ShutdownError, match='响应过大'):
        protocol.peer.receive()


def test_unterminated_oversize_json_cannot_grow_forever(monkeypatch):
    protocol = Protocol(monkeypatch, [b'x'*65536] * 5)
    with pytest.raises(shutdown.ShutdownError, match='响应过大'):
        protocol.peer.receive()


@pytest.mark.parametrize('phase,nonce,boot', [
    ('ready', 'bb'*32, BOOT), ('shutdown_requested', NONCE, BOOT),
    ('ready', NONCE, OTHER_BOOT), ('ready', NONCE, '-'*36),
    ('ready', NONCE, '1'*36), ('ready', NONCE, BOOT.upper()),
])
def test_wrong_transaction_or_invalid_boot_not_ack(monkeypatch, phase, nonce, boot):
    protocol = Protocol(monkeypatch, [encoded(phase, nonce=nonce, boot=boot)])
    expected_boot = BOOT if boot in (BOOT, OTHER_BOOT) else None
    with pytest.raises(shutdown.ShutdownError, match='未获确认'):
        shutdown.expect(protocol.peer, 'ready', NONCE, expected_boot)


def test_valid_ack_requires_exact_boot_and_nonce(monkeypatch):
    protocol = Protocol(monkeypatch, [encoded('shutdown_requested')])
    value = shutdown.expect(protocol.peer, 'shutdown_requested', NONCE, BOOT)
    assert value == dict(phase='shutdown_requested', nonce=NONCE, bootId=BOOT)


def test_sender_writes_single_json_frame(monkeypatch):
    protocol = Protocol(monkeypatch, [])
    item = shutdown.transaction_message('commit', NONCE, BOOT)
    protocol.peer.send(item)
    raw = protocol.process.stdin.getvalue()
    assert raw.count(b'\n') == 1
    assert json.loads(raw) == item


def test_sender_rejects_nonfinite_json(monkeypatch):
    protocol = Protocol(monkeypatch, [])
    with pytest.raises(ValueError):
        protocol.peer.send({'value': float('nan')})
    assert protocol.process.stdin.getvalue() == b''


class CloseProcess:
    def __init__(self, timeouts):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.timeouts = timeouts
        self.waits = []
        self.terminated = self.killed = False

    def wait(self, timeout):
        self.waits.append(timeout)
        if len(self.waits) <= self.timeouts:
            raise shutdown.subprocess.TimeoutExpired('owned helper', timeout)
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.mark.parametrize('timeouts', [0, 1, 2])
def test_close_first_signals_eof_then_only_reaps_own_peer(timeouts):
    process = CloseProcess(timeouts)
    shutdown.JsonPeer(process).close()
    assert process.stdin.closed and process.stdout.closed
    assert process.terminated is (timeouts > 0)
    assert process.killed is (timeouts > 1)
    assert process.waits == [4, 2, 2][:timeouts+1]
