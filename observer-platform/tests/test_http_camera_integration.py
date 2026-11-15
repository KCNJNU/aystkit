"""HTTP camera wiring regression; all launch/network/device effects are intercepted."""
from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import shlex
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

from collector import main as launcher
from collector.config import Config, HTTP_CAMERA_ORIGIN, HTTP_CAMERA_PROFILES
from collector.media import MediaStore
from collector.state import CollectorState, topic_specs
from collector.transport import Uploader
from shared.protocol import CAMERA_KEYS, HTTP_CAMERA_KEYS, MAP_KEYS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith('COLLECTOR_') or name in (
                'CLOUD_URL', 'DEVICE_TOKEN', 'ROBOT_ID', 'SOURCE_ID', 'ALLOW_INSECURE_LOCAL',
                'ROBOT_PROFILE_FILE'):
            monkeypatch.delenv(name, raising=False)
    # A real, private robot profile must remain enforced in production. These
    # unrelated HTTP fixtures must never read the developer's bound identity.
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('CLOUD_URL', 'https://isolated.invalid')
    monkeypatch.setenv('DEVICE_TOKEN', 'isolated-http-wiring-token-12345678901234567890')
    monkeypatch.setenv('ROBOT_ID', 'isolated-http-wiring')
    monkeypatch.setenv('SOURCE_ID', 'isolated-primary')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))


@pytest.mark.parametrize('audit', [False, True])
def test_http_opt_in_still_rejects_a_bound_robot_identity_mismatch(isolated_environment, monkeypatch, audit):
    from collector import robot_profile
    profile = Path.home() / '.config/robot-cloud/robot.json'
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps(dict(schemaVersion=1, observerId='a-different-robot',
        warehouseId='12345678-1234-4234-8234-123456789abc', rosDomainId=0,
        machineId='isolated-machine')))
    profile.chmod(0o600)
    validate = robot_profile.validate_current_profile
    monkeypatch.setattr(robot_profile, 'validate_current_profile',
                        lambda: validate(machine_id='isolated-machine'))
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_URL', HTTP_CAMERA_ORIGIN)
    with pytest.raises(ValueError, match='观测身份与本车配置不一致'):
        Config.from_env(audit=audit)


def enable_http(monkeypatch, profile='legacy3'):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_URL', HTTP_CAMERA_ORIGIN)
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_PROFILE', profile)
    return Config.from_env()


def test_opt_in_keeps_default_ros_keys_and_enlarges_one_upload_queue(isolated_environment, monkeypatch):
    ordinary = Config.from_env()
    assert ordinary.media_keys == Config.media_keys
    assert len(ordinary.media_keys) == 11
    assert not HTTP_CAMERA_KEYS.intersection(ordinary.media_keys)
    assert ordinary.http_camera_url == ''
    assert Uploader(ordinary).queue.max_keys == 13

    configured = enable_http(monkeypatch)
    assert len(configured.media_keys) == 14
    assert configured.http_camera_profile == ordinary.http_camera_profile == 'legacy3'
    assert set(configured.media_keys) == set(ordinary.media_keys) | set(HTTP_CAMERA_PROFILES['legacy3'])
    assert configured.source_id == ordinary.source_id
    assert configured.role == ordinary.role == 'primary'
    uploader = Uploader(configured)
    assert uploader.thread is None  # No uploader thread is started in this test.
    assert uploader.queue.max_keys == 16
    uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
    uploader.enqueue('observation', '/observer/observation',
                     {key: {} for key in ('system', 'robot', 'devices', 'ros2', 'pages', 'errors')})
    for key in configured.media_keys:
        kind = 'image' if key in CAMERA_KEYS else 'map' if key in MAP_KEYS else 'cloud'
        uploader.enqueue('media', '/isolated/' + key,
                         {'key': key, 'kind': kind, 'status': 'WAITING_UPSTREAM'})
    assert uploader.queue.stats()['keys'] == 16
    assert uploader.queue.stats()['dropped'] == 0
    assert {'heartbeat', 'observation'} <= set(uploader.queue.entries)


def test_http_only_rate_requires_enabled_receiver_and_preserves_ros_rate(isolated_environment, monkeypatch):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_FPS', '2')
    with pytest.raises(ValueError, match='explicitly enabled'):
        Config.from_env()
    config = enable_http(monkeypatch, 'jetson-hub4')
    assert config.http_camera_fps == 2 and config.image_fps == 1
    monkeypatch.delenv('COLLECTOR_HTTP_CAMERA_FPS')
    assert enable_http(monkeypatch, 'jetson-hub4').http_camera_fps is None


@pytest.mark.parametrize('fps', ['0', '2.1', 'nan', 'inf', 'true'])
def test_http_only_rate_rejects_invalid_env_values(isolated_environment, monkeypatch, fps):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_FPS', fps)
    with pytest.raises(ValueError):
        enable_http(monkeypatch, 'jetson-hub4')


