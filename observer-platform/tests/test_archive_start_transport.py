"""Use synthetic Python helpers only; never start physical devices."""
import struct
import subprocess
import sys
import time

import pytest

from collector import archive_start_transport as transport


def command(*args, fast=True):
    bootstrap = transport.REMOTE_BOOTSTRAP
    if fast:
        bootstrap = bootstrap.replace('HEARTBEAT_TIMEOUT = 10.0', 'HEARTBEAT_TIMEOUT = .4')
    return [sys.executable, '-B', '-c', bootstrap, *args]


def test_normal_startup_preserves_argv_and_binary_output():
    result = transport.run_remote_startup(command('jetson', '--recording-only'),
        payload=b'import sys; print(sys.argv[1:]); sys.stderr.buffer.write(b"\\xff")', timeout=2)
    assert result.returncode == 0
    assert result.stdout == b"['jetson', '--recording-only']\n"
    assert result.stderr == b'\xff'


def test_heartbeat_keeps_long_startup_alive(monkeypatch):
    monkeypatch.setattr(transport, 'HEARTBEAT_INTERVAL', .08)
    result = transport.run_remote_startup(command(),
        payload=b'import time; time.sleep(.9); print("finished")', timeout=2)
    assert result.returncode == 0 and result.stdout == b'finished\n'


def helper(marker):
    return (f'import time\nfrom pathlib import Path\n'
            f'Path({str(marker)!r}).write_text("first")\n'
            'time.sleep(2)\n'
            f'Path({str(marker)!r}).write_text("second")\n').encode()


def wait_first(marker, process):
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        assert process.poll() is None
        time.sleep(.01)
    assert marker.read_text() == 'first'


@pytest.mark.parametrize('disconnect', [False, True])
def test_remote_lease_cancels_after_disconnect_or_missing_heartbeat(tmp_path, disconnect):
    marker = tmp_path/'actions'
    payload = helper(marker)
    process = subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        process.stdin.write(struct.pack('!I', len(payload)) + payload)
        process.stdin.flush()
        wait_first(marker, process)
        started = time.monotonic()
        if disconnect:
            process.stdin.close()
            process.stdin = None
        process.wait(timeout=2)
        assert process.returncode != 0
        assert time.monotonic() - started < 1.5
        assert marker.read_text() == 'first'
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=2)


def test_local_cancel_reaps_process_before_later_actions(tmp_path, monkeypatch):
    marker = tmp_path/'actions'
    monkeypatch.setattr(transport, 'HEARTBEAT_INTERVAL', .08)
    started = time.monotonic()
    with pytest.raises(transport.RemoteStartupCancelled):
        transport.run_remote_startup(command(), payload=helper(marker),
            cancelled=lambda: marker.exists(), timeout=3)
    assert time.monotonic() - started < 1.5
    assert marker.read_text() == 'first'


def test_timeout_reaps_process_before_later_actions(tmp_path):
    marker = tmp_path/'actions'
    with pytest.raises(transport.RemoteStartupError, match='超时'):
        transport.run_remote_startup(command(), payload=helper(marker), timeout=.2)
    assert marker.read_text() == 'first'


def test_output_is_bounded():
    with pytest.raises(transport.RemoteStartupError, match='过大'):
        transport.run_remote_startup(command(),
            payload=b'import sys; sys.stdout.buffer.write(b"x"*300000)', timeout=2)


def test_cancel_before_launch_and_invalid_payload_have_no_side_effects(tmp_path):
    marker = tmp_path/'actions'
    with pytest.raises(transport.RemoteStartupCancelled):
        transport.run_remote_startup(command(), payload=helper(marker), cancelled=lambda: True)
    with pytest.raises(transport.RemoteStartupError):
        transport.run_remote_startup(command(), payload=b'x'*131073)
    assert not marker.exists()
