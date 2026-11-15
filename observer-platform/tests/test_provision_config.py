"""Production provisioning tests use only isolated temporary configuration."""
import json
import os
import shlex
import stat

import pytest

from cloud.backend.security import Config, verify_password
from collector.config import Config as CollectorConfig
from deploy.scripts import provision_config as helper


def env_values(path):
    return {key: shlex.split(value)[0]
            for key, value in (line.split('=', 1) for line in path.read_text().splitlines())}


def test_private_config_is_valid_and_separates_credentials(tmp_path, monkeypatch, capsys):
    helper.main(['--project-dir', str(tmp_path), '--domain', 'Example.test',
                 '--robot-id', 'rk3588-robot', '--source-id', 'rk3588'])
    cloud = env_values(tmp_path / '.env')
    collector = env_values(tmp_path / '.runtime/collector.env')
    access = json.loads((tmp_path / '.runtime/initial-access.json').read_text())
    assert set(access) == {'username', 'password'}
    assert access['username'] == 'observer'
    assert len(access['password']) >= 24
    assert verify_password(access['password'], cloud['VIEWER_PASSWORD_HASH'])
    devices = json.loads(cloud['DEVICE_CREDENTIALS_JSON'])
    assert devices[0]['token'] == collector['DEVICE_TOKEN']
    assert len(collector['DEVICE_TOKEN']) >= 64
    assert len(cloud['SESSION_SECRET']) >= 64
    assert len({collector['DEVICE_TOKEN'], cloud['SESSION_SECRET'], access['password']}) == 3
    assert set(collector) == {'CLOUD_URL', 'ROBOT_ID', 'SOURCE_ID', 'DEVICE_TOKEN', 'COLLECTOR_ROLE', 'ALLOW_INSECURE_LOCAL'}
    for value in (access['password'], cloud['SESSION_SECRET'], cloud['VIEWER_PASSWORD_HASH']):
        assert value not in (tmp_path / '.runtime/collector.env').read_text()
    assert stat.S_IMODE((tmp_path / '.runtime').stat().st_mode) == 0o700
    for target in (tmp_path / '.env', tmp_path / '.runtime/initial-access.json', tmp_path / '.runtime/collector.env'):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    for key, value in cloud.items():
        monkeypatch.setenv(key, value)
    config = Config.from_env().validate()
    assert config.public_origin == 'https://example.test'
    for key, value in collector.items():
        monkeypatch.setenv(key, value)
    robot = CollectorConfig.from_env()
    assert robot.robot_id == config.robot_id == 'rk3588-robot'
    assert robot.source_id == 'rk3588'
    assert robot.cloud_url == 'https://example.test/api/v1/ingest'
    output = capsys.readouterr().out
    for value in (access['password'], collector['DEVICE_TOKEN'], cloud['SESSION_SECRET'], cloud['VIEWER_PASSWORD_HASH']):
        assert value not in output


@pytest.mark.parametrize('existing', ['.env', '.runtime/initial-access.json', '.runtime/collector.env'])
def test_refuses_all_existing_targets_without_partial_files(tmp_path, existing):
    if existing.startswith('.runtime/'):
        (tmp_path / '.runtime').mkdir(mode=0o700)
    target = tmp_path / existing
    target.write_text('preserve exactly')
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*'))
    with pytest.raises(FileExistsError):
        helper.provision(tmp_path, 'example.test', 'robot', 'rk3588')
    assert target.read_text() == 'preserve exactly'
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob('*')) == before


@pytest.mark.parametrize('domain', ['https://example.test', 'example.test\nSESSION_SECRET=x',
                                    'x.test\r', 'x.test/', "x.test'", 'a..test', '-a.test',
                                    'a-.test', '127.0.0.1', 'localhost', 'a' * 64 + '.test'])
def test_domain_injection_is_rejected_without_writes(tmp_path, domain):
    with pytest.raises(ValueError):
        helper.provision(tmp_path, domain, 'robot', 'rk3588')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('field', ['robot_id', 'source_id', 'viewer_username'])
