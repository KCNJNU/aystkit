"""The desktop recorder must use its configured package from any directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def entry(tmp_path):
    script = tmp_path / 'capture.sh'
    shutil.copy2(Path.home() / '桌面/camera-section-tool/launch_sensors.sh', script)
    package = tmp_path / 'configured package'
    collector = package / 'collector'
    collector.mkdir(parents=True)
    (collector / '__init__.py').write_text('')
    shutil.copy2(Path.home() / '.local/share/robot-cloud/collector/archive_desktop_entry.py',
                 collector / 'archive_desktop_entry.py')
    desktop_entry = collector / 'archive_desktop_entry.py'
    desktop_entry.write_text(desktop_entry.read_text() +
        '\n# Routing fixtures must never rotate the operator\'s real logs.\n'
        '_open_log = lambda _tool: (None, None)\n')
    (collector / 'robot_profile.py').write_text('def validate_current_profile(**_options):\n    return None\n')
    (collector / 'archive_capture_session.py').write_text(
        'import json, os, sys\n'
        'print(json.dumps(dict(source="configured", cwd=os.getcwd(), '
        'args=sys.argv[1:], bytecodeDisabled=sys.dont_write_bytecode)))\n')
    environment = dict(os.environ, NATIVE_CAMERA_ARCHIVE_PACKAGE_ROOT=str(package))
    return script, package, environment


def test_entry_ignores_a_different_collector_in_the_callers_directory(tmp_path, entry):
    script, package, environment = entry
    shadow = tmp_path / 'collector'
    shadow.mkdir()
    (shadow / '__init__.py').write_text('raise RuntimeError("wrong collector")\n')
    (tmp_path / 'runpy.py').write_text('raise RuntimeError("wrong bootstrap")\n')
    result = subprocess.run(['/bin/bash', str(script), '--dry-run'], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == dict(source='configured', cwd=str(tmp_path), args=['--dry-run'],
                         bytecodeDisabled=True)
    assert not list(package.rglob('__pycache__'))
    assert not list(shadow.rglob('__pycache__'))


def test_relative_package_and_arguments_keep_the_callers_directory(tmp_path, entry):
    script, _, environment = entry
    environment['NATIVE_CAMERA_ARCHIVE_PACKAGE_ROOT'] = 'configured package'
    result = subprocess.run(['/bin/bash', str(script), '--duration', '0.25'],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['cwd'] == str(tmp_path)
    assert report['args'] == ['--duration', '0.25']


def test_missing_configured_program_fails_before_running_a_fallback(tmp_path, entry):
    script, _, environment = entry
    environment['NATIVE_CAMERA_ARCHIVE_PACKAGE_ROOT'] = str(tmp_path / 'missing')
    result = subprocess.run(['/bin/bash', str(script), '--dry-run'], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert '缺少采集会话程序' in result.stderr
    assert not result.stdout
