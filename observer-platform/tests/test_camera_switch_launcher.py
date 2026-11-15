"""Switch launchers / forced receiver command, without production operations."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'deploy/scripts/switch_camera_owner.sh'
GATE = ROOT / 'deploy/scripts/web_receiver_gate.sh'


def test_launcher_help_never_contacts_hosts():
    result = subprocess.run(['bash', str(LAUNCHER), '--help'], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert all(word in result.stdout for word in ('web', 'peer', 'status', '--dry-run', '不会强杀'))


@pytest.mark.parametrize('args', [ ['other'], ['web', 'peer'], ['--release-pid', '1'],
                                   ['web;echo bad'], ['--unknown'] ])
def test_launcher_rejects_unknown_arguments_before_ssh(args):
    result = subprocess.run(['bash', str(LAUNCHER), *args], capture_output=True, timeout=3)
    assert result.returncode == 64


@pytest.mark.parametrize('command', ['', 'bash', 'receiver-stop', 'receiver-start extra',
                                     'receiver-start; id', 'receiver-status\nreceiver-start',
                                     'receiver-status | cat /etc/shadow'])
def test_forced_gate_rejects_shell_and_extra_commands(command):
    result = subprocess.run(['sh', str(GATE)], env={**os.environ, 'SSH_ORIGINAL_COMMAND': command},
                            capture_output=True, timeout=3)
    assert result.returncode == 64
    assert b'Only receiver-status or receiver-start' in result.stderr


def test_gate_has_fixed_start_only_boundary():
    source = GATE.read_text()
    assert 'case "${SSH_ORIGINAL_COMMAND:-}"' in source
    assert 'cd /opt/robot-cloud' in source
    assert 'up -d --no-build --pull never' in source
    assert '--wait --wait-timeout 60 backend nginx' in source
    assert 'eval ' not in source and 'sh -c' not in source
    assert 'stop ' not in source and 'down ' not in source and 'sudo ' not in source


def test_status_and_dry_run_precede_mutations():
    source = LAUNCHER.read_text()
    read_only = source.index('if [[ "$mode" == status || "$dry_run" == true ]]')
    exit_read_only = source.index('exit 0', read_only)
    mutation = source.index('"${jetson_ssh[@]}" "${remote_action[@]}"', exit_read_only)
    assert read_only < exit_read_only < mutation
    assert 'receiver-status >/dev/null' in source[:mutation]
    assert exit_read_only < source.index('receiver-start', exit_read_only) < mutation
    assert 'StrictHostKeyChecking=yes' in source
    assert 'PasswordAuthentication=no' in source
    assert 'fuser -k' not in source and 'kill ' not in source
    assert exit_read_only < source.index('exec 9>') < mutation
    assert '/usr/bin/flock -n 9' in source


def test_site_and_status_remain_available_in_peer_or_busy_mode():
    source = LAUNCHER.read_text()
    action = source.index('if ! "${jetson_ssh[@]}" "${remote_action[@]}"')
    start_site = source.index('if ! "${receiver_ssh[@]}" receiver-start')
    start_status = source.index('/usr/bin/systemctl --user start "${native_units[@]}"')
    start_probe = source.index('/usr/bin/systemctl --user start robot-cloud-camera-status-gateway.service')
    assert start_site < start_probe < start_status
    assert start_site < start_status < action
    assert '/usr/bin/systemctl --user stop' not in source
    assert '网站保持可用' in source[action:]


def test_gateway_transport_and_unit_do_not_control_capture():
    transport = (ROOT / 'deploy/scripts/native_video_tunnel.sh').read_text()
    unit = (ROOT / 'deploy/systemd/robot-cloud-camera-status-gateway.jetson.user.service').read_text()
    assert ':127.0.0.1:8081' in transport
    assert 'camera_status_gateway.py' in unit
    assert 'camera_native_web' not in unit and 'camera_owner.py web' not in unit
    assert '[Install]' not in unit


def test_launchers_shell_syntax():
    for path in (LAUNCHER, GATE):
        subprocess.run(['bash', '-n', str(path)], check=True, capture_output=True)
