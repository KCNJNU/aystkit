"""Exercise actual subscription construction without ROS or camera hardware."""
import json
import os
import sys
import time
from types import ModuleType, SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from collector.config import Config
from collector.main import build_node
from collector.state import CollectorState


@pytest.fixture
def fake_ros(monkeypatch):
    class Node:
        def __init__(self, *args, **kwargs):
            self.subscriptions = {}

        def create_subscription(self, kind, topic, callback, qos):
            self.subscriptions[topic] = NS(kind=kind, callback=callback, qos=qos)
            return self.subscriptions[topic]

        def create_timer(self, *args):
            pass

        def get_publishers_info_by_topic(self, topic):
            return []

    modules = {
        'rclpy': {},
        'rclpy.node': {'Node': Node},
        'rclpy.qos': {
            'QoSProfile': lambda **values: NS(**values),
            'DurabilityPolicy': NS(VOLATILE='volatile', TRANSIENT_LOCAL='transient_local'),
            'HistoryPolicy': NS(KEEP_LAST='keep_last'),
            'ReliabilityPolicy': NS(RELIABLE='reliable', BEST_EFFORT='best_effort'),
        },
        'rosidl_runtime_py': {},
        'rosidl_runtime_py.utilities': {'get_message': lambda kind: kind},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)


def state_for(**kwargs):
    return CollectorState(Config('isolated-robot', 'isolated-source', **kwargs),
                          collector=NS(collect=lambda: {}))


def test_berxel_transport_defaults_and_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('COLLECTOR_'):
            monkeypatch.delenv(name)
    assert Config('isolated-robot', 'isolated-source').berxel_transport == 'raw'
    assert Config.from_env(audit=True).berxel_transport == 'raw'
    monkeypatch.setenv('COLLECTOR_BERXEL_TRANSPORT', 'bridge')
    assert Config.from_env(audit=True).berxel_transport == 'bridge'
    monkeypatch.setenv('COLLECTOR_BERXEL_TRANSPORT', 'disabled')
    with pytest.raises(ValueError, match='COLLECTOR_BERXEL_TRANSPORT'):
        Config.from_env(audit=True)


@pytest.mark.parametrize('value', ['', 'BRIDGE', 'off', None, True])
def test_invalid_berxel_transport_is_rejected(value):
    with pytest.raises(ValueError, match='COLLECTOR_BERXEL_TRANSPORT'):
        Config('isolated-robot', 'isolated-source', berxel_transport=value)


@pytest.mark.parametrize('role', ['primary', 'cameras'])
def test_bridge_skips_only_berxel_raw_subscriptions(fake_ros, monkeypatch, role):
    # Topic overrides must not accidentally bypass the explicit transport choice.
    monkeypatch.setenv('COLLECTOR_TOPIC_COLOR', '/isolated/berxel/color')
    raw_state = state_for(role=role)
    bridge_state = state_for(role=role, berxel_transport='bridge')
    raw = build_node(raw_state).subscriptions
    bridge = build_node(bridge_state).subscriptions
    raw_berxel = {raw_state.specs[key][1] for key in ('color', 'depth', 'depthPoints')}
    assert raw_berxel <= raw.keys()
    assert raw_berxel.isdisjoint(bridge)
    assert set(bridge) == set(raw) - raw_berxel
    assert '/fisheye_front/image_raw' in bridge
    if role == 'primary':
        assert {'/livox/points', '/livox/imu', '/chassis_host/telemetry', '/map'} <= bridge.keys()
    for key in ('color', 'depth', 'depthPoints'):
        topic, kind = bridge_state.bridge_specs[key]
        assert bridge[topic].kind == kind
        assert bridge[topic].qos.depth == 1
        assert bridge[topic].qos.reliability == 'best_effort'
    # Diagnostics preserve real upstream topics; no fictitious aliases are used.
    assert bridge_state.specs == raw_state.specs


def test_bridge_callbacks_supply_live_media_and_sensor_health(fake_ros):
    state = state_for(berxel_transport='bridge')
    node = build_node(state)
    now = time.time()
    header = NS(frame_id='isolated_depth_frame',
                stamp=NS(sec=int(now), nanosec=int(now % 1 * 1e9)))
    ok, encoded = cv2.imencode('.jpg', np.zeros((4, 6, 3), dtype=np.uint8))
    assert ok
    for key in ('color', 'depth'):
        topic = state.bridge_specs[key][0]
        node.subscriptions[topic].callback(NS(header=header, format='jpeg', data=encoded.tobytes()))
    point_topic = state.bridge_specs['depthPoints'][0]
    node.subscriptions[point_topic].callback(NS(data=json.dumps({
        'points': [[1.0, 2.0, 3.0]], 'totalPoints': 1, 'frameId': 'isolated_depth_frame',
    })))
    for key in ('color', 'depth', 'depthPoints'):
        sensor = state.sensor_snapshot()[key]
        assert sensor['status'] == 'AVAILABLE'
        assert sensor['topic'] == state.bridge_specs[key][0]
        assert sensor['messageCount'] == 1
        assert state.samples[key].message_count == 0
        assert state.media.status(key)['status'] == 'AVAILABLE'
    assert state.media.status('color')['previewWidth'] == 6
    assert state.media.status('depth')['previewHeight'] == 4
    assert state.media.status('depthPoints')['points'] == [[1.0, 2.0, 3.0]]



def test_runtime_bridge_reports_only_local_frame_metadata(fake_ros, monkeypatch):
    from shared import preview_health
    writes = []
    class Writer:
        def write(self, channels):
            writes.append(channels)
    monkeypatch.setattr(preview_health, 'PreviewHealthWriter', Writer)
    uploader = NS(enqueue=lambda *args: None)
    state = state_for(berxel_transport='bridge')
    node = build_node(state, uploader)
    node.publish_latest()
    assert set(writes[0]) == {'color', 'depth'}
    assert all(row == {'frameSeq': 0, 'status': 'WAITING_UPSTREAM', 'ageS': None}
               for row in writes[0].values())
    now = time.time()
    header = NS(frame_id='synthetic', stamp=NS(sec=int(now), nanosec=0))
    ok, encoded = cv2.imencode('.jpg', np.zeros((4, 6, 3), dtype=np.uint8))
    assert ok
    for key in ('color', 'depth'):
        node.subscriptions[state.bridge_specs[key][0]].callback(
            NS(header=header, format='jpeg', data=encoded.tobytes()))
    node.publish_latest()
    assert all(row['frameSeq'] == 1 and row['status'] == 'AVAILABLE'
               for row in writes[-1].values())
    assert all(set(row) == {'frameSeq', 'status', 'ageS'} for row in writes[-1].values())
    assert build_node(state).preview_health is None  # Audit is read-only.
    assert build_node(state_for(berxel_transport='raw'), uploader).preview_health is None
    assert build_node(state_for(role='cameras', berxel_transport='bridge'), uploader).preview_health is None
