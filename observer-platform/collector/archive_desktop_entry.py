"""Desktop terminal output and logs without changing the capture/upload process."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
import os
from pathlib import Path
import re
import runpy
import select
import signal
import stat
import sys
import traceback


READ_ONLY_FLAGS = frozenset(('--help', '-h', '--dry-run', '--status', '--stop'))
MAX_LOGS = 20


class _Tee:
    def __init__(self, stream, log):
        self.stream = stream
        self.log = log

    def write(self, value):
        try:
            self.log.write(value)
            self.log.flush()
        except (OSError, ValueError, UnicodeError):
            pass  # A logging problem must never stop recording or uploading.
        try:
            result = self.stream.write(value)
            self.stream.flush()
            return result
        except (OSError, ValueError):
            # Closing a terminal must not interrupt the target's finally block
            # before it releases the capture lease and persists the stop proof.
            return len(value)

    def flush(self):
        try:
            self.log.flush()
        except (OSError, ValueError):
            pass
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _open_log(tool):
    """Create a private log; prune only this tool's recognized, owned logs."""
    safe_tool = re.sub(r'[^a-zA-Z0-9_-]', '_', str(tool))[:40] or 'desktop'
    root = Path.home() / '.local/state/robot-cloud/desktop-logs'
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root.is_symlink():
            return None, None
        stamp = datetime.now().strftime('%Y%m%dT%H%M%S%f')
        path = root / f'{safe_tool}-{stamp}-{os.getpid()}.log'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            log = os.fdopen(descriptor, 'w', encoding='utf-8', buffering=1)
        except BaseException:
            os.close(descriptor)
            raise
    except (OSError, ValueError):
        return None, None
    pattern = re.compile(re.escape(safe_tool) + r'-[0-9]{8}T[0-9]{12}-[0-9]+\.log')
    try:
        previous = []
        for candidate in root.iterdir():
            if candidate == path or not pattern.fullmatch(candidate.name):
                continue
            info = candidate.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                previous.append(candidate)
        for candidate in sorted(previous, reverse=True)[MAX_LOGS - 1:]:
            try:
                candidate.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return path, log


def _exit_code(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    print(value, file=sys.stderr)
    return 1


class _PauseSignal(BaseException):
    def __init__(self, number):
        self.number = number


def _restore_signals(handlers):
    for number, handler in handlers.items():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError):
            pass


def _close_log(log):
    try:
        log.close()
    except (OSError, ValueError):
        pass


def _wait_for_close():
    print('按 Enter 关闭窗口…', end='', flush=True)
    try:
        descriptor = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        sys.stdin.readline()
        return
    # input()/PyOS_Readline can enter its C stdio read just after SIGINT
    # arrives, leaving the Python handler pending until another line appears.
    # Poll at the Python boundary so a signal always interrupts this prompt.
    while True:
        ready, _, _ = select.select([descriptor], [], [], .2)
        if ready:
            sys.stdin.readline()
            return


def _pause(code):
    previous = {number: signal.getsignal(number) for number in
        (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        # Preserve the target's handlers during its work. Only the closing
        # prompt needs its own HUP handler, in case the target installed one.
        def interrupted(number, _frame):
            raise _PauseSignal(number)
        for number in previous:
            signal.signal(number, interrupted)
        _wait_for_close()
    except KeyboardInterrupt:
        return 130
    except _PauseSignal as error:
        return 128 + error.number
    except (EOFError, OSError, ValueError):
        return code
    finally:
        _restore_signals(previous)
    return code


def run_module(module, *, tool, argv=None):
    """Run the target in this process, retaining its TTY, signals and exit code."""
    original_args = list(sys.argv[1:] if argv is None else argv)
    target_args = []
    pause_override = None
    for argument in original_args:
        if argument == '--pause':
            pause_override = True
        elif argument == '--no-pause':
            pause_override = False
        else:
            target_args.append(argument)
    terminal = _terminal(sys.stdin) and _terminal(sys.stdout)
    interactive = terminal and (pause_override is True or
        (pause_override is None and not original_args))
    if terminal and not original_args and tool == 'upload':
        target_args.append('--summary')
    if not READ_ONLY_FLAGS.intersection(target_args):
        try:
            from .robot_profile import validate_current_profile
            validate_current_profile()
        except (OSError, ValueError, KeyError, TypeError):
            print('机器人身份配置核对失败；未启动设备或上传。请运行机器人接入检查。', file=sys.stderr)
            return 2
    path, log = (None, None)
    if not READ_ONLY_FLAGS.intersection(target_args):
        path, log = _open_log(tool)
    if path is not None:
        print(f'运行日志：{path}', file=sys.stderr, flush=True)
    saved_argv = sys.argv
    saved_signals = {number: signal.getsignal(number) for number in
        (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    with ExitStack() as stack:
        if log is not None:
            # Register close before redirects so streams are restored first.
            stack.callback(_close_log, log)
            stack.enter_context(redirect_stdout(_Tee(sys.stdout, log)))
            stack.enter_context(redirect_stderr(_Tee(sys.stderr, log)))
        try:
            sys.argv = [module, *target_args]
            try:
                runpy.run_module(module, run_name='__main__', alter_sys=True)
                code = 0
            except SystemExit as error:
                code = _exit_code(error.code)
            except KeyboardInterrupt:
                code = 130
            except Exception:
                traceback.print_exc()
                code = 2
        finally:
            sys.argv = saved_argv
            _restore_signals(saved_signals)
        if interactive:
            state = '成功' if code == 0 else '未完成' if code == 3 else '失败'
            print(f'\n{state}，退出码：{code}', flush=True)
            code = _pause(code)
    return code