def test_explicit_hub_profile_adds_four_audited_ports_not_a_rear_alias(isolated_environment, monkeypatch):
    config = enable_http(monkeypatch, 'jetson-hub4')
    assert len(config.media_keys) == 15
    assert HTTP_CAMERA_KEYS.intersection(config.media_keys) == {'usbFront', 'usbLeft', 'usbHub2', 'usbHub3'}
    assert 'usbRear' not in config.media_keys
    assert Uploader(config).queue.max_keys == 17
    monkeypatch.setenv('COLLECTOR_MEDIA_KEYS', 'usbHub2,usbHub3')
    subset = Config.from_env()
    assert subset.media_keys == ('usbHub2', 'usbHub3')
    assert Uploader(subset).queue.max_keys == 4
    with pytest.raises(ValueError, match='not enabled'):
        Uploader(subset).enqueue('media', '/http_camera/camera1',
                                {'key': 'usbRear', 'kind': 'image', 'status': 'WAITING_UPSTREAM'})


def test_explicit_direct_profile_keeps_disconnected_hub_and_rear_keys_disabled(isolated_environment, monkeypatch):
    config = enable_http(monkeypatch, 'jetson-direct3')
    assert len(config.media_keys) == 14
    assert HTTP_CAMERA_KEYS.intersection(config.media_keys) == {'usbFront', 'usbLeft', 'usbPort21'}
    assert not {'usbRear', 'usbHub2', 'usbHub3'}.intersection(config.media_keys)
    uploader = Uploader(config)
    assert uploader.queue.max_keys == 16
    for key in ('usbRear', 'usbHub2', 'usbHub3'):
        with pytest.raises(ValueError, match='not enabled'):
            uploader.enqueue('media', '/isolated/not-enabled',
                             {'key': key, 'kind': 'image', 'status': 'WAITING_UPSTREAM'})


@pytest.mark.parametrize('audit', [False, True])
@pytest.mark.parametrize('profile,url,role,keys', [
    ('', HTTP_CAMERA_ORIGIN, 'primary', None), ('hub', HTTP_CAMERA_ORIGIN, 'primary', None),
    ('legacy', HTTP_CAMERA_ORIGIN, 'primary', None), ('unknown', '', 'primary', None),
    ('jetson-hub4', '', 'primary', None), ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'cameras', None),
    ('legacy3', HTTP_CAMERA_ORIGIN, 'primary', 'usbHub2'),
    ('legacy3', HTTP_CAMERA_ORIGIN, 'primary', 'usbHub3'),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'primary', 'usbRear'),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'primary', 'usbFront,usbRear,usbHub2'),
    ('jetson-direct3', '', 'primary', None), ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'cameras', None),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', 'usbRear'),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', 'usbHub2'),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', 'usbHub3'),
    ('legacy3', HTTP_CAMERA_ORIGIN, 'primary', 'usbPort21'),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'primary', 'usbPort21'),
])
def test_profile_validation_is_fail_closed_even_during_audit(isolated_environment, monkeypatch, audit, profile, url, role, keys):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_PROFILE', profile)
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_URL', url)
    monkeypatch.setenv('COLLECTOR_ROLE', role)
    if keys is not None:
        monkeypatch.setenv('COLLECTOR_MEDIA_KEYS', keys)
    with pytest.raises(ValueError):
        Config.from_env(audit=audit)


@pytest.mark.parametrize('profile,url,role,keys', [
    ('unknown', HTTP_CAMERA_ORIGIN, 'primary', ('usbFront',)),
    ('jetson-hub4', '', 'primary', ('usbHub2',)),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'cameras', ('usbHub2',)),
    ('legacy3', HTTP_CAMERA_ORIGIN, 'primary', ('usbHub2',)),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'primary', ('usbRear',)),
    ('jetson-direct3', '', 'primary', ('usbPort21',)),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'cameras', ('usbPort21',)),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', ('usbHub2',)),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', ('usbHub3',)),
    ('jetson-direct3', HTTP_CAMERA_ORIGIN, 'primary', ('usbRear',)),
    ('legacy3', HTTP_CAMERA_ORIGIN, 'primary', ('usbPort21',)),
    ('jetson-hub4', HTTP_CAMERA_ORIGIN, 'primary', ('usbPort21',)),
])
def test_direct_configuration_cannot_bypass_profile_ownership(profile, url, role, keys):
    with pytest.raises(ValueError):
        Config('test-only', 'test-only', role=role, media_keys=keys,
               http_camera_url=url, http_camera_profile=profile)


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:18080/', 'http://localhost:18080', 'http://10.48.106.37:8080',
    'https://127.0.0.1:18080', 'http://127.0.0.1:18081',
    'http://127.0.0.1:18080?target=external', 'http://127.0.0.1:18080#fragment',
    'http://user:password@127.0.0.1:18080',
])
def test_environment_rejects_noncanonical_http_origin(isolated_environment, monkeypatch, url):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_URL', url)
    with pytest.raises(ValueError, match='fixed loopback'):
        Config.from_env()


