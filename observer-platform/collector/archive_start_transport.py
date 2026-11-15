"""Bounded SSH startup transport with a remote, stdin-backed cancellation lease."""
from __future__ import annotations

import os
import selectors
import signal
import struct
import subprocess
import time


MAX_HELPER_BYTES = 131072
MAX_REPORT_BYTES = 262144
HEARTBEAT_INTERVAL = 2.0

# Python -c leaves stdin available for this protocol. The watchdog never stops
# hardware owners: it only interrupts this helper's remaining startup actions.
REMOTE_BOOTSTRAP = r'''
import os, select, signal, struct, sys, threading, time
HEARTBEAT_TIMEOUT = 10.0
class _StartupTransportCancelled(BaseException):
    pass
def _cancel_startup(_signal, _frame):
    raise _StartupTransportCancelled()
signal.signal(signal.SIGTERM, _cancel_startup)
def _read_exact(size):
    value = bytearray()
    deadline = time.monotonic() + HEARTBEAT_TIMEOUT
    while len(value) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise _StartupTransportCancelled()
        chunk = os.read(0, size - len(value))
        if not chunk:
            raise _StartupTransportCancelled()
        value.extend(chunk)
    return bytes(value)
_size = struct.unpack('!I', _read_exact(4))[0]
if not 0 < _size <= 131072:
    raise ValueError('invalid startup helper size')
_code = _read_exact(_size)
_finished = threading.Event()
def _watch_transport():
    deadline = time.monotonic() + HEARTBEAT_TIMEOUT
    while not _finished.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if not _finished.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
            return
        if not select.select([0], [], [], min(.25, remaining))[0]:
            continue
        heartbeat = os.read(0, 4096)
        if not heartbeat or any(value != 1 for value in heartbeat):
            if not _finished.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
            return
        deadline = time.monotonic() + HEARTBEAT_TIMEOUT
threading.Thread(target=_watch_transport, daemon=True).start()
try:
    exec(compile(_code, '<recording-startup>', 'exec'),
         {'__name__': '__main__', '__file__': '<recording-startup>'})
finally:
    _finished.set()
'''


class RemoteStartupCancelled(RuntimeError):
    pass


class RemoteStartupError(RuntimeError):
    pass


def _reap(process):
    # Closing the heartbeat channel also cancels a remote helper when SSH does
    # not forward Unix signals. A severed connection expires remotely in 10 s.
    if process.stdin is not None:
        process.stdin.close()
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=.5)


def run_remote_startup(command, *, payload, cancelled=lambda: False, timeout=150,
                       notice='设备启动检查仍在进行；按 Ctrl+C 可取消后续启动。'):
    """Send helper bytes, retain stdin for heartbeats, and bound all output."""
    if cancelled():
        raise RemoteStartupCancelled()
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_HELPER_BYTES:
        raise RemoteStartupError('设备启动程序大小无效')
    if timeout <= 0:
        raise RemoteStartupError('设备启动检查超时')
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS',
        f"unix:path={environment['XDG_RUNTIME_DIR']}/bus")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env=environment)
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    pending = memoryview(struct.pack('!I', len(payload)) + payload)
    deadline = time.monotonic() + timeout
    heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL
    notice_at = time.monotonic() + 15
    stdin_open = True
    with selectors.DefaultSelector() as selector:
        for stream, kind, events in ((process.stdin, 'stdin', selectors.EVENT_WRITE),
                                    (process.stdout, 'stdout', selectors.EVENT_READ),
                                    (process.stderr, 'stderr', selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events, kind)
        try:
            while True:
                if cancelled():
                    raise RemoteStartupCancelled()
                now = time.monotonic()
                if now >= deadline:
                    raise RemoteStartupError('设备启动检查超时，已启动的设备保持运行')
                if stdin_open and not pending and now >= heartbeat_at:
                    pending = memoryview(b'\x01')
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                if process.poll() is not None and not any(
                        key.data in output for key in selector.get_map().values()):
                    return subprocess.CompletedProcess(command, process.returncode,
                        bytes(output['stdout']), bytes(output['stderr']))
                for key, _events in selector.select(min(.1, deadline - now)):
                    if key.data == 'stdin':
                        try:
                            written = os.write(key.fd, pending)
                            pending = pending[written:]
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            stdin_open = False
                            pending = memoryview(b'')
                            continue
                        if not pending:
                            selector.unregister(key.fileobj)
                            heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL
                    else:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            target = output[key.data]
                            if len(target) + len(chunk) > MAX_REPORT_BYTES:
                                raise RemoteStartupError('设备启动报告过大')
                            target.extend(chunk)
                if time.monotonic() >= notice_at:
                    try:
                        print(notice, flush=True)
                    except (OSError, ValueError):
                        # Terminal loss must not cancel a camera shutdown.
                        pass
                    notice_at = time.monotonic() + 15
        finally:
            _reap(process)
            for stream in (process.stdout, process.stderr):
                stream.close()
