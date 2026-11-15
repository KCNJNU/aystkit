"""Only synthetic marker summaries: ROS input bounds, age and pose independence."""
import json
import math
import time
from types import SimpleNamespace as NS

import pytest

from collector.aruco import (ARUCO_DICTIONARY, ARUCO_KEYS, ARUCO_MAX_BYTES, ARUCO_TOPIC,
                             ArucoPreview, parse_aruco_summary)
from collector.config import Config
from collector.legacy.devices import DeviceInterfaceStore, VISION_TOPICS
from collector.main import build_node
from collector.state import CollectorState
from tests.test_berxel_transport import fake_ros


def summary(timestamp=1000, age=125):
    return {'version': 1, 'timestamp': timestamp, 'cameras': {
        key: {'status': 'AVAILABLE', 'ageMs': age, 'dictionary': ARUCO_DICTIONARY,
              'ids': [10, 11]} for key in ARUCO_KEYS}}


def encoded(**kwargs):
    return json.dumps(summary(**kwargs))


def test_fixed_public_fields_and_network_then_receiver_age_are_cumulative():
    now = [10.0]
    preview = ArucoPreview(lambda: now[0], lambda: 1000.25)
    value = summary()
    value.update(private='SECRET')
    value['cameras']['http://SECRET'] = {'ids': [15]}
    for row in value['cameras'].values():
        row.update(private='SECRET')
    assert preview.receive(json.dumps(value))
    first = preview.snapshot()
    assert set(first['cameras']) == set(ARUCO_KEYS)
    assert first['ageMs'] == 250
    assert first['cameras']['usbFront']['ageMs'] == 375
    now[0] = 11.5
    second = preview.snapshot()
    assert second['ageMs'] == 1750
    assert second['cameras']['usbFront']['ageMs'] == 1875
    assert first['cameras']['usbFront']['ageMs'] == 375
    assert 'SECRET' not in json.dumps(second)
    assert 'timestamp' not in second
    second['cameras']['usbFront']['ids'].clear()
    assert preview.snapshot()['cameras']['usbFront']['ids'] == [10, 11]


def test_no_marker_is_available_and_ids_are_bounded_deduplicated_sorted():
    value = summary()
    value['cameras']['usbFront']['ids'] = []
    value['cameras']['color']['ids'] = [99, 0, 11, 10, 11]
    _, output = parse_aruco_summary(json.dumps(value), 1000)
    assert output['cameras']['usbFront'] == {
        'status': 'AVAILABLE', 'ageMs': 125, 'dictionary': ARUCO_DICTIONARY, 'ids': []}
    assert output['cameras']['color']['ids'] == [0, 10, 11, 99]


def test_unavailable_error_and_expired_source_cannot_retain_ids():
    value = summary(age=4900)
    value['cameras']['usbFront'].update(status='WAITING_UPSTREAM', ageMs=None)
    value['cameras']['usbLeft'].update(status='ERROR', ageMs=None)
    _, output = parse_aruco_summary(json.dumps(value), 1000.2)
    assert output['status'] == 'AVAILABLE'
    assert output['cameras']['usbLeft']['status'] == 'ERROR'
    assert all(row['ids'] == [] and row['ageMs'] is None for row in output['cameras'].values())


def test_each_source_and_whole_summary_expire_without_any_new_message():
    clock = [10.0]
    preview = ArucoPreview(lambda: clock[0], lambda: 1000)
    assert preview.receive(encoded(age=4500))
    clock[0] = 10.5
    result = preview.snapshot()
    assert result['status'] == 'AVAILABLE'
    assert all(row['status'] == 'WAITING_UPSTREAM' and not row['ids'] for row in result['cameras'].values())
    clock[0] = 15
    assert preview.snapshot()['status'] == 'WAITING_UPSTREAM'


@pytest.mark.parametrize('field,bad', [
    ('dictionary', 'DICT_4X4_100'), ('dictionary', None), ('dictionary', 'SECRET'),
    ('status', 'DETECTED'), ('status', None), ('ageMs', None), ('ageMs', -1),
    ('ageMs', True), ('ageMs', math.nan), ('ageMs', math.inf), ('ageMs', 86_400_001),
    ('ids', None), ('ids', {}), ('ids', [True]), ('ids', [10.0]), ('ids', ['10']),
    ('ids', [-1]), ('ids', [100]), ('ids', [0] * 13),
])
def test_invalid_rows_clear_previous_ids_without_renewing_reception(field, bad):
    clock = [10.0]
    preview = ArucoPreview(lambda: clock[0], lambda: 1000.5)
    assert preview.receive(encoded())
    value = summary(timestamp=1000.5)
    value['cameras']['usbFront'][field] = bad
    clock[0] = 10.25
    assert not preview.receive(json.dumps(value))
    assert preview.received == 10.0 and preview.timestamp == 1000
    result = preview.snapshot()
    assert result['status'] == 'WAITING_UPSTREAM'
    assert all(not row['ids'] for row in result['cameras'].values())