def test_identity_injection_is_rejected(tmp_path, field):
    values = {'robot_id': 'robot', 'source_id': 'rk3588', 'viewer_username': 'observer'}
    values[field] = 'value\nBAD=true'
    with pytest.raises(ValueError):
        helper.provision(tmp_path, 'example.test', **values)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['project', 'ancestor', 'runtime', 'env', 'collector'])
def test_symlinks_never_followed(tmp_path, kind):
    project = tmp_path / 'project'
    project.mkdir(mode=0o700)
    destination = tmp_path / 'destination'
    destination.mkdir(mode=0o700)
    if kind == 'project':
        project.rmdir()
        project.symlink_to(destination, target_is_directory=True)
    elif kind == 'ancestor':
        project.rmdir()
        project.symlink_to(destination, target_is_directory=True)
        destination.joinpath('nested').mkdir(mode=0o700)
        project = project / 'nested'
    elif kind == 'runtime':
        project.joinpath('.runtime').symlink_to(destination, target_is_directory=True)
    elif kind == 'env':
        project.joinpath('.env').symlink_to(destination / 'missing')
    else:
        project.joinpath('.runtime').mkdir(mode=0o700)
        project.joinpath('.runtime/collector.env').symlink_to(destination / 'missing')
    with pytest.raises((OSError, ValueError)):
        helper.provision(project, 'example.test', 'robot', 'rk3588')
    assert not destination.joinpath('missing').exists()
    assert not project.joinpath('.env').exists()
    assert not project.joinpath('.runtime/initial-access.json').exists()


@pytest.mark.parametrize('kind', ['project', 'ancestor', 'runtime'])
def test_unsafe_directory_permissions_rejected(tmp_path, kind):
    project = tmp_path / 'project'
    project.mkdir(mode=0o700)
    if kind == 'project':
        project.chmod(0o777)
    elif kind == 'ancestor':
        project.chmod(0o777)
        project = project / 'nested'
        project.mkdir(mode=0o700)
    else:
        project.joinpath('.runtime').mkdir(mode=0o755)
    with pytest.raises(ValueError):
        helper.provision(project, 'example.test', 'robot', 'rk3588')
    assert not project.joinpath('.env').exists()


def test_partial_failure_removes_only_this_calls_files(tmp_path, monkeypatch):
    runtime = tmp_path / '.runtime'
    runtime.mkdir(mode=0o700)
    runtime.joinpath('keep.txt').write_text('untouched')
    original = os.open
    def fail_second(name, flags, *args, **kwargs):
        if name == 'initial-access.json' and flags & os.O_CREAT:
            raise OSError('simulated write failure')
        return original(name, flags, *args, **kwargs)
    monkeypatch.setattr(helper.os, 'open', fail_second)
    with pytest.raises(OSError):
        helper.provision(tmp_path, 'example.test', 'robot', 'rk3588')
    assert not tmp_path.joinpath('.env').exists()
    assert runtime.joinpath('keep.txt').read_text() == 'untouched'
    assert list(runtime.iterdir()) == [runtime / 'keep.txt']


def test_partial_failure_removes_new_private_directory(tmp_path, monkeypatch):
    def fail_hash(_password):
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(helper, 'hash_password', fail_hash)
    with pytest.raises(RuntimeError):
        helper.provision(tmp_path, 'example.test', 'robot', 'rk3588')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('path', ['/', 'relative/project', '/tmp/../unsafe'])
def test_root_relative_and_parent_traversal_paths_rejected(path):
    with pytest.raises(ValueError):
        helper.provision(path, 'example.test', 'robot', 'rk3588')


def test_untrusted_directory_owner_rejected():
    info = os.stat_result((stat.S_IFDIR | 0o755, 1, 1, 1, 987654, 987654, 0, 0, 0, 0))
    with pytest.raises(ValueError):
        helper._directory_safe(info)
