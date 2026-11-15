"""Exercise real shell argument selection using a fake SSH executable, no network."""
import configparser
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_tunnel(tmp_path, name, interface=None, exit_code=0):
    fake = tmp_path/'fixture-ssh'
    fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ.get("FIXTURE_SSH_EXIT","0")))\n')
    fake.chmod(0o700)
    script = tmp_path/name
    script.write_text((ROOT/'deploy/scripts'/name).read_text().replace('/usr/bin/ssh', str(fake)))
    key, hosts = tmp_path/'fixture-key', tmp_path/'fixture-known-hosts'
    key.write_text('NOT_A_REAL_PRIVATE_KEY\n')
    hosts.write_text('FIXTURE_HOST_ONLY\n')
    env = dict(os.environ, VIDEO_CLOUD_KEY=str(key), VIDEO_CLOUD_KNOWN_HOSTS=str(hosts),
               FIXTURE_SSH_EXIT=str(exit_code))
    env.pop('VIDEO_CLOUD_BIND_INTERFACE', None)
    if interface is not None:
        env['VIDEO_CLOUD_BIND_INTERFACE'] = interface
    args = ['cloud', 'usbFront'] if name == 'native_video_tunnel.sh' else []
    return subprocess.run(['bash', str(script), *args], env=env, capture_output=True, text=True, timeout=3)


@pytest.mark.parametrize('name', ['native_video_tunnel.sh', 'dbprog_tunnel.sh'])
@pytest.mark.parametrize('interface', [None, '', 'auto'])
def test_default_and_auto_follow_os_route_instead_of_old_wifi(tmp_path, name, interface):
    result = run_tunnel(tmp_path, name, interface)
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert not any('BindInterface=' in item or 'BindAddress=' in item for item in args)
    assert 'StrictHostKeyChecking=yes' in args
    assert 'IdentitiesOnly=yes' in args and 'ExitOnForwardFailure=yes' in args
    assert 'ForwardAgent=no' in args and 'PasswordAuthentication=no' in args
    assert args[-1] == 'robot-video-tunnel@8.140.19.17'
    assert '-N' in args and '-T' in args
    expected = 1 if name == 'native_video_tunnel.sh' else 2
    assert args.count('-R') == expected
    assert not any('0.0.0.0' in item for item in args)


@pytest.mark.parametrize('name', ['native_video_tunnel.sh', 'dbprog_tunnel.sh'])
def test_operator_can_still_explicitly_pin_an_interface(tmp_path, name):
    result = run_tunnel(tmp_path, name, 'fixture_wifi0')
    assert result.returncode == 0
    assert 'BindInterface=fixture_wifi0' in json.loads(result.stdout)


@pytest.mark.parametrize('name', ['native_video_tunnel.sh', 'dbprog_tunnel.sh'])
def test_invalid_bind_interface_is_rejected_before_ssh(tmp_path, name):
    result = run_tunnel(tmp_path, name, 'bad interface;fixture')
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize('name', ['native_video_tunnel.sh', 'dbprog_tunnel.sh'])
def test_ssh_failure_reaches_supervisor_instead_of_being_swallowed(tmp_path, name):
    result = run_tunnel(tmp_path, name, 'auto', exit_code=255)
    assert result.returncode == 255


@pytest.mark.parametrize('name', ['robot-cloud-native-cloud-tunnel@.user.service',
    'robot-cloud-native-source-tunnel@.user.service', 'robot-cloud-dbprog-tunnel.user.service'])
def test_existing_tunnels_reconnect_on_clean_or_failed_exit_without_new_autostart(name):
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(ROOT/'deploy/systemd'/name)
    assert unit['Service']['Restart'] == 'always'
    assert unit['Service']['RestartSec'] == '5'
    assert unit['Unit']['StartLimitIntervalSec'] == '0'
    assert 'ExecStartPre' not in unit['Service'] and 'ExecStartPost' not in unit['Service']


@pytest.mark.parametrize('host,alias,expected,expected_alias', [
    ('auto', None, 'nvidia-desktop.local', '192.168.1.50'),
    ('192.0.2.12', None, '192.0.2.12', None),
    ('fixture.local', 'trusted-fixture-key', 'fixture.local', 'trusted-fixture-key')])
def test_source_dynamic_hostname_reuses_only_the_pinned_identity(tmp_path, host, alias, expected, expected_alias):
    fake = tmp_path/'fixture-ssh'
    fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    fake.chmod(0o700)
    script = tmp_path/'native-video'
    script.write_text((ROOT/'deploy/scripts/native_video_tunnel.sh').read_text().replace('/usr/bin/ssh', str(fake)))
    env = dict(os.environ, VIDEO_SOURCE_HOST=host, VIDEO_SOURCE_USER='fixture',
               VIDEO_SOURCE_KEY='/FIXTURE/key', VIDEO_SOURCE_KNOWN_HOSTS='/FIXTURE/pinned-hosts')
    env.pop('VIDEO_SOURCE_HOST_KEY_ALIAS', None)
    env.pop('VIDEO_SOURCE_NAME', None)
    if alias:
        env['VIDEO_SOURCE_HOST_KEY_ALIAS'] = alias
    result = subprocess.run(['bash', str(script), 'source', 'usbFront'], env=env,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert args[-1] == 'fixture@'+expected
    assert 'StrictHostKeyChecking=yes' in args and 'UserKnownHostsFile=/FIXTURE/pinned-hosts' in args
    if expected_alias:
        assert 'HostKeyAlias='+expected_alias in args
    else:
        assert not any('HostKeyAlias=' in item for item in args)
    assert '127.0.0.1:18082:127.0.0.1:8081' in args