@pytest.mark.parametrize('url,role,keys', [
    ('', 'primary', 'usbFront'),
    (HTTP_CAMERA_ORIGIN, 'cameras', None),
    (HTTP_CAMERA_ORIGIN, 'cameras', 'usbRear'),
    (HTTP_CAMERA_ORIGIN, 'primary', 'fisheye'),
    (HTTP_CAMERA_ORIGIN, 'primary', ''),
    (HTTP_CAMERA_ORIGIN, 'primary', 'usbFront,usbFront'),
    (HTTP_CAMERA_ORIGIN, 'primary', 'usbRight'),
])
def test_environment_fail_closed_role_and_explicit_allowlist(isolated_environment, monkeypatch, url, role, keys):
    monkeypatch.setenv('COLLECTOR_HTTP_CAMERA_URL', url)
    monkeypatch.setenv('COLLECTOR_ROLE', role)
    if keys is not None:
        monkeypatch.setenv('COLLECTOR_MEDIA_KEYS', keys)
    with pytest.raises(ValueError):
        Config.from_env()


def test_explicit_subset_does_not_auto_grant_other_cameras(isolated_environment, monkeypatch):
    monkeypatch.setenv('COLLECTOR_MEDIA_KEYS', 'fisheye,usbRear')
    config = enable_http(monkeypatch)
    assert config.media_keys == ('fisheye', 'usbRear')
    assert Uploader(config).queue.max_keys == 4
    with pytest.raises(ValueError, match='not enabled'):
        Uploader(config).enqueue('media', '/isolated/usbFront',
                                 {'key': 'usbFront', 'kind': 'image', 'status': 'WAITING_UPSTREAM'})


@pytest.mark.parametrize('profile', ['legacy3', 'jetson-hub4', 'jetson-direct3'])
def test_ros_media_and_observation_never_claim_http_camera_keys(isolated_environment, monkeypatch, profile):
    config = enable_http(monkeypatch, profile)
    media = MediaStore(config)
    assert set(media.keys) == set(Config.media_keys)
    assert not HTTP_CAMERA_KEYS.intersection(topic_specs(config))
    # Even an accidental ROS delivery cannot populate an HTTP identity.
    for key in HTTP_CAMERA_KEYS:
        media.receive(key, object(), '/must-not-be-a-ros-camera', compressed=True)
    assert not media.items and not media.errors and not media.dirty
    cameras, visuals = media.snapshots({})
    assert set(cameras) == CAMERA_KEYS - HTTP_CAMERA_KEYS
    assert not HTTP_CAMERA_KEYS.intersection(visuals)

    uploader = Uploader(config)
    media.publish(uploader, {key: {'topic': '/isolated/' + key} for key in config.media_keys})
    uploaded = {entry.envelope['payload']['key'] for entry in uploader.queue.entries.values()}
    assert uploaded == set(Config.media_keys)
    state = CollectorState(config, collector=NS(collect=lambda: {'status': 'AVAILABLE'}))
    observation = state.observation()
    assert not HTTP_CAMERA_KEYS.intersection(state.bridge_specs)
    assert not HTTP_CAMERA_KEYS.intersection(observation['devices']['sensors'])
    assert set(observation['devices']['cameras']) == CAMERA_KEYS - HTTP_CAMERA_KEYS
    assert not any('/http_camera/' in topic for topic in observation['ros2']['topics'])


def intercepted_ros(monkeypatch, events):
    """Install a fake ROS runtime; no node, discovery, or device interface is opened."""
    runtime, executors = ModuleType('rclpy'), ModuleType('rclpy.executors')
    executors.ExternalShutdownException = type('ExternalShutdownException', (Exception,), {})
    running = [True]
    runtime.init = lambda **kwargs: events.append('ros.init')
    runtime.ok = lambda: running[0]
    def shutdown():
        events.append('ros.shutdown')
        running[0] = False
    runtime.shutdown = shutdown
    runtime.spin_once = lambda *args, **kwargs: events.append('ros.spin_once')
    def spin(node):
        events.append('ros.spin')
        raise KeyboardInterrupt()
    runtime.spin = spin
    monkeypatch.setitem(sys.modules, 'rclpy', runtime)
    monkeypatch.setitem(sys.modules, 'rclpy.executors', executors)
    monkeypatch.setattr(launcher, 'CollectorState', lambda config: NS(config=config, local_chassis=None))
    node = NS(audit=lambda: {'mode': 'READ_ONLY_AUDIT', 'cloud_connected': False},
              destroy_node=lambda: events.append('node.destroy'))
    return node


