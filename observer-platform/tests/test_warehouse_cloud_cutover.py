"""Cloud cutover cannot revive a retired local writable warehouse."""
import itertools
import json
import os

import pytest

from tests.test_start_all_devices import startup  # noqa: F401
from tests.test_stop_all_devices import shutdown  # noqa: F401


def cloud_marker(home):
    path = home / '.config/robot-cloud/warehouse-cloud.json'
    path.write_text(json.dumps(dict(schemaVersion=1, mode='cloud',
        apiUrl='http://127.0.0.1:13000/api', publicUrl='https://aystkit.cn/dbprog/',
        unit='robot-warehouse-cloud-api-tunnel.service')))
    path.chmod(0o600)
    return path


def test_cloud_full_start_only_uses_cloud_tunnel_and_keeps_device_collection(startup, monkeypatch):
    module, fake, home = startup
    cloud_marker(home)
    health = []
    monkeypatch.setattr(module, 'http_ready', lambda url: health.append(url) or True)
    assert module.main(['--all-devices']) == 0
    assert not any(command[0] == 'docker' for command in fake.commands())
    assert 'robot-cloud-dbprog-tunnel.service' not in fake.started_units()
    assert 'robot-warehouse-cloud-api-tunnel.service' in fake.started_units()
    assert {'robot-native-archive.service', 'robot-native-archive-source-link.service',
            'robot-cloud-collector.service'}.issubset(fake.started_units())
    assert any(command[0] == 'ssh' for command in fake.commands())
    assert health == ['http://127.0.0.1:13000/ready']


def test_cloud_status_does_not_start_units_or_consult_old_docker_warehouse(startup):
    module, fake, home = startup
    cloud_marker(home)
    fake.active.update(module.LOCAL_UNITS + module.TUNNEL_UNITS)
    fake.active.add('robot-warehouse-cloud-api-tunnel.service')
    assert module.main(['--all-devices', '--status']) == 0
    assert not any(command[0] == 'docker' for command in fake.commands())
    assert not any({'start', 'stop', 'restart'}.intersection(command)
                   for command in fake.commands())


def test_cloud_no_warehouse_keeps_upload_transport_without_reviving_reverse_tunnel(startup):
    module, fake, home = startup
    cloud_marker(home)
    assert module.main(['--all-devices', '--no-warehouse']) == 0
    assert 'robot-warehouse-cloud-api-tunnel.service' in fake.started_units()
    assert 'robot-cloud-dbprog-tunnel.service' not in fake.started_units()
    assert not any(command[0] == 'docker' for command in fake.commands())


def test_cloud_dry_run_reports_cutover_without_commands_or_new_state(startup, capsys):
    module, fake, home = startup
    cloud_marker(home)
    assert module.main(['--all-devices', '--dry-run']) == 0
    assert fake.commands() == []
    assert not (home / '.local/state/robot-cloud').exists()
    text = capsys.readouterr().out
    assert '云数仓' in text and '旧仓容器保持停用' in text
    assert 'robot-cloud-dbprog-tunnel.service' not in text


def test_unreachable_cloud_keeps_local_collection_and_never_falls_back(startup, monkeypatch):
    module, fake, home = startup
    cloud_marker(home)
    monkeypatch.setattr(module, 'http_ready', lambda _: False)
    clock = itertools.count(0, 2)
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock))
    assert module.main(['--all-devices', '--ready-timeout', '1']) == 1
    assert 'robot-native-archive.service' in fake.started_units()
    assert 'robot-cloud-collector.service' in fake.started_units()
    assert 'robot-cloud-dbprog-tunnel.service' not in fake.started_units()
    assert not any(command[0] == 'docker' for command in fake.commands())


@pytest.mark.parametrize('fault', ['json', 'schema-bool', 'api', 'unit', 'public',
                                  'permissions', 'symlink', 'dangling', 'hardlink'])
def test_invalid_marker_fails_closed_before_any_external_command(startup, fault):
    module, fake, home = startup
    path = cloud_marker(home)
    if fault == 'json':
        path.write_text('{')
    elif fault == 'permissions':
        path.chmod(0o644)
    elif fault in ('symlink', 'dangling'):
        target = path.with_suffix('.original')
        path.rename(target)
        path.symlink_to(target)
        if fault == 'dangling':
            target.unlink()
    elif fault == 'hardlink':
        os.link(path, path.with_suffix('.copy'))
    else:
        value = json.loads(path.read_text())
        key, replacement = {
            'schema-bool': ('schemaVersion', True), 'api': ('apiUrl', 'http://127.0.0.1:3000/api'),
            'unit': ('unit', 'robot-cloud-dbprog-tunnel.service'),
            'public': ('publicUrl', 'https://other.example/dbprog/'),
        }[fault]
        value[key] = replacement
        path.write_text(json.dumps(value))
    assert module.main(['--all-devices']) == 1
    assert fake.commands() == []


def test_cloud_shutdown_preserves_upload_tunnel_and_never_stops_either_warehouse(shutdown):
    module, fake, home = shutdown
    cloud_marker(home)
    fake.active.add('robot-warehouse-cloud-api-tunnel.service')
    assert module.main(['--stop']) == 0
    assert 'robot-warehouse-cloud-api-tunnel.service' in fake.active
    assert 'robot-warehouse-cloud-api-tunnel.service' not in fake.stopped_units()
    assert 'robot-cloud-dbprog-tunnel.service' not in fake.stopped_units()
    assert not any(command[0] == 'docker' for command in fake.commands())


def test_cloud_stop_dry_run_does_not_describe_or_execute_old_warehouse_shutdown(shutdown, capsys):
    module, fake, home = shutdown
    cloud_marker(home)
    assert module.main(['--stop', '--dry-run']) == 0
    assert fake.commands() == []
    assert '云数仓及上传隧道保持运行' in capsys.readouterr().out
