"""Desktop convenience must preserve recording behavior and headless output."""
import io
import os
from pathlib import Path
import pty
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from collector import archive_desktop_entry as entry


class Terminal(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='desktop-terminal-')
        self.home = Path(self.temporary.name)
        self.logs = self.home / '.local/state/robot-cloud/desktop-logs'
        self.home_patch = patch.object(entry.Path, 'home', return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temporary.cleanup()

    def invoke(self, args=None, *, tty=False, tool='capture', target=None, answer=None):
        self.stdin = Terminal(tty)
        self.stdout = Terminal(tty)
        self.stderr = Terminal(tty)
        self.target_args = None
        def execute(_module, **kwargs):
            self.target_args = sys.argv[1:]
            self.assertEqual(kwargs, dict(run_name='__main__', alter_sys=True))
            self.assertIs(sys.stdin, self.stdin)
            self.assertEqual(sys.stdout.isatty(), tty)
            if target:
                return target()
        with patch.object(sys, 'stdin', self.stdin), patch.object(sys, 'stdout', self.stdout), \
             patch.object(sys, 'stderr', self.stderr), patch.object(entry.runpy, 'run_module', side_effect=execute), \
             patch.object(entry, '_wait_for_close', side_effect=answer, return_value='') as prompt:
            saved_argv = sys.argv
            code = entry.run_module('collector.fixture', tool=tool, argv=args or [])
            self.assertIs(sys.argv, saved_argv)
        self.prompted = prompt.call_count
        return code

    def test_tty_no_argument_upload_uses_summary_and_pauses(self):
        self.assertEqual(self.invoke(tty=True, tool='upload'), 0)
        self.assertEqual(self.target_args, ['--summary'])
        self.assertEqual(self.prompted, 1)
        self.assertIn('成功，退出码：0', self.stdout.getvalue())

    def test_non_tty_and_explicit_arguments_never_pause_or_reformat_output(self):
        for args, tty in (([], False), (['--timeout', '5'], True), (['--pause'], False),
                          (['--no-pause'], True)):
            with self.subTest(args=args, tty=tty):
                self.assertEqual(self.invoke(args, tty=tty, tool='upload',
                    target=lambda: print('{"state":"complete"}')), 0)
                self.assertEqual(self.stdout.getvalue(), '{"state":"complete"}\n')
                self.assertEqual(self.prompted, 0)
                self.assertNotIn('--pause', self.target_args)
                self.assertNotIn('--no-pause', self.target_args)

    def test_explicit_pause_is_consumed_and_preserves_other_arguments(self):
        self.assertEqual(self.invoke(['--timeout', '7', '--pause'], tty=True), 0)
        self.assertEqual(self.target_args, ['--timeout', '7'])
        self.assertEqual(self.prompted, 1)

    def test_read_only_options_never_create_log_directories(self):
        for option in ('--help', '-h', '--dry-run', '--status', '--stop'):
            with self.subTest(option=option):
                self.assertEqual(self.invoke([option]), 0)
                self.assertFalse(self.logs.exists())

    def test_stdout_and_stderr_are_logged_privately_in_the_same_process(self):
        expected_pid = os.getpid()
        def target():
            self.assertEqual(os.getpid(), expected_pid)
            print('capture output')
            print('capture diagnostic', file=sys.stderr)
        self.assertEqual(self.invoke(target=target), 0)
        logs = list(self.logs.glob('capture-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)
        self.assertEqual(logs[0].read_text(), 'capture output\ncapture diagnostic\n')
        self.assertEqual(self.stdout.getvalue(), 'capture output\n')
        self.assertIn(str(logs[0]), self.stderr.getvalue())

    def test_only_this_tools_recognized_regular_logs_are_pruned(self):
        self.logs.mkdir(parents=True)
        for index in range(24):
            (self.logs / f'capture-20261101T000000{index:06d}-42.log').write_text('old')
        other = self.logs / 'upload-20261101T000000000000-42.log'
        other.write_text('keep other tool')
        unknown = self.logs / 'capture-not-a-generated-file.log'
        unknown.write_text('keep unknown')
        linked = self.logs / 'capture-20261101T000000999999-42.log'
        linked.symlink_to(other)
        self.assertEqual(self.invoke(), 0)
        regular = [path for path in self.logs.glob('capture-*.log')
                   if path != unknown and not path.is_symlink()]
        self.assertEqual(len(regular), 20)
        self.assertTrue(other.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(linked.is_symlink())

    def test_logging_failure_does_not_prevent_the_target(self):
        (self.home / '.local').write_text('not a directory')
        self.assertEqual(self.invoke(target=lambda: print('still running')), 0)
        self.assertEqual(self.stdout.getvalue(), 'still running\n')

    def test_closed_terminal_does_not_interrupt_target_cleanup_or_its_log(self):
        cleaned_up = []
        def target():
            self.stdout.close()
            self.stderr.close()
            try:
                print('terminal disconnected', flush=True)
                print('terminal diagnostic', file=sys.stderr, flush=True)
            finally:
                print('capture lease released', flush=True)
                print('stop proof persisted', file=sys.stderr, flush=True)
                cleaned_up.append(True)
            raise SystemExit(3)
        self.assertEqual(self.invoke(target=target), 3)
        self.assertEqual(cleaned_up, [True])
        logs = list(self.logs.glob('capture-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].read_text(), 'terminal disconnected\nterminal diagnostic\n'
                         'capture lease released\nstop proof persisted\n')

    def test_tee_logs_first_and_isolates_terminal_write_and_flush_errors(self):
        for operation in ('write', 'flush'):
            for error_type in (BrokenPipeError, OSError, ValueError):
                with self.subTest(operation=operation, error=error_type.__name__):
                    terminal, log = io.StringIO(), io.StringIO()
                    tee = entry._Tee(terminal, log)
                    def disconnected(*_):
                        self.assertEqual(log.getvalue(), 'shutdown evidence')
                        raise error_type('fixture terminal disconnected')
                    with patch.object(terminal, operation, side_effect=disconnected):
                        self.assertEqual(tee.write('shutdown evidence'), len('shutdown evidence'))
                        tee.flush()
                    self.assertEqual(log.getvalue(), 'shutdown evidence')

    def test_broken_output_pipe_keeps_cleanup_messages_in_log(self):
        reader, writer = os.pipe()
        os.close(reader)
        terminal = os.fdopen(writer, 'w', buffering=1, encoding='utf-8')
        log = io.StringIO()
        try:
            tee = entry._Tee(terminal, log)
            self.assertEqual(tee.write('capture stopped\n'), len('capture stopped\n'))
            tee.flush()
            self.assertEqual(tee.write('all cleanup complete\n'), len('all cleanup complete\n'))
            self.assertEqual(log.getvalue(), 'capture stopped\nall cleanup complete\n')
        finally:
            try:
                terminal.close()
            except BrokenPipeError:
                pass

    def test_tee_preserves_normal_return_and_stream_descriptor_delegation(self):
        terminal, log = io.StringIO(), io.StringIO()
        tee = entry._Tee(terminal, log)
        with patch.object(terminal, 'write', return_value=3), \
             patch.object(terminal, 'isatty', return_value=True), \
             patch.object(terminal, 'fileno', return_value=81):
            self.assertEqual(tee.write('message'), 3)
            self.assertTrue(tee.isatty())
            self.assertEqual(tee.fileno(), 81)
        self.assertEqual(log.getvalue(), 'message')

    def test_tee_log_errors_do_not_skip_terminal_or_its_flush(self):
        for operation in ('write', 'flush'):
            with self.subTest(operation=operation):
                terminal, log = io.StringIO(), io.StringIO()
                tee = entry._Tee(terminal, log)
                with patch.object(log, operation, side_effect=OSError('fixture log failed')), \
                     patch.object(terminal, 'flush', wraps=terminal.flush) as flushed:
                    self.assertEqual(tee.write('still capturing'), len('still capturing'))
                    tee.flush()
                    self.assertEqual(flushed.call_count, 2)
                self.assertEqual(terminal.getvalue(), 'still capturing')

    def test_tee_flush_attempts_log_when_terminal_is_already_closed(self):
        terminal, log = io.StringIO(), io.StringIO()
        terminal.close()
        with patch.object(log, 'flush', wraps=log.flush) as flushed:
            entry._Tee(terminal, log).flush()
            flushed.assert_called_once_with()

    def test_system_exit_codes_and_unexpected_exception_are_preserved(self):
        for value, expected in ((None, 0), (3, 3), (7, 7), ('bad argument', 1)):
            with self.subTest(value=value):
                def target():
                    raise SystemExit(value)
                self.assertEqual(self.invoke(target=target), expected)
        def broken():
            raise RuntimeError('fixture error')
        self.assertEqual(self.invoke(target=broken), 2)
        self.assertIn('Traceback', self.stderr.getvalue())
        self.assertIn('fixture error', self.stderr.getvalue())
        self.assertTrue(any('fixture error' in path.read_text() for path in self.logs.glob('*.log')))

    def test_interactive_incomplete_result_and_pause_interruptions(self):
        def incomplete():
            raise SystemExit(3)
        self.assertEqual(self.invoke(tty=True, target=incomplete), 3)
        self.assertIn('未完成，退出码：3', self.stdout.getvalue())
        self.assertEqual(self.invoke(tty=True, answer=KeyboardInterrupt), 130)
        for number in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(number=number):
                def interrupted(*_args):
                    signal.raise_signal(number)
                self.assertEqual(self.invoke(tty=True, answer=interrupted), 128 + number)

    def test_pause_eof_and_closed_input_preserve_target_exit_code(self):
        def incomplete():
            raise SystemExit(3)
        for error in (EOFError, OSError, ValueError):
            with self.subTest(error=error.__name__):
                self.assertEqual(self.invoke(tty=True, target=incomplete, answer=error), 3)

    def test_close_prompt_polls_before_reading_and_accepts_enter_or_eof(self):
        for response in ('\n', ''):
            with self.subTest(response=response):
                terminal = Terminal(True)
                output = io.StringIO()
                with patch.object(sys, 'stdin', terminal), patch.object(sys, 'stdout', output), \
                     patch.object(terminal, 'fileno', return_value=83), \
                     patch.object(terminal, 'readline', return_value=response) as read_line, \
                     patch.object(entry.select, 'select', side_effect=[([], [], []), ([83], [], [])]) as polling:
                    entry._wait_for_close()
                self.assertIn('Enter', output.getvalue())
                self.assertEqual(polling.call_args_list[0].args, ([83], [], [], .2))
                self.assertEqual(polling.call_count, 2)
                read_line.assert_called_once_with()

    def test_target_signal_handlers_are_not_replaced_during_work_and_are_restored_afterwards(self):
        original = {number: signal.getsignal(number) for number in
                    (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        def target():
            for number, handler in original.items():
                self.assertIs(signal.getsignal(number), handler)
                signal.signal(number, lambda *_args: None)
        self.assertEqual(self.invoke(tty=True, target=target), 0)
        for number, handler in original.items():
            self.assertIs(signal.getsignal(number), handler)

    def test_pty_ctrl_c_closes_the_prompt_after_a_capture_installs_its_handlers(self):
        fixture = self.home / 'fixture_capture.py'
        fixture.write_text('import signal, sys\n'
            'for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):\n'
            '    signal.signal(number, lambda *_args: None)\n'
            'print("fixture tty:", sys.stdin.isatty(), flush=True)\n')
        package = str(Path(entry.__file__).resolve().parents[1])
        code = ('import sys, fcntl, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); '
                'sys.path[:0] = sys.argv[1:3]; '
                'from collector.archive_desktop_entry import run_module; '
                'raise SystemExit(run_module("fixture_capture", tool="capture", argv=[]))')
        master, slave = pty.openpty()
        process = subprocess.Popen(['/usr/bin/python3', '-B', '-c', code, package, str(self.home)],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            env=dict(os.environ, HOME=str(self.home)))
        os.close(slave)
        output = b''
        try:
            deadline = time.monotonic() + 5
            while b'Enter' not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    output += os.read(master, 65536)
                if process.poll() is not None:
                    break
            self.assertIn(b'fixture tty: True', output)
            self.assertIn(b'Enter', output)
            os.write(master, b'\x03')
            self.assertEqual(process.wait(timeout=3), 130)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
