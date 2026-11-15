"""Raw archival adapter tests using fake nodes and sinks; no ROS or devices."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from collector.archive_ros import ArchiveRosAdapter, RAW_TOPIC_SPECS, build_archive_node, sensor_specs


class FakeNode:
    def __init__(self):
        self.created, self.destroyed, self.timers = [], [], []

    def create_subscription(self, kind, topic, callback, qos, **options):
        assert options['raw'] is True
        value = SimpleNamespace(topic=topic, callback=callback, qos=qos, options=options,
                                thread=threading.get_ident())
        self.created.append(value)
        return value

    def destroy_subscription(self, subscription):
        self.destroyed.append((subscription, threading.get_ident()))

    def create_timer(self, interval, callback):
        self.timers.append((interval, callback))


class Store:
    def __init__(self):
        self.rows = []
        self.ticks = 0

    def record(self, *args, **options):
        self.rows.append((args, options, threading.get_ident()))
        return True

    def tick(self):
        self.ticks += 1


class BlockingStore(Store):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def record(self, *args, **options):
        if not self.rows:
            self.entered.set()
            assert self.release.wait(3), 'test must release the fake disk write'
        return super().record(*args, **options)


def wait_for(condition):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.005)
    assert condition()


def bound(store, gate, **options):
    adapter = ArchiveRosAdapter(store, gate, sensors=('雷达MID360',), **options)
    node = FakeNode()
    adapter.bind(node, lambda name: name, lambda spec: {'latched': spec.latched})
    return adapter, node


def test_gate_creates_no_subscriptions_or_files_until_enabled_and_no_message_stays_waiting():
    gate = [False]
    store = Store()
    adapter, node = bound(store, lambda: gate[0])
    try:
        adapter.reconcile_gate()
        assert not node.created and adapter.worker is None and not store.rows
        gate[0] = True
        adapter.reconcile_gate()
        assert len(node.created) == 2
        state = adapter.snapshot()
        assert state['state'] == 'WAITING_UPSTREAM'
        assert all(row['state'] == 'WAITING_UPSTREAM' for row in state['topics'].values())
        assert not store.rows
    finally:
        assert adapter.close()
    assert all(thread == threading.get_ident() for _, thread in node.destroyed)


def test_raw_bytes_are_unchanged_and_only_the_worker_writes_with_receive_timestamp():
    store = Store()
    adapter, node = bound(store, lambda: True, wall_clock=lambda: 1793070000.25)
    try:
        adapter.reconcile_gate()
        raw = bytearray(b'\x00\x01raw-CDR\xff')
        node.created[0].callback(raw)
        raw[-1] = 0
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['written'] == 1)
        args, options, thread = store.rows[0]
        assert args == ('雷达MID360', '/livox/lidar', 'livox_ros_driver2/msg/CustomMsg',
                        b'\x00\x01raw-CDR\xff', 1793070000250000000)
        assert options['wall_time'] == 1793070000.25
        assert thread != threading.get_ident()
        assert store.ticks >= 1
    finally:
        assert adapter.close()


def test_ten_minutes_of_wan_outage_does_not_gate_ros_capture(monkeypatch):
    import socket
    def no_wan(*args, **kwargs):
        raise OSError('simulated WAN offline for ten minutes')
    monkeypatch.setattr(socket, 'create_connection', no_wan)
    clock = [1793070000.0]
    store = Store()
    adapter, node = bound(store, lambda: True, wall_clock=lambda: clock[0])
    try:
        adapter.reconcile_gate()
        callback = node.created[0].callback
        # Advance wall time rather than spending ten real minutes in a test.
        for second in range(0, 601, 10):
            clock[0] = 1793070000.0 + second
            callback(f'lidar-{second}'.encode())
            wait_for(lambda: len(store.rows) == second // 10 + 1)
            adapter.reconcile_gate()
        assert adapter.snapshot()['active'] is True
        assert adapter.snapshot()['topics']['/livox/lidar']['dropped'] == 0
        assert store.rows[-1][0][4] - store.rows[0][0][4] == 600 * 10**9
    finally:
        assert adapter.close()


def test_count_capacity_and_gate_epoch_drain_accepted_packets_and_reject_late_callbacks():
    gate = [True]
    store = BlockingStore()
    adapter, node = bound(store, lambda: gate[0], queue_capacity=2)
    try:
        adapter.reconcile_gate()
        old = node.created[0].callback
        old(b'first')
        assert store.entered.wait(1)
        old(b'queued-old-1'); old(b'queued-old-2'); old(b'overflow')
        row = adapter.snapshot()['topics']['/livox/lidar']
        assert row['received'] == 4 and row['reasons']['queue_capacity'] == 1
        assert adapter.snapshot()['queueMessages'] == 2
        gate[0] = False
        adapter.reconcile_gate()
        assert len(node.destroyed) == 2 and adapter.snapshot()['queueMessages'] == 2
        store.release.set()
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['written'] == 3)
        gate[0] = True
        adapter.reconcile_gate()
        assert adapter.snapshot()['topics']['/livox/lidar']['state'] == 'WAITING_UPSTREAM'
        old(b'late-old-callback')
        node.created[2].callback(b'new')
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['written'] == 4)
        assert [row[0][3] for row in store.rows] == [b'first', b'queued-old-1', b'queued-old-2', b'new']
        row = adapter.snapshot()['topics']['/livox/lidar']
        assert row['reasons']['inactive_epoch'] == 1
        assert row['dropped'] == 2
    finally:
        store.release.set()
        assert adapter.close()


def test_byte_capacity_and_invalid_payloads_are_counted_without_blocking_callback():
    store = BlockingStore()
    adapter, node = bound(store, lambda: True, queue_bytes=4, queue_capacity=10)
    try:
        adapter.reconcile_gate()
        callback = node.created[0].callback
        callback(b'123')
        assert store.entered.wait(1)
        callback(b'456'); callback(b'789'); callback(b''); callback(object())
        state = adapter.snapshot()
        assert state['queueBytes'] == 3
        assert state['topics']['/livox/lidar']['reasons'] == {
            'queue_bytes': 1, 'empty_message': 1, 'invalid_message': 1}
    finally:
        store.release.set()
        assert adapter.close()


def test_one_sensor_writer_failure_and_rejection_do_not_disable_other_sources():
    class FailingStore(Store):
        def record(self, *args, **options):
            if args[1] == '/livox/lidar':
                raise OSError('fake disk failure')
            return super().record(*args, **options)
    store = FailingStore()
    adapter, node = bound(store, lambda: True, writer_retry_s=10)
    try:
        adapter.reconcile_gate()
        node.created[0].callback(b'lidar')
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['dropped'] == 1)
        node.created[0].callback(b'backoff')
        node.created[1].callback(b'imu')
        wait_for(lambda: adapter.snapshot()['topics']['/livox/imu']['written'] == 1)
        row = adapter.snapshot()['topics']['/livox/lidar']
        assert row['reasons'] == {'writer_error': 1, 'writer_backoff': 1}
        assert row['state'] == 'ERROR'
    finally:
        assert adapter.close()
    class RejectingStore(Store):
        def record(self, *args, **options):
            return False
    adapter, node = bound(RejectingStore(), lambda: True)
    try:
        adapter.reconcile_gate()
        node.created[0].callback(b'not accepted')
        wait_for(lambda: adapter.snapshot()['topics']['/livox/lidar']['dropped'] == 1)
        row = adapter.snapshot()['topics']['/livox/lidar']
        assert row['written'] == 0 and row['reasons'] == {'store_rejected': 1}
    finally:
        assert adapter.close()


def test_gate_exception_fails_closed_and_lost_dds_messages_are_explicit():
    gate = [True]
    def enabled():
        if isinstance(gate[0], Exception):
            raise gate[0]
        return gate[0]
    store = Store()
    adapter, node = bound(store, enabled)
    adapter.events_factory = lambda callback: SimpleNamespace(message_lost=callback)
    try:
        adapter.reconcile_gate()
        node.created[0].options['event_callbacks'].message_lost(SimpleNamespace(total_count_change=3))
        state = adapter.snapshot()['topics']['/livox/lidar']
        assert state['dds_lost'] == 3 and state['dds_loss_events']
        gate[0] = OSError('fake unreadable site declaration')
        adapter.reconcile_gate()
        assert not adapter.snapshot()['active']
        assert adapter.snapshot()['gateError'] == 'OSError'
        assert len(node.destroyed) == 2
    finally:
        assert adapter.close()


def test_missing_message_type_isolated_and_optional_dds_events_can_fall_back():
    class BasicNode(FakeNode):
        def create_subscription(self, *args, **options):
            if 'event_callbacks' in options:
                raise NotImplementedError('RMW cannot report loss')
            return super().create_subscription(*args, **options)
    def load(name):
        if name.startswith('livox_'):
            raise ImportError('optional sensor type absent')
        return name
    adapter = ArchiveRosAdapter(Store(), lambda: True, sensors=('雷达MID360',))
    node = BasicNode()
    adapter.bind(node, load, lambda spec: spec.latched, lambda callback: callback)
    try:
        adapter.reconcile_gate()
        assert [item.topic for item in node.created] == ['/livox/imu']
        state = adapter.snapshot()['topics']
        assert state['/livox/lidar']['state'] == 'ERROR'
        assert not state['/livox/imu']['dds_loss_events']
        node.created[0].callback(b'imu remains usable')
        wait_for(lambda: adapter.snapshot()['topics']['/livox/imu']['written'] == 1)
    finally:
        assert adapter.close()


def test_factory_uses_raw_sensor_qos_and_latched_metadata_without_real_ros(monkeypatch):
    import sys
    node, constructor = FakeNode(), []
    def create_node(*args, **options):
        constructor.append((args, options))
        return node
    monkeypatch.setitem(sys.modules, 'rclpy.node', SimpleNamespace(Node=create_node))
    monkeypatch.setitem(sys.modules, 'rclpy.qos', SimpleNamespace(
        QoSProfile=lambda **values: values, ReliabilityPolicy=SimpleNamespace(RELIABLE='R', BEST_EFFORT='B'),
        DurabilityPolicy=SimpleNamespace(TRANSIENT_LOCAL='T', VOLATILE='V'),
        HistoryPolicy=SimpleNamespace(KEEP_LAST='K')))
    monkeypatch.setitem(sys.modules, 'rclpy.event_handler', SimpleNamespace(
        SubscriptionEventCallbacks=lambda **values: values))
    monkeypatch.setitem(sys.modules, 'rosidl_runtime_py.utilities', SimpleNamespace(get_message=lambda name: name))
    gate = [False]
    actual, adapter = build_archive_node(Store(), lambda: gate[0], sensors=('定位与TF',))
    try:
        assert actual is node and not node.created
        assert constructor[0][1] == {'enable_rosout': False, 'start_parameter_services': False}
        gate[0] = True
        node.timers[0][1]()
        by_topic = {item.topic: item for item in node.created}
        assert by_topic['/tf_static'].qos['durability'] == 'T'
        assert by_topic['/map'].qos['reliability'] == 'R'
        assert by_topic['/odom_nav'].qos['durability'] == 'V'
        assert by_topic['/odom_nav'].qos['reliability'] == 'B'
    finally:
        assert adapter.close()


def test_fixed_allowlist_has_no_control_or_web_preview_sources():
    assert set(sensor_specs()) == {'雷达MID360', '二维雷达scan', '二维雷达蓝海', 'Berxel彩色', 'Berxel深度',
                                   'Berxel点云', '鱼眼相机', '定位与TF', '底盘遥测', '诊断日志', '覆盖任务'}
    assert all(not any(marker in item.topic for marker in ('/cmd_', 'compressed', 'observer_camera'))
               for item in RAW_TOPIC_SPECS)
    with pytest.raises(ValueError):
        ArchiveRosAdapter(Store(), lambda: True, sensors=('arbitrary-untrusted-topic',))


def test_depth_images_and_original_pointcloud_use_separate_sensor_segments():
    groups = sensor_specs()
    assert {row.topic for row in groups['Berxel深度']} == {
        '/berxel/depth/image_raw', '/berxel/depth/camera_info'}
    assert [(row.topic,row.type_name) for row in groups['Berxel点云']] == [
        ('/berxel/points','sensor_msgs/msg/PointCloud2')]
    adapter = ArchiveRosAdapter(Store(),lambda: True,sensors=('Berxel深度','Berxel点云'))
    assert len(adapter.specs) == 3


def test_gate_closure_retries_failed_subscription_destruction_before_reopening():
    class RetryNode(FakeNode):
        failures = True
        def destroy_subscription(self, subscription):
            if self.failures:
                raise RuntimeError('fake cleanup interruption')
            super().destroy_subscription(subscription)
    gate, node = [True], RetryNode()
    adapter = ArchiveRosAdapter(Store(), lambda: gate[0], sensors=('雷达MID360',))
    adapter.bind(node, lambda name: name, lambda spec: spec.latched)
    try:
        adapter.reconcile_gate()
        gate[0] = False
        adapter.reconcile_gate()
        state = adapter.snapshot()
        assert not state['active'] and state['subscriptionCleanupPending'] == 2
        assert state['state'] == 'ERROR'
        gate[0] = True
        adapter.reconcile_gate()
        assert len(node.created) == 2, 'no duplicate subscriptions while cleanup is incomplete'
        node.failures = False
        adapter.reconcile_gate()
        assert len(node.destroyed) == 2 and len(node.created) == 4
    finally:
        assert adapter.close()


def test_immediate_close_drains_inflight_and_queued_bytes_and_rejects_late_callbacks():
    store = BlockingStore()
    adapter, node = bound(store, lambda: True)
    adapter.reconcile_gate()
    callback = node.created[0].callback
    callback(b'first-in-flight')
    assert store.entered.wait(1)
    callback(b'queued-second')
    callback(b'queued-third')

    def release_after_close_stops_admission():
        try:
            wait_for(lambda: adapter.stopping)
            callback(b'late-callback')
            snapshot = adapter.snapshot()
            assert snapshot['queueMessages'] == 2
            assert snapshot['inFlightMessages'] == 1
            assert snapshot['topics']['/livox/lidar']['reasons'] == {'inactive_epoch': 1}
        finally:
            store.release.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        observer = pool.submit(release_after_close_stops_admission)
        assert adapter.close(timeout=2)
        observer.result()
    assert [row[0][3] for row in store.rows] == [b'first-in-flight', b'queued-second', b'queued-third']
    state = adapter.snapshot()
    assert state['queueMessages'] == state['queueBytes'] == state['inFlightMessages'] == 0
    assert not state['workerAlive'] and not state['active']
    assert state['topics']['/livox/lidar']['written'] == 3
    assert all(thread == threading.get_ident() for _, thread in node.destroyed)
    assert adapter.close(), 'repeated close must not discard or rewrite anything'
    assert len(store.rows) == 3


def test_close_timeout_retains_queued_data_and_can_finish_on_retry():
    store = BlockingStore()
    adapter, node = bound(store, lambda: True)
    adapter.reconcile_gate()
    callback = node.created[0].callback
    callback(b'in-flight')
    assert store.entered.wait(1)
    callback(b'queued')
    try:
        assert not adapter.close(timeout=.01)
        state = adapter.snapshot()
        assert state['queueMessages'] == state['inFlightMessages'] == 1
        assert state['tickError'] == 'WriterShutdownTimeout'
        callback(b'late')
        # A late enabled-gate timer must not turn graceful close into discard.
        adapter.reconcile_gate()
        assert adapter.snapshot()['queueMessages'] == 1
    finally:
        store.release.set()
        assert adapter.close(timeout=2)
    assert [row[0][3] for row in store.rows] == [b'in-flight', b'queued']
    assert adapter.snapshot()['tickError'] is None


@pytest.mark.parametrize('gate_failure', [False, OSError('gate unavailable')])
def test_gate_closure_during_shutdown_drains_previously_accepted_data(gate_failure):
    gate = [True]
    def enabled():
        if isinstance(gate[0], Exception):
            raise gate[0]
        return gate[0]
    store = BlockingStore()
    adapter, node = bound(store, enabled)
    adapter.reconcile_gate()
    callback = node.created[0].callback
    callback(b'already-writing')
    assert store.entered.wait(1)
    callback(b'queued-second')
    callback(b'queued-third')

    def close_gate_then_release():
        try:
            wait_for(lambda: adapter.stopping)
            gate[0] = gate_failure
        finally:
            store.release.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        observer = pool.submit(close_gate_then_release)
        assert adapter.close(timeout=2)
        observer.result()
    assert [row[0][3] for row in store.rows] == [b'already-writing', b'queued-second', b'queued-third']
    state = adapter.snapshot()
    assert state['queueMessages'] == state['queueBytes'] == state['inFlightMessages'] == 0
    assert state['topics']['/livox/lidar']['reasons'] == {}


def test_close_before_start_is_repeatable_and_cannot_restart():
    adapter, node = bound(Store(), lambda: False)
    adapter.reconcile_gate()
    assert adapter.close() and adapter.close()
    assert adapter.snapshot()['inFlightMessages'] == 0
    assert not node.created and adapter.worker is None
    with pytest.raises(RuntimeError, match='closed'):
        adapter.start()


def test_close_reports_subscription_cleanup_failure_and_retries_without_losing_queue():
    class RetryNode(FakeNode):
        failures = True
        def destroy_subscription(self, subscription):
            if self.failures:
                raise RuntimeError('fake cleanup interruption')
            super().destroy_subscription(subscription)

    store, node = Store(), RetryNode()
    adapter = ArchiveRosAdapter(store, lambda: True, sensors=('雷达MID360',))
    adapter.bind(node, lambda name: name, lambda spec: spec.latched)
    adapter.reconcile_gate()
    node.created[0].callback(b'accepted-before-close')
    assert not adapter.close()
    state = adapter.snapshot()
    assert not state['workerAlive'] and state['subscriptionCleanupPending'] == 2
    assert [row[0][3] for row in store.rows] == [b'accepted-before-close']
    node.created[0].callback(b'rejected-after-close')
    node.failures = False
    assert adapter.close()
    assert adapter.snapshot()['subscriptionCleanupPending'] == 0
    assert len(store.rows) == 1


@pytest.mark.parametrize('armed', [False, True])
def test_explicit_empty_selection_never_subscribes_or_starts_writer(armed):
    sink, node = Store(), FakeNode()
    adapter = ArchiveRosAdapter(sink, lambda: armed, sensors=())
    adapter.bind(node, lambda name: name, lambda spec: {})
    try:
        for _ in range(3):adapter.reconcile_gate()
        assert node.created == [] and adapter.worker is None and sink.rows == []
        status = adapter.snapshot()
        assert status['topics'] == {} and status['state'] == 'DISABLED'
        assert not status['active'] and status['queueBytes'] == 0
    finally:assert adapter.close()


def test_camera_only_selection_writes_images_without_pointcloud_subscriptions():
    from collector.archive_policy import parse_ros_sensors
    sink, node = Store(), FakeNode()
    selected = parse_ros_sensors('Berxel彩色,Berxel深度,Berxel点云,雷达MID360', True)
    adapter = ArchiveRosAdapter(sink, lambda: True, sensors=selected)
    adapter.bind(node, lambda name: name, lambda spec: {})
    try:
        adapter.reconcile_gate()
        assert {row.topic for row in node.created} == {
            '/berxel/color/image_raw', '/berxel/color/camera_info',
            '/berxel/depth/image_raw', '/berxel/depth/camera_info'}
        image = next(row for row in node.created if row.topic == '/berxel/color/image_raw')
        image.callback(b'camera-only-fixture-cdr')
        wait_for(lambda: len(sink.rows) == 1)
        assert sink.rows[0][0][0] == 'Berxel彩色'
        assert sink.rows[0][0][1] == '/berxel/color/image_raw'
        assert all('/points' not in row.topic and '/livox/' not in row.topic for row in node.created)
    finally:assert adapter.close()
