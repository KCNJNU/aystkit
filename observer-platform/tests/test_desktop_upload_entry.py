"""The desktop upload entry must choose a scope without touching live devices."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DesktopUploadEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='desktop-upload-entry-')
        self.root = Path(self.temporary.name)
        self.script = self.root / '上传.sh'
        shutil.copy2(Path.home() / '桌面/camera-section-tool/upload_data.sh', self.script)
        self.legacy_root = self.root / '旧版 会话'
        self.legacy_root.mkdir()
        self.package = self.root / 'package'
        collector = self.package / 'collector'
        collector.mkdir(parents=True)
        (collector / '__init__.py').write_text('')
        (collector / 'robot_profile.py').write_text(
            'def validate_current_profile(**kwargs): return {}\n')
        shutil.copy2(Path.home() / '.local/share/robot-cloud/collector/archive_desktop_entry.py',
                     collector / 'archive_desktop_entry.py')
        desktop_entry = collector / 'archive_desktop_entry.py'
        desktop_entry.write_text(desktop_entry.read_text() +
            '\n# Routing fixtures must never rotate the operator\'s real logs.\n'
            '_open_log = lambda _tool: (None, None)\n')
        (collector / 'archive_upload_only.py').write_text(
            'import json, os, sys\n'
            'print(json.dumps(dict(mode="native", args=sys.argv[1:], '
            'cwd=os.getcwd(), bytecodeDisabled=sys.dont_write_bytecode)))\n')
        self.delegate = self.root / 'legacy.sh'
        self.delegate.write_text('#!/bin/bash\nexec /usr/bin/python3 -c '
            "'import json, sys; print(json.dumps(dict(mode=\"legacy\", args=sys.argv[1:])))' \"$@\"\n")
        self.commands = self.root / 'bin'
        self.commands.mkdir()
        self.forbidden = self.root / 'forbidden-command'
        for command in ('systemctl', 'ssh', 'scp', 'docker', 'ros2', 'pkill', 'killall'):
            executable = self.commands / command
            executable.write_text('#!/bin/bash\nprintf called > "$FORBIDDEN_COMMAND_MARKER"\nexit 99\n')
            executable.chmod(0o700)
        self.env = dict(os.environ,
            PATH=str(self.commands) + os.pathsep + os.environ['PATH'],
            LOCAL_CAMERA_DATASET_ROOT=str(self.legacy_root),
            LOCAL_CAMERA_UPLOAD_SCRIPT=str(self.delegate),
            NATIVE_CAMERA_ARCHIVE_PACKAGE_ROOT=str(self.package),
            FORBIDDEN_COMMAND_MARKER=str(self.forbidden))

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *args):
        return subprocess.run(['/bin/bash', str(self.script), *args], env=self.env,
            cwd=self.root, capture_output=True, text=True, timeout=5)

    def report(self, *args):
        result = self.run_script(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.forbidden.exists())
        return json.loads(result.stdout.splitlines()[-1])

    def test_old_completed_session_does_not_replace_default_native_scope(self):
        session = self.legacy_root / '20261102_120000'
        session.mkdir()
        (session / 'capture-session.json').write_text('{"state":"completed"}')
        report = self.report('--dry-run')
        self.assertEqual(report['mode'], 'native')
        self.assertEqual(report['args'], ['--dry-run'])
        self.assertTrue(report['bytecodeDisabled'])
        self.assertFalse(list(self.package.rglob('__pycache__')))

    def test_default_upload_syncs_source_with_visible_summary_and_local_only_opts_out(self):
        report = self.report()
        self.assertEqual(report['args'], ['--sync-source', '--summary'])
        report = self.report('--local-only', '--no-pause')
        self.assertEqual(report['args'], ['--local-only'])

    def test_source_scope_is_explicit_and_cannot_mix_with_legacy_or_itself(self):
        for args in (['--local-only', '--sync-source'], ['--sync-source', '--sync-source'],
                     ['--latest', '--local-only'], ['--latest', '--sync-source']):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('"mode"', result.stdout)
                self.assertFalse(self.forbidden.exists())

    def test_explicit_latest_session_and_root_keep_legacy_compatibility(self):
        for selector in (['--latest'], ['--session', '20261102_120000'], []):
            with self.subTest(selector=selector):
                report = self.report('--root', str(self.legacy_root), *selector, '--dry-run')
                self.assertEqual(report['mode'], 'legacy')
                self.assertEqual(report['args'], ['--root', str(self.legacy_root), *selector, '--dry-run'])

    def test_native_root_environment_and_timeout_are_forwarded_as_single_arguments(self):
        root = self.root / '原生 目录'
        env = self.root / '归档 配置.env'
        report = self.report('--archive-root', str(root), '--env-file', str(env),
            '--timeout', '2.5', '--dry-run')
        self.assertEqual(report['mode'], 'native')
        self.assertEqual(report['args'], ['--archive-root', str(root), '--env-file', str(env),
            '--timeout', '2.5', '--dry-run'])

    def test_callers_working_directory_cannot_override_the_configured_package(self):
        shadow = self.root / 'collector'
        shadow.mkdir()
        (shadow / '__init__.py').write_text('raise RuntimeError("wrong package")\n')
        (self.root / 'runpy.py').write_text('raise RuntimeError("wrong bootstrap")\n')
        (self.root / 'math.py').write_text('raise RuntimeError("wrong timeout validation")\n')
        report = self.report('--timeout', '5', '--dry-run')
        self.assertEqual(report['mode'], 'native')
        self.assertFalse(list(shadow.rglob('__pycache__')))
        self.assertFalse((self.root / '__pycache__').exists())

    def test_relative_data_and_config_paths_keep_the_callers_working_directory(self):
        report = self.report('--archive-root', 'data', '--env-file', 'archive.env', '--dry-run')
        self.assertEqual(report['cwd'], str(self.root))
        self.assertEqual(report['args'], ['--archive-root', 'data', '--env-file', 'archive.env', '--dry-run'])

    def test_relative_package_root_is_resolved_without_changing_the_callers_directory(self):
        self.env['NATIVE_CAMERA_ARCHIVE_PACKAGE_ROOT'] = 'package'
        report = self.report('--archive-root', 'data', '--dry-run')
        self.assertEqual(report['mode'], 'native')
        self.assertEqual(report['cwd'], str(self.root))
        self.assertEqual(report['args'], ['--archive-root', 'data', '--dry-run'])

    def test_invalid_or_conflicting_options_fail_before_invoking_either_uploader(self):
        invalid = [
            ['--root'], ['--root', ''], ['--root', '--latest'],
            ['--archive-root'], ['--archive-root', '-h'], ['--env-file'],
            ['--session'], ['--session', '../outside'], ['--session', '--dry-run'],
            ['--latest', '--session', '20261102_120000'], ['--latest', '--latest'],
            ['--session', '20261102_120000', '--session', '20261102_120001'],
            ['--root', str(self.legacy_root), '--root', str(self.legacy_root)],
            ['--archive-root', '/one', '--archive-root', '/two'],
            ['--env-file', '/one', '--env-file', '/two'],
            ['--latest', '--archive-root', '/native'], ['--latest', '--env-file', '/env'],
            ['--latest', '--timeout', '5'], ['--root', str(self.legacy_root), '--timeout', '5'],
            ['--timeout'], ['--timeout', '0'], ['--timeout', '-1'],
            ['--timeout', 'nan'], ['--timeout', 'inf'], ['--timeout', '3601'],
            ['--timeout', 'abc'], ['--timeout', '1', '--timeout', '2'], ['--unknown'],
        ]
        for args in invalid:
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertNotIn('"mode"', result.stdout)
                self.assertFalse(self.forbidden.exists())

    def test_missing_or_symlinked_legacy_root_and_session_are_rejected(self):
        linked = self.root / 'linked'
        linked.symlink_to(self.legacy_root, target_is_directory=True)
        session = self.legacy_root / '20261102_120000'
        session.symlink_to(self.root, target_is_directory=True)
        for args in (['--root', str(self.root / 'missing')],
                     ['--root', str(linked)],
                     ['--root', str(linked) + '/'],
                     ['--session', session.name]):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('"mode"', result.stdout)
                self.assertFalse(self.forbidden.exists())

    def test_help_is_read_only_and_explains_the_two_scopes(self):
        result = self.run_script('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('默认只上传当前原生归档', result.stdout)
        self.assertIn('--timeout 混用', result.stdout)
        self.assertFalse(self.forbidden.exists())


if __name__ == '__main__':
    unittest.main()
