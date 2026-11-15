"""Explicit chassis authority and lifecycle, without ROS or production sockets."""
import json
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

from collector.config import Config
from collector.state import CollectorState


def frame(page=0x10):
    data = bytearray([0x7b, page] + [0] * 13 + [0, 0x7d])
    for value in data[:15]:
        data[15] ^= value
    return {'type': 'rx', 'hex': data.hex()}


def ros_payload(**values):
    return json.dumps({'telemetryTransportSchema': 1, 'telemetrySourceNode': '/chassis_host', **values})


def state(source='ros2', clock=lambda: 0):
    return CollectorState(Config('test-only-robot', 'test-only-source', chassis_source=source),
                          collector=NS(collect=lambda: {'status': 'AVAILABLE'}), clock=clock)


def test_source_default_env_and_explicit_local(monkeypatch):
    monkeypatch.delenv('COLLECTOR_CHASSIS_SOURCE', raising=False)
    monkeypatch.setenv('COLLECTOR_ROLE', 'primary')
    assert Config.from_env(audit=True).chassis_source == 'ros2'
    assert state().local_chassis is None
    monkeypatch.setenv('COLLECTOR_CHASSIS_SOURCE', 'local_ws')
    assert Config.from_env(audit=True).chassis_source == 'local_ws'
    assert state('local_ws').local_chassis.thread is None


@pytest.mark.parametrize('source', ['', 'auto', 'fallback', 'ws://127.0.0.1:8765/ws'])
def test_source_rejects_unknown_values_from_env_and_constructor(monkeypatch, source):
    monkeypatch.setenv('COLLECTOR_CHASSIS_SOURCE', source)
    with pytest.raises(ValueError, match='COLLECTOR_CHASSIS_SOURCE'):
        Config.from_env(audit=True)
    with pytest.raises(ValueError, match='COLLECTOR_CHASSIS_SOURCE'):
        Config('test-only-robot', 'test-only-source', chassis_source=source)


def test_camera_role_rejects_local_source_even_with_direct_config(monkeypatch):
    monkeypatch.setenv('COLLECTOR_ROLE', 'cameras')
    monkeypatch.setenv('COLLECTOR_CHASSIS_SOURCE', 'local_ws')
    with pytest.raises(ValueError, match='primary'):
        Config.from_env(audit=True)
    with pytest.raises(ValueError, match='primary'):
        Config('test-only-robot', 'test-only-source', role='cameras', chassis_source='local_ws')


def test_local_selection_preserves_real_ros_diagnostics_and_errors():
    store = state('local_ws')
    store.local_chassis.connected = True
    store.local_chassis.receive(frame())
    observation = store.observation()
    assert observation['robot']['status'] == 'AVAILABLE'
    assert observation['robot']['source'] == '本机底盘服务只读广播 · ws://127.0.0.1:8765/ws'
    assert observation['ros2']['topics']['/chassis_host/telemetry']['status'] == 'WAITING_UPSTREAM'
    assert observation['ros2']['status'] == 'WAITING_UPSTREAM'
    assert 'payload' not in observation['devices']['localChassis']
    store.telemetry.receive('malformed test-only input')
    store.local_chassis.receive({'type': 'rx', 'hex': 'bad'})
    observation = store.observation()
    assert observation['robot']['status'] == 'AVAILABLE'
    assert observation['ros2']['topics']['/chassis_host/telemetry']['status'] == 'ERROR'
    assert observation['errors']['telemetryParseCount'] == 1
    assert observation['errors']['localChassisParseCount'] == 1