@pytest.mark.parametrize('profile', ['legacy3', 'jetson-hub4', 'jetson-direct3'])
def test_audit_never_constructs_http_receiver_or_uploader(isolated_environment, monkeypatch, capsys, profile):
    enable_http(monkeypatch, profile)
    # Audit remains valid without cloud credentials even if HTTP input is configured.
    monkeypatch.delenv('CLOUD_URL')
    monkeypatch.delenv('DEVICE_TOKEN')
    events = []
    node = intercepted_ros(monkeypatch, events)
    def forbidden(*args, **kwargs):
        pytest.fail('Audit must not construct or start any HTTP receiver/uploader')
    monkeypatch.setattr(launcher, 'Uploader', forbidden)
    optional_module = ModuleType('collector.http_camera')
    optional_module.HttpCameraCollector = forbidden
    monkeypatch.setitem(sys.modules, 'collector.http_camera', optional_module)
    def build(state, uploader):
        assert uploader is None
        assert state.config.http_camera_url == HTTP_CAMERA_ORIGIN
        return node
    monkeypatch.setattr(launcher, 'build_node', build)
    ticks = iter((0, 0, .01, .11))
    monkeypatch.setattr(launcher, 'time', NS(monotonic=lambda: next(ticks)))
    launcher.main(['--audit-seconds', '0.1'])
    assert json.loads(capsys.readouterr().out)['cloud_connected'] is False
    assert events == ['ros.init', 'ros.spin_once', 'node.destroy', 'ros.shutdown']


@pytest.mark.parametrize('build_failure', [False, True])
@pytest.mark.parametrize('profile', ['legacy3', 'jetson-hub4', 'jetson-direct3'])
def test_http_receiver_stops_before_shared_uploader(isolated_environment, monkeypatch, build_failure, profile):
    enable_http(monkeypatch, profile)
    events = []
    node = intercepted_ros(monkeypatch, events)
    uploader = NS(start=lambda: events.append('uploader.start'), stop=lambda: events.append('uploader.stop'),
                  enable_events=lambda *args: None, enable_test_metrics=lambda *args: None)
    receiver = NS(start=lambda: events.append('http.start'), stop=lambda: events.append('http.stop'))
    monkeypatch.setattr(launcher, 'Uploader', lambda config: uploader)
    optional_module = ModuleType('collector.http_camera')
    def http_factory(config, shared_uploader):
        assert shared_uploader is uploader
        assert set(HTTP_CAMERA_PROFILES[profile]) <= set(config.media_keys)
        return receiver
    optional_module.HttpCameraCollector = http_factory
    monkeypatch.setitem(sys.modules, 'collector.http_camera', optional_module)
    def build(state, shared_uploader):
        assert shared_uploader is uploader
        if build_failure:
            raise RuntimeError('isolated node construction failure')
        return node
    monkeypatch.setattr(launcher, 'build_node', build)
    if build_failure:
        with pytest.raises(RuntimeError, match='isolated node'):
            launcher.main([])
    else:
        launcher.main([])
    assert events.index('uploader.start') < events.index('http.start')
    assert events.index('http.stop') < events.index('uploader.stop') < events.index('ros.shutdown')
    assert events.count('http.stop') == events.count('uploader.stop') == 1
    assert ('node.destroy' in events) is not build_failure


def test_tunnel_template_exposes_only_fixed_loopback_with_pinned_host_key():
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(ROOT / 'deploy/systemd/robot-cloud-camera-tunnel.user.service')
    command = shlex.split(unit['Service']['ExecStart'])
    assert command[0] == '/usr/bin/ssh'
    assert command[command.index('-F') + 1] == '/dev/null'
    assert '-NT' in command
    assert command.count('-L') == 1
    assert command[command.index('-L') + 1] == '127.0.0.1:18080:127.0.0.1:18081'
    assert not {'-g', '-A', '-X', '-Y', '-R', '-D'} & set(command)
    options = [command[index + 1] for index, item in enumerate(command) if item == '-o']
    for expected in ('BatchMode=yes', 'PasswordAuthentication=no', 'IdentitiesOnly=yes',
                     'StrictHostKeyChecking=yes', 'HostKeyAlgorithms=ssh-ed25519',
                     'UserKnownHostsFile=%h/.ssh/known_hosts', 'HostKeyAlias=10.48.106.96',
                     'ForwardAgent=no', 'ForwardX11=no', 'ExitOnForwardFailure=yes'):
        assert expected in options
    assert command[-1] == 'nvidia@${JETSON_CAMERA_HOST}'
    assert unit['Service']['NoNewPrivileges'] == 'true'
    assert unit['Service']['UMask'] == '0077'
