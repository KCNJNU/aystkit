"""Validate the actual SSH command and server policy without any network."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def tunnel(tmp_path, *, updates=None, exit_code=0):
    fake = tmp_path / 'fake-ssh'
    fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                    'print(json.dumps(sys.argv[1:]))\n'
                    'sys.exit(int(os.environ.get("TEST_SSH_EXIT", "0")))\n')
    fake.chmod(0o700)
    script = tmp_path / 'tunnel.sh'
    script.write_text((ROOT / 'deploy/scripts/warehouse_cloud_api_tunnel.sh').read_text()
                      .replace('/usr/bin/ssh', str(fake)))
    key, known = tmp_path / 'private key', tmp_path / 'known hosts'
    key.write_text('FAKE TEST KEY')
    known.write_text('FAKE PINNED HOST')
    key.chmod(0o600)
    known.chmod(0o600)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('WAREHOUSE_')}
    environment.update(WAREHOUSE_CLOUD_KEY=str(key), WAREHOUSE_CLOUD_KNOWN_HOSTS=str(known),
                       TEST_SSH_EXIT=str(exit_code))
    environment.update(updates or {})
    return script, key, known, environment


@pytest.mark.parametrize('interface', ['', 'auto', 'fixture_wifi0'])
@pytest.mark.parametrize('legacy_alias', [None, 'true', 'false'])
@pytest.mark.parametrize('cloud_user', [None, '', 'robot-fleet-upload', 'root'])
def test_only_fixed_loopback_api_is_forwarded_with_independent_identity(tmp_path, interface, legacy_alias, cloud_user):
    updates = {'WAREHOUSE_CLOUD_BIND_INTERFACE': interface, 'WAREHOUSE_LOCAL_PORT': '5432'}
    if cloud_user is not None:
        updates['WAREHOUSE_CLOUD_USER'] = cloud_user
    if legacy_alias is not None:
        updates['WAREHOUSE_CLOUD_LEGACY_API_ALIAS'] = legacy_alias
    script, key, known, environment = tunnel(tmp_path, updates=updates)
    result = subprocess.run(['bash', str(script)], env=environment,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    arguments = json.loads(result.stdout)
    assert arguments[-1] == (cloud_user or 'robot-warehouse-upload') + '@8.140.19.17'
    forwards = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == '-L']
    expected = ['127.0.0.1:13000:127.0.0.1:13000']
    if legacy_alias != 'false':
        expected.append('127.0.0.1:3000:127.0.0.1:13000')
    assert forwards == expected
    assert {value.split(':', 2)[2] for value in forwards} == {'127.0.0.1:13000'}
    assert not {'-R', '-D', '0.0.0.0'}.intersection(arguments)
    assert arguments[arguments.index('-i') + 1] == str(key)
    assert 'UserKnownHostsFile=' + str(known) in arguments
    for option in ('StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none',
                   'ExitOnForwardFailure=yes', 'ForwardAgent=no', 'ForwardX11=no',
                   'PasswordAuthentication=no'):
        assert option in arguments
    assert '-N' in arguments and '-T' in arguments
    assert any(value.startswith('BindInterface=') for value in arguments) == bool(
        interface and interface != 'auto')


@pytest.mark.parametrize('fault', ['public-key', 'symlink-key', 'writable-hosts', 'symlink-hosts',
                                  'host-injection', 'interface-injection', 'alias-empty',
                                  'alias-case', 'alias-other', 'user-injection',
                                  'user-option'])
def test_invalid_private_configuration_rejected_before_ssh(tmp_path, fault):
    script, key, known, environment = tunnel(tmp_path)
    if fault == 'public-key':
        key.chmod(0o644)
    elif fault in ('symlink-key', 'symlink-hosts'):
        original = key if fault == 'symlink-key' else known
        target = original.with_suffix('.original')
        original.rename(target)
        original.symlink_to(target)
    elif fault == 'writable-hosts':
        known.chmod(0o666)
    elif fault == 'host-injection':
        environment['WAREHOUSE_CLOUD_HOST'] = 'host; unwanted-command'
    elif fault == 'interface-injection':
        environment['WAREHOUSE_CLOUD_BIND_INTERFACE'] = 'wifi0; unwanted-command'
    elif fault.startswith('user-'):
        environment['WAREHOUSE_CLOUD_USER'] = {
            'user-injection': 'root; unwanted-command',
            'user-option': '-oProxyCommand=unwanted-command'}[fault]
    else:
        environment['WAREHOUSE_CLOUD_LEGACY_API_ALIAS'] = {
            'alias-empty': '', 'alias-case': 'TRUE', 'alias-other': '1'}[fault]
    result = subprocess.run(['bash', str(script)], env=environment,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 2 and not result.stdout


def test_disconnect_exit_reaches_systemd_for_reconnect(tmp_path):
    script, _, _, environment = tunnel(tmp_path, exit_code=255)
    result = subprocess.run(['bash', str(script)], env=environment,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 255


@pytest.mark.skipif(not shutil.which('sshd') or not shutil.which('ssh-keygen'),
                    reason='OpenSSH configuration parser is not installed')
def test_real_sshd_parser_limits_identity_to_one_destination_and_leaves_other_users_alone(tmp_path):
    key = tmp_path / 'test-host-key'
    generated = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                               capture_output=True, text=True, timeout=3)
    assert generated.returncode == 0, generated.stderr
    config = tmp_path / 'sshd_config'
    config.write_text('HostKey ' + str(key) + '\n' +
        (ROOT / 'deploy/ssh/warehouse-upload-tunnel.conf').read_text())
    def effective(user):
        result = subprocess.run([shutil.which('sshd'), '-T', '-f', str(config), '-C',
            'user=' + user + ',host=fixture,addr=192.0.2.10'],
            capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, result.stderr
        return dict(line.split(' ', 1) for line in result.stdout.splitlines())
    restricted = effective('robot-warehouse-upload')
    assert restricted['allowtcpforwarding'] == 'local'
    assert restricted['permitopen'] == '127.0.0.1:13000'
    assert restricted['permitlisten'] == 'none'
    assert restricted['allowstreamlocalforwarding'] == 'no'
    assert restricted['forcecommand'] == '/usr/bin/false'
    assert restricted['passwordauthentication'] == 'no'
    assert restricted['permittty'] == 'no'
    assert effective('fixture-other')['forcecommand'] == 'none'