def test_selected_local_never_falls_back_or_combines_ros_pages_or_fields():
    now = [0.0]
    store = state('local_ws', clock=lambda: now[0])
    store.telemetry.receive(ros_payload(actualVxMps=77, targetVxMps=99,
                                       serialDebug={'telemetryPages': {'0x11': {'count': 50, 'ageMs': 0}}}))
    observation = store.observation()
    assert observation['robot']['status'] == 'WAITING_UPSTREAM'
    assert observation['robot']['data'] is None
    assert observation['pages']['0x11']['status'] == 'WAITING_UPSTREAM'
    store.local_chassis.connected = True
    store.local_chassis.receive(frame())
    observation = store.observation()
    assert observation['robot']['data']['debug']['fields']['actualVxMps']['value'] == 0
    assert 'targetVxMps' not in observation['robot']['data']
    assert observation['pages']['0x11']['count'] == 0
    now[0] = 3
    store.telemetry.receive(ros_payload(actualVxMps=88))
    observation = store.observation()
    assert observation['robot']['status'] == 'STALE'
    assert observation['robot']['data']['debug']['fields']['actualVxMps']['value'] is None
    assert observation['ros2']['topics']['/chassis_host/telemetry']['status'] == 'AVAILABLE'
    now[0] = 6
    assert store.observation()['ros2']['topics']['/chassis_host/telemetry']['status'] == 'STALE'


def test_default_ros_keeps_original_data_and_truth_semantics():
    store = state()
    store.telemetry.receive(ros_payload(actualVxMps=0, targetVxMps=1, busVoltageV=0,
                                       missing=None, nonfinite=float('nan')))
    observation = store.observation()
    assert observation['robot']['source'] == 'ROS2 /chassis_host/telemetry'
    assert observation['robot']['data']['actualVxMps'] == 0
    assert observation['robot']['data']['busVoltageV'] == 0
    assert observation['robot']['data']['targetVxMps'] == 1
    assert observation['robot']['data']['nonfinite'] is None
    assert observation['errors']['telemetryNonFiniteCount'] == 1
    assert observation['errors']['localChassisParseCount'] is None
    assert 'localChassis' not in observation['devices']


@pytest.mark.parametrize('audit', [True, False])
def test_main_starts_receiver_only_outside_audit_and_stops_on_exit(monkeypatch, capsys, tmp_path, audit):
    from collector import main, faults
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setattr(faults, 'default_spool_path', lambda source: tmp_path / 'test-only-faults.sqlite3')
    events = []
    config = Config('test-only-robot', 'test-only-source', chassis_source='local_ws')
    receiver = NS(start=lambda: events.append('receiver-start'), stop=lambda: events.append('receiver-stop'))
    uploader = NS(start=lambda: events.append('uploader-start'), stop=lambda: events.append('uploader-stop'),
                  enable_events=lambda acknowledge, monitor: events.append('faults-enable'),
                  enable_test_metrics=lambda *args: events.append('metrics-enable'))
    fake_state = NS(local_chassis=receiver)
    node = NS(audit=lambda: {'mode': 'READ_ONLY_AUDIT'}, destroy_node=lambda: events.append('destroy'))
    ros = ModuleType('rclpy')
    ros.init = lambda **kwargs: events.append('init')
    ros.ok = lambda: False
    ros.spin = lambda node: events.append('spin')
    ros.spin_once = lambda *args, **kwargs: pytest.fail('No live ROS audit in isolated test')
    executors = ModuleType('rclpy.executors')
    executors.ExternalShutdownException = type('ExternalShutdownException', (Exception,), {})
    monkeypatch.setitem(sys.modules, 'rclpy', ros)
    monkeypatch.setitem(sys.modules, 'rclpy.executors', executors)
    monkeypatch.setattr(main.Config, 'from_env', lambda **kwargs: config)
    monkeypatch.setattr(main, 'CollectorState', lambda config: fake_state)
    monkeypatch.setattr(main, 'Uploader', lambda config: uploader)
    monkeypatch.setattr(main, 'build_node', lambda state, uploader: node)
    main.main(['--audit-seconds', '0.1'] if audit else [])
    assert ('receiver-start' in events) is not audit
    assert ('uploader-start' in events) is not audit
    assert ('faults-enable' in events) is not audit
    assert (tmp_path / 'test-only-faults.sqlite3').exists() is not audit
    assert events.count('receiver-stop') == 1
    assert 'destroy' in events
    if audit:
        assert json.loads(capsys.readouterr().out)['mode'] == 'READ_ONLY_AUDIT'