@pytest.mark.parametrize('raw', [
    None, b'{}', '', '[]', '{}', '{"version":1,"version":1}',
    '{"version":1,"extra":NaN}', '{"version":1,"extra":1e999}',
    '{"extra":' + '1' * 21 + '}', '{"extra":1.' + '1' * 65 + '}',
    '[' * 1000 + ']' * 1000, ' ' * (ARUCO_MAX_BYTES + 1),
    json.dumps(dict(summary(), token='汉' * 3000), ensure_ascii=False),
    json.dumps(dict(summary(), version=True)), json.dumps(dict(summary(), version=2)),
    json.dumps(dict(summary(), cameras={})), json.dumps(dict(summary(), cameras=[])),
    json.dumps(dict(summary(), timestamp=True)), json.dumps(dict(summary(), timestamp=0)),
    json.dumps(dict(summary(), timestamp=-1)), json.dumps(dict(summary(), timestamp=1000.251)),
    json.dumps(dict(summary(), timestamp=995)), json.dumps(dict(summary(), timestamp=math.nan)),
])
def test_bad_oversized_future_expired_or_ambiguous_messages_cannot_claim_live_data(raw):
    preview = ArucoPreview(lambda: 10, lambda: 1000)
    assert not preview.receive(raw)
    assert preview.received is None
    assert preview.snapshot()['status'] == 'WAITING_UPSTREAM'


def test_replayed_or_reordered_timestamps_do_not_renew_ids_and_valid_new_message_recovers():
    wall = [1000.0]
    clock = [10.0]
    preview = ArucoPreview(lambda: clock[0], lambda: wall[0])
    assert preview.receive(encoded())
    clock[0], wall[0] = 11, 1001
    assert not preview.receive(encoded())
    assert preview.received == 10
    assert not preview.receive(encoded(timestamp=999.5))
    assert preview.snapshot()['cameras']['color']['ids'] == []
    assert preview.receive(encoded(timestamp=1001))
    assert preview.snapshot()['cameras']['color']['ids'] == [10, 11]
    assert preview.received == 11


def test_small_future_skew_uses_zero_transport_age_without_ignoring_source_age():
    _, value = parse_aruco_summary(encoded(timestamp=1000.25), 1000)
    assert value['ageMs'] == 0
    assert value['cameras']['color']['ageMs'] == 125


def test_new_marker_success_does_not_assert_old_detection_pose_or_docking_state():
    store = DeviceInterfaceStore(lambda: 10, lambda: 1000)
    store.receive_aruco_preview(encoded())
    value = store.snapshot()['visionDocking']
    assert value['arucoPreview']['status'] == 'AVAILABLE'
    assert value['arucoPreview']['cameras']['usbFront']['ids'] == [10, 11]
    assert value['status'] == value['targetStatus'] == 'WAITING_UPSTREAM'
    assert value['detectionState'] is None and value['detectedIds'] is None
    assert value['boardPose'] is None and value['dockingState'] is None
    assert all(value['topics'][key]['messageCount'] == 0 for key in (
        'detectionStatus', 'detectedIds', 'boardPose', 'dockingState'))
    assert store.topic_snapshots()[ARUCO_TOPIC]['topicType'] == 'std_msgs/msg/String'


def test_old_pose_success_and_preview_failure_are_independent_and_bad_input_does_not_renew_topic():
    clock = [10.0]
    store = DeviceInterfaceStore(lambda: clock[0], lambda: 1000)
    store.receive_detection_status('ok_single')
    store.receive_detected_ids([10])
    store.receive_board_pose({'position': {'xM': 1.2}, 'frameId': 'synthetic_camera_frame'})
    store.receive_docking_state('DISABLED')
    store.receive_aruco_preview(encoded())
    before = store.snapshot()['visionDocking']
    clock[0] = 10.5
    store.receive_aruco_preview('{invalid')
    after = store.snapshot()['visionDocking']
    for key in ('status', 'targetStatus', 'detectionState', 'detectedIds', 'boardPose', 'dockingState'):
        assert after[key] == before[key]
    assert after['targetStatus'] == 'AVAILABLE'
    assert after['arucoPreview']['status'] == 'WAITING_UPSTREAM'
    assert after['topics']['arucoPreview']['messageCount'] == 1
    assert after['topics']['arucoPreview']['ageS'] == .5
    assert '{invalid' not in json.dumps(after)


def test_topic_stale_or_contract_conflict_clears_ids_even_when_summary_source_was_fresh():
    clock = [10.0]
    store = DeviceInterfaceStore(lambda: clock[0], lambda: 1000)
    store.receive_aruco_preview(encoded())
    clock[0] = 12.1
    result = store.snapshot()['visionDocking']
    assert result['arucoPreview']['status'] == 'STALE'
    assert all(not row['ids'] for row in result['arucoPreview']['cameras'].values())
    clock[0] = 10.5
    store.set_graph('arucoPreview', 1, ['std_msgs/msg/Int32MultiArray'])
    result = store.snapshot()['visionDocking']
    assert result['arucoPreview']['status'] == 'ERROR'
    assert all(not row['ids'] for row in result['topics']['arucoPreview']['value']['cameras'].values())


def test_real_node_constructor_adds_only_reliable_read_only_summary_subscription(fake_ros):
    state = CollectorState(Config('isolated-robot', 'isolated-primary'), collector=NS(collect=lambda: {}))
    node = build_node(state)
    subscription = node.subscriptions[ARUCO_TOPIC]
    assert subscription.kind == 'std_msgs/msg/String'
    assert subscription.qos.reliability == 'reliable'
    subscription.callback(NS(data=encoded(timestamp=time.time())))
    result = state.observation()
    assert result['devices']['visionDocking']['arucoPreview']['cameras']['usbFront']['ids'] == [10, 11]
    assert result['ros2']['topics'][ARUCO_TOPIC]['messageCount'] == 1
    assert result['devices']['visionDocking']['boardPose'] is None
    assert VISION_TOPICS['boardPose']['topic'] == '/fisheye_aruco/board_pose'
    camera_state = CollectorState(Config('isolated-robot', 'isolated-camera', role='cameras'))
    assert ARUCO_TOPIC not in build_node(camera_state).subscriptions
