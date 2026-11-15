"""Narrow, transaction-bound poweroff handshake; importing never requests power."""
from __future__ import annotations

import re


# This source is sent as a fixed Python -c argument, never interpolated with input.
# The program runs only after the coordinator explicitly launches root_command().
ROOT_HELPER_SOURCE = r'''
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path

_BUS = ['/usr/bin/busctl', '--system']
_DEST = ['org.freedesktop.login1', '/org/freedesktop/login1',
         'org.freedesktop.login1.Manager']
_NONCE = re.compile(r'[0-9a-f]{64}')
_BOOT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class _LineReader:
    def __init__(self, stream, clock=time.monotonic):
        self.fd = stream.fileno()
        self.clock = clock
        self.buffer = b''

    def __call__(self, timeout):
        deadline = self.clock() + timeout
        while True:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                return line.decode('utf-8')
            if len(self.buffer) > 4096:
                raise ValueError('input_too_large')
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError('input_timeout')
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                raise TimeoutError('input_timeout')
            chunk = os.read(self.fd, 4097 - len(self.buffer))
            if not chunk:
                if self.buffer:
                    raise ValueError('incomplete_input')
                return None
            self.buffer += chunk


def _helper_main(nonce, *, reader=None, output=None, run=None, popen=None,
                 clock=None, sleep=None, boot_reader=None, geteuid=None):
    """Dependencies are injectable so tests cannot invoke any power operation."""
    run = run or subprocess.run
    popen = popen or subprocess.Popen
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    geteuid = geteuid or os.geteuid
    output = output or sys.stdout
    boot_reader = boot_reader or (lambda: Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    boot_id = None
    requested = False
    client = None

    def emit(phase, **fields):
        output.write(json.dumps(dict(phase=phase, nonce=nonce, bootId=boot_id, **fields),
                                allow_nan=False, separators=(',', ':')) + '\n')
        output.flush()

    def property_value(name, timeout=3):
        result = run(_BUS + ['get-property'] + _DEST + [name],
                     stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     timeout=timeout, check=False)
        if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 4096:
            raise ValueError('power_state_unavailable')
        return result.stdout.strip()

    def preparing(timeout=3):
        value = property_value('PreparingForShutdown', timeout)
        if value not in ('b false', 'b true'):
            raise ValueError('invalid_power_state')
        return value == 'b true'

    def check_idle():
        if preparing():
            raise ValueError('shutdown_already_in_progress')
        scheduled = shlex.split(property_value('ScheduledShutdown'))
        if scheduled != ['(st)', '', '0']:
            raise ValueError('shutdown_already_scheduled')

    def message(line):
        if not isinstance(line, str) or len(line) > 4096:
            raise ValueError('invalid_input')
        item = json.loads(line)
        if (not isinstance(item, dict) or set(item) != {'phase', 'nonce', 'bootId'}
                or item.get('nonce') != nonce or item.get('bootId') != boot_id):
            raise ValueError('transaction_mismatch')
        return item.get('phase')

    try:
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise ValueError('invalid_nonce')
        if geteuid() != 0:
            raise ValueError('root_required')
        boot_id = boot_reader()
        if not isinstance(boot_id, str) or _BOOT.fullmatch(boot_id) is None:
            raise ValueError('invalid_boot_identity')
        reader = reader or _LineReader(sys.stdin.buffer, clock)
        check_idle()
        emit('ready')
        try:
            line = reader(600)
        except TimeoutError:
            emit('cancelled', reason='commit_timeout')
            return 0
        if line is None:
            emit('cancelled', reason='coordinator_disconnected')
            return 0
        phase = message(line)
        if phase == 'cancel':
            emit('cancelled', reason='coordinator_cancelled')
            return 0
        if phase != 'commit':
            raise ValueError('commit_required')
        if boot_reader() != boot_id:
            raise ValueError('boot_identity_changed')
        check_idle()
        # PowerOff may wait until the delay inhibitor is released. Poll a separate
        # connection instead of blocking on its return or accepting exit 0 as proof.
        client = popen(_BUS + ['call'] + _DEST + ['PowerOff', 'b', 'false'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, close_fds=True)
        requested = True
        deadline = clock() + 20
        while True:
            status = client.poll()
            if status is not None and status != 0:
                raise ValueError('poweroff_request_rejected')
            remaining = deadline - clock()
            if remaining <= 0:
                raise ValueError('shutdown_not_confirmed')
            if preparing(timeout=min(3, remaining)):
                break
            sleep(min(0.1, max(0, deadline - clock())))
        emit('shutdown_requested')
        # Give the coordinator a brief explicit release opportunity. Closing this
        # process then releases systemd-inhibit's delay lock, including on EOF.
        try:
            line = reader(1)
            if line is not None and message(line) != 'release':
                raise ValueError('release_required')
        except TimeoutError:
            pass
        return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        # Errors after commit cannot undo an already accepted shutdown request.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        try:
            emit('error', error=reason, requestSubmitted=requested)
        except (OSError, ValueError, TypeError):
            pass
        return 1
if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(_helper_main(sys.argv[1]))
'''


def root_command(nonce: str) -> list[str]:
    """Return the exact privileged command; caller owns whether to launch it."""
    if not isinstance(nonce, str) or re.fullmatch(r'[0-9a-f]{64}', nonce) is None:
        raise ValueError('nonce must be 64 lowercase hexadecimal characters')
    return ['/usr/bin/sudo', '-n', '/usr/bin/systemd-inhibit', '--what=shutdown',
            '--mode=delay', '--who=robot-safe-shutdown',
            '--why=Save robot capture data before coordinated host shutdown',
            '/usr/bin/python3', '-I', '-u', '-c', ROOT_HELPER_SOURCE, nonce]
