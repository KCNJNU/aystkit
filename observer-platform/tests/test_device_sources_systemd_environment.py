"""Opt-in ExecStart checks using private printers, never ROS or a real device.

ROBOT_CLOUD_TEST_USER_SYSTEMD=1 python -m pytest -q <this file>
"""
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from deploy.scripts import device_sources as sources


pytestmark = pytest.mark.skipif(
    os.environ.get('ROBOT_CLOUD_TEST_USER_SYSTEMD') != '1',
    reason='requires explicit opt-in to the real user service manager')


@pytest.mark.parametrize('recording', [False, True])
@pytest.mark.parametrize('kind', ['driver', 'preview'])
def test_systemd_preserves_dds_restore_and_camera_transport(tmp_path, monkeypatch,
                                                          recording, kind):
    # The service has only fixture setup scripts and a Python JSON printer.
    # Production unit names, ROS setup, sensor executables and devices are absent.
    workspace = tmp_path / 'workspace with spaces'
    (workspace / 'install').mkdir(parents=True)
    setup = tmp_path / 'fixture-ros-setup.bash'
    for path in (setup, workspace / 'install/setup.bash', workspace / 'env.sh'):
        path.write_text('export ROS_DOMAIN_ID=91\n'
                        'export ROS_LOCALHOST_ONLY=1\n'
                        'export ROS_DISCOVERY_SERVER=incorrect-fixture-server\n'
                        'export RMW_FASTRTPS_PUBLICATION_MODE=SYNCHRONOUS\n')
    for key in ('ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'RMW_IMPLEMENTATION',
                'ROS_DISCOVERY_SERVER', 'FASTRTPS_DEFAULT_PROFILES_FILE',
                'RMW_FASTRTPS_USE_QOS_FROM_XML', 'RMW_FASTRTPS_PUBLICATION_MODE',
                'MID360_NAV2_DDS_SERVER', 'CYCLONEDDS_URI'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ROS_DOMAIN_ID', '7')
    monkeypatch.setenv('ROS_DISCOVERY_SERVER', '127.0.0.1:11811')
    cls = sources.RecordingDeviceSources if recording else sources.DeviceSources
    manager = cls('jetson', home=tmp_path, workspace=workspace, ros_setup=setup)
    fields = ['ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'ROS_DISCOVERY_SERVER',
              'FASTRTPS_DEFAULT_PROFILES_FILE', 'RMW_FASTRTPS_USE_QOS_FROM_XML',
              'RMW_FASTRTPS_PUBLICATION_MODE']
    printer = ('import json,os;print(json.dumps('
               'dict((key,os.environ.get(key)) for key in ' + repr(fields) + ')))')
    if kind == 'driver':
        fixture = sources.Source('berxel', 'fixture-unused', (),
            (sys.executable, '-B', '-c', printer), '/fixture-unused')
        command = manager.start_command(fixture)
    else:
        manager.preview_script.parent.mkdir(parents=True)
        manager.preview_script.write_text(printer)
        command = manager.preview_start_command()
    unit = 'robot-camera-environment-test-' + uuid4().hex + '.service'
    command = [('--unit=' + unit) if part.startswith('--unit=') else part
               for part in command]
    command[1:1] = ['--wait', '--pipe', '--quiet']
    environment = dict(os.environ)
    environment.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
    environment.setdefault('DBUS_SESSION_BUS_ADDRESS',
                           'unix:path=' + environment['XDG_RUNTIME_DIR'] + '/bus')
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                env=environment, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            'ROS_DOMAIN_ID': '7' if recording else '0',
            'ROS_LOCALHOST_ONLY': '0',
            'ROS_DISCOVERY_SERVER': '127.0.0.1:11811' if recording else None,
            'FASTRTPS_DEFAULT_PROFILES_FILE': str(manager.camera_transport_profile),
            'RMW_FASTRTPS_USE_QOS_FROM_XML': '0',
            'RMW_FASTRTPS_PUBLICATION_MODE': 'ASYNCHRONOUS' if kind == 'driver' else (
                None if recording else 'SYNCHRONOUS'),
        }
    finally:
        # Only this test's unique printer unit may be cleaned up on timeout.
        subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True,
                       text=True, env=environment, timeout=5)
