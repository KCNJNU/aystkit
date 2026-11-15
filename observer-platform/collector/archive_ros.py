"""Garage-gated serialized ROS archival; never open devices or publish controls.

ROS callbacks only copy bounded CDR messages into memory. One writer thread owns
disk calls. Counts describe messages observed here; upstream and DDS losses can
only be reported where the ROS implementation supplies message-lost events.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class TopicSpec:
    sensor: str
    topic: str
    type_name: str
    latched: bool = False


RAW_TOPIC_SPECS = (
    TopicSpec('雷达MID360', '/livox/lidar', 'livox_ros_driver2/msg/CustomMsg'),
    TopicSpec('雷达MID360', '/livox/imu', 'sensor_msgs/msg/Imu'),
    # /scan can belong to BlueSea or LDROBOT. Preserve its real frame_id in CDR;
    # the archive must not label every publisher as BlueSea without evidence.
    TopicSpec('二维雷达scan', '/scan', 'sensor_msgs/msg/LaserScan'),
    TopicSpec('二维雷达蓝海', '/bluesea_new/scan', 'sensor_msgs/msg/LaserScan'),
    TopicSpec('Berxel彩色', '/berxel/color/image_raw', 'sensor_msgs/msg/Image'),
    TopicSpec('Berxel彩色', '/berxel/color/camera_info', 'sensor_msgs/msg/CameraInfo'),
    TopicSpec('Berxel深度', '/berxel/depth/image_raw', 'sensor_msgs/msg/Image'),
    TopicSpec('Berxel深度', '/berxel/depth/camera_info', 'sensor_msgs/msg/CameraInfo'),
    TopicSpec('Berxel点云', '/berxel/points', 'sensor_msgs/msg/PointCloud2'),
    TopicSpec('鱼眼相机', '/fisheye_front/image_raw', 'sensor_msgs/msg/Image'),
    TopicSpec('鱼眼相机', '/fisheye_front/camera_info', 'sensor_msgs/msg/CameraInfo'),
    TopicSpec('定位与TF', '/Odometry', 'nav_msgs/msg/Odometry'),
    TopicSpec('定位与TF', '/odom_nav', 'nav_msgs/msg/Odometry'),
    TopicSpec('定位与TF', '/tf', 'tf2_msgs/msg/TFMessage'),
    TopicSpec('定位与TF', '/tf_static', 'tf2_msgs/msg/TFMessage', True),
    TopicSpec('定位与TF', '/map', 'nav_msgs/msg/OccupancyGrid', True),
    TopicSpec('定位与TF', '/localization/state', 'std_msgs/msg/String', True),
    TopicSpec('定位与TF', '/localization/ready', 'std_msgs/msg/Bool', True),
    TopicSpec('底盘遥测', '/chassis_host/telemetry', 'std_msgs/msg/String'),
    TopicSpec('诊断日志', '/diagnostics', 'diagnostic_msgs/msg/DiagnosticArray'),
    TopicSpec('诊断日志', '/rosout', 'rcl_interfaces/msg/Log'),
    TopicSpec('覆盖任务', '/coverage/events', 'std_msgs/msg/String', True),
)


def sensor_specs():
    """Return the fixed source definitions without importing ROS or touching disk."""
    result = {}
    for item in RAW_TOPIC_SPECS:
        result.setdefault(item.sensor, []).append(item)
    return result


def subscription_policy(spec, publishers=()):
    """Request compatible QoS, keeping large latched histories bounded."""
    if not spec.latched:
        return dict(reliability='BEST_EFFORT', durability='VOLATILE', depth=8)
    profiles = [getattr(item, 'qos_profile', item) for item in publishers]
    def name(value):
        return getattr(value, 'name', str(value)).upper()
    durable = all(name(getattr(item, 'durability', '')) in ('TRANSIENT_LOCAL','1') for item in profiles) if profiles else spec.topic in ('/tf_static','/map')
    reliable = all(name(getattr(item, 'reliability', '')) in ('RELIABLE','1') for item in profiles) if profiles else durable
    return dict(reliability='RELIABLE' if reliable else 'BEST_EFFORT',
        durability='TRANSIENT_LOCAL' if durable else 'VOLATILE', depth=100 if spec.topic == '/tf_static' else 1)


@dataclass(frozen=True)
class _Packet:
    spec: TopicSpec
    data: bytes
    timestamp_ns: int
    wall_time: float
    epoch: int


class ArchiveRosAdapter:
    def __init__(self, store, gate, *, sensors=None, queue_capacity=256,
                 queue_bytes=64 * 1024 * 1024, wall_clock=time.time,
                 monotonic=time.monotonic, writer_retry_s=1.0, image_fps=0):
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError('queue_capacity must be a positive integer')
        if type(queue_bytes) is not int or queue_bytes < 1:
            raise ValueError('queue_bytes must be a positive integer')
        if not callable(gate) or not math.isfinite(writer_retry_s) or writer_retry_s < 0:
            raise ValueError('invalid gate or writer retry interval')
        names = set(sensor_specs()) if sensors is None else set(sensors)
        if not names.issubset(sensor_specs()):
            raise ValueError('sensors must use the fixed raw source allowlist')
        self.specs = tuple(item for item in RAW_TOPIC_SPECS if item.sensor in names)
        self.store, self.gate = store, gate
        from .archive_sampling import FrameSampler
        self.image_sampler = FrameSampler(image_fps)
        self.queue_capacity, self.queue_bytes = queue_capacity, queue_bytes
        self.wall_clock, self.monotonic = wall_clock, monotonic
        self.writer_retry_s = writer_retry_s
        self.condition = threading.Condition(threading.RLock())
        self.queue = deque()
        self.pending_bytes = 0
        self.in_flight = 0
        self.epoch = 0
        self.active = False
        self.stopping = False
        self.worker = None
        self.subscriptions = {}
        self.retired_subscriptions = {}
        self.node = self.message_loader = self.qos_factory = self.events_factory = None
        self.errors = {}
        self.retry_at = {}
        self.subscription_retry_at = {}
        self.subscription_failures = Counter()
        self.subscription_qos = {}
        self.qos_checked_at = {}
        self.gate_error = None
        self.tick_error = None
        self.stats = {item.topic: dict(received=0, enqueued=0, written=0,
            dropped=0, sampled=0, dds_lost=0, incompatible_qos=0, dds_loss_events=False, last_seen=None,
            reasons=Counter()) for item in self.specs}

    def bind(self, node, message_loader, qos_factory, events_factory=None):
        """Call from the ROS owner thread, as with reconcile_gate() and close()."""
        if self.node is not None and self.node is not node:
            raise RuntimeError('adapter is already bound to a ROS node')
        self.node, self.message_loader = node, message_loader
        self.qos_factory, self.events_factory = qos_factory, events_factory

    def start(self):
        """Start only the disk worker; this does not initialize ROS or sensors."""
        with self.condition:
            if self.stopping:
                raise RuntimeError('archive adapter is closed')
            if self.worker is None:
                self.worker = threading.Thread(target=self._write_loop,
                    name='archive-ros-writer', daemon=True)
                self.worker.start()

    def _gate_enabled(self):
        try:
            enabled = self.gate() is True
            with self.condition:
                self.gate_error = None
            return enabled
        except Exception as error:
            with self.condition:
                self.gate_error = type(error).__name__
            return False

    def _drop(self, spec, reason, count=1):
        row = self.stats[spec.topic]
        row['dropped'] += count
        row['reasons'][reason] += count

    def _discard_queue(self, reason):
        while self.queue:
            packet = self.queue.popleft()
            self._drop(packet.spec, reason)
        self.pending_bytes = 0

    def _disable(self, reason):
        with self.condition:
            self.active = False
            self.epoch += 1
        self._destroy_subscriptions()
        with self.condition:
            # Detach the tap first, then drain the packets it already accepted.
            # Epoch invalidation rejects late callbacks, never queued bytes.
            self.condition.notify_all()

    def _destroy_subscriptions(self):
        """Detach subscriptions on the ROS owner thread, retaining failures."""
        with self.condition:
            subscriptions = {**self.retired_subscriptions, **self.subscriptions}
            self.subscriptions, self.retired_subscriptions = {}, {}
            self.subscription_qos.clear()
            self.qos_checked_at.clear()
        for topic, subscription in subscriptions.items():
            try:
                self.node.destroy_subscription(subscription)
                with self.condition:
                    if self.errors.get(topic, '').startswith('destroy:'):
                        self.errors.pop(topic, None)
            except Exception as error:
                with self.condition:
                    self.retired_subscriptions[topic] = subscription
                    self.errors[topic] = 'destroy:' + type(error).__name__

    def reconcile_gate(self):
        """Create/destroy subscriptions on the same ROS executor thread."""
        # An explicit empty selection makes IPC an uploader-only host.
        # Never turn an empty allowlist into subscriptions for every sensor.
        if not self.specs:
            return
        enabled = self._gate_enabled()
        with self.condition:
            was_active = self.active
            stopping = self.stopping
        if not enabled:
            if was_active or self.subscriptions or self.retired_subscriptions:
                self._disable('gate_closed')
            return
        if stopping:
            return
        if self.node is None:
            raise RuntimeError('bind a ROS node before enabling the gate')
        if self.retired_subscriptions:
            self._disable('subscription_cleanup')
            if self.retired_subscriptions:
                return
        self.start()
        with self.condition:
            if not self.active:
                self.epoch += 1
                self.active = True
                self.image_sampler.clear()
                for row in self.stats.values():
                    row['last_seen'] = None
                self.retry_at.clear()
                self.subscription_retry_at.clear()
                self.subscription_failures.clear()
            epoch = self.epoch
        for spec in self.specs:
            if spec.topic in self.subscriptions:
                # A publisher may appear later with VOLATILE/BEST_EFFORT QoS.
                # Refresh only latched channels, at a low rate; radar never
                # needs graph polling or subscriber churn on this path.
                if not spec.latched or self.monotonic() - self.qos_checked_at.get(spec.topic, 0) < 5:
                    continue
                self.qos_checked_at[spec.topic] = self.monotonic()
                try:
                    qos = self.qos_factory(spec)
                    signature = self._qos_signature(qos)
                    if signature is None or signature == self.subscription_qos.get(spec.topic):
                        continue
                    if self.node.destroy_subscription(self.subscriptions[spec.topic]) is False:
                        raise RuntimeError('SubscriptionCleanupIncomplete')
                    del self.subscriptions[spec.topic]
                except Exception as error:
                    with self.condition:
                        self.errors[spec.topic] = 'qos:' + type(error).__name__
                    continue
            if self.monotonic() < self.subscription_retry_at.get(spec.topic, 0):
                continue
            try:
                kind = self.message_loader(spec.type_name)
                options = {'raw': True}
                if self.events_factory is not None:
                    options['event_callbacks'] = self.events_factory(
                        lambda event, item=spec: self._message_lost(item, event))
                callback = lambda raw, item=spec, generation=epoch: self.receive(item, raw, generation)
                qos = self.qos_factory(spec)
                try:
                    subscription = self.node.create_subscription(kind, spec.topic, callback, qos, **options)
                except Exception as error:
                    if ('event_callbacks' not in options or
                            type(error).__name__ not in ('UnsupportedEventTypeException', 'NotImplementedError', 'TypeError')):
                        raise
                    # Optional loss events must not disable the raw receiver on
                    # an RMW that cannot report them. State records the gap.
                    options.pop('event_callbacks')
                    subscription = self.node.create_subscription(kind, spec.topic, callback, qos, **options)
                self.subscriptions[spec.topic] = subscription
                self.subscription_qos[spec.topic] = self._qos_signature(qos)
                self.qos_checked_at[spec.topic] = self.monotonic()
                self.subscription_retry_at.pop(spec.topic, None)
                self.subscription_failures.pop(spec.topic, None)
                with self.condition:
                    self.stats[spec.topic]['dds_loss_events'] = 'event_callbacks' in options
                    self.errors.pop(spec.topic, None)
            except Exception as error:
                with self.condition:
                    self.errors[spec.topic] = 'subscription:' + type(error).__name__
                    self.subscription_failures[spec.topic] += 1
                    delay = min(30, 2 ** min(5, self.subscription_failures[spec.topic] - 1))
                    self.subscription_retry_at[spec.topic] = self.monotonic() + delay

    @staticmethod
    def _qos_signature(qos):
        names = ('reliability','durability','depth')
        return tuple(getattr(qos, name, None) for name in names) if any(hasattr(qos, name) for name in names) else None

    def _message_lost(self, spec, event):
        count = getattr(event, 'total_count_change', None)
        if type(count) is int and count > 0:
            with self.condition:
                if hasattr(event, 'last_policy_kind'):
                    self.stats[spec.topic]['incompatible_qos'] += count
                    self.errors[spec.topic] = 'qos:IncompatiblePublisher'
                else:
                    self.stats[spec.topic]['dds_lost'] += count

    def receive(self, spec, raw, epoch):
        """Bounded callback: serialized bytes remain byte-for-byte unchanged."""
        wall = self.wall_clock()
        with self.condition:
            self.stats[spec.topic]['received'] += 1
            if self.stopping or not self.active or epoch != self.epoch:
                self._drop(spec, 'inactive_epoch')
                return
            if not isinstance(raw, (bytes, bytearray, memoryview)) or not math.isfinite(wall) or wall <= 0:
                self._drop(spec, 'invalid_message')
                return
            size = raw.nbytes if isinstance(raw, memoryview) else len(raw)
            if size == 0:
                self._drop(spec, 'empty_message')
                return
            self.stats[spec.topic]['last_seen'] = self.monotonic()
            if self.errors.get(spec.topic) == 'qos:IncompatiblePublisher':
                self.errors.pop(spec.topic, None)
            seconds = int(wall)
            timestamp_ns = seconds * 1_000_000_000 + round((wall - seconds) * 1_000_000_000)
            if spec.type_name == 'sensor_msgs/msg/Image' and not self.image_sampler.accept(spec.topic, timestamp_ns):
                self.stats[spec.topic]['sampled'] += 1
                return
            if len(self.queue) >= self.queue_capacity:
                self._drop(spec, 'queue_capacity')
                return
            if size > self.queue_bytes - self.pending_bytes:
                self._drop(spec, 'queue_bytes')
                return
            self.queue.append(_Packet(spec, bytes(raw), timestamp_ns, wall, epoch))
            self.pending_bytes += size
            self.stats[spec.topic]['enqueued'] += 1
            self.condition.notify()

    def _write_loop(self):
        next_tick = self.monotonic()
        while True:
            with self.condition:
                if not self.queue and not self.stopping:
                    self.condition.wait(.25)
                if self.stopping and not self.queue:
                    self.active = False
                    return
                packet = self.queue.popleft() if self.queue else None
                if packet is not None:
                    self.pending_bytes -= len(packet.data)
                    self.in_flight = 1
            if packet is not None:
                try:
                    if self.monotonic() < self.retry_at.get(packet.spec.topic, 0):
                        with self.condition:
                            self._drop(packet.spec, 'writer_backoff')
                        continue
                    try:
                        record = getattr(self.store, 'record_accepted', self.store.record)
                        accepted = record(packet.spec.sensor, packet.spec.topic,
                            packet.spec.type_name, packet.data, packet.timestamp_ns, wall_time=packet.wall_time)
                        with self.condition:
                            if accepted is False:
                                self._drop(packet.spec, 'store_rejected')
                            else:
                                self.stats[packet.spec.topic]['written'] += 1
                                self.errors.pop(packet.spec.topic, None)
                    except Exception as error:
                        with self.condition:
                            self._drop(packet.spec, 'writer_error')
                            self.errors[packet.spec.topic] = 'writer:' + type(error).__name__
                        self.retry_at[packet.spec.topic] = self.monotonic() + self.writer_retry_s
                finally:
                    with self.condition:
                        self.in_flight = 0
                        self.condition.notify_all()
            with self.condition:
                drained = not self.queue
            if drained and self.monotonic() >= next_tick:
                tick = getattr(self.store, 'tick', None)
                if callable(tick):
                    try:
                        tick()
                        with self.condition:
                            self.tick_error = None
                    except Exception as error:
                        with self.condition:
                            self.tick_error = type(error).__name__
                next_tick = self.monotonic() + 1.0

    def close(self, timeout=5.0):
        """Stop admission, drain accepted data, then permit node/store cleanup.

        False means the writer or subscription cleanup is still unfinished;
        the caller must retain the store and may retry close later.
        """
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
            worker = self.worker
        self._destroy_subscriptions()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        alive = worker is not None and worker.is_alive()
        with self.condition:
            if alive:
                self.tick_error = 'WriterShutdownTimeout'
            else:
                self.active = False
                if self.tick_error == 'WriterShutdownTimeout':
                    self.tick_error = None
            return not alive and not self.retired_subscriptions

    def snapshot(self):
        now = self.monotonic()
        with self.condition:
            topics = {}
            for spec in self.specs:
                row = self.stats[spec.topic]
                age = None if row['last_seen'] is None else max(0, now - row['last_seen'])
                state = ('DISABLED' if not self.active else 'ERROR' if spec.topic in self.errors else
                         'WAITING_UPSTREAM' if row['last_seen'] is None else
                         'AVAILABLE' if age <= 2 or spec.latched else 'STALE')
                topics[spec.topic] = {**row, 'reasons': dict(row['reasons']),
                    'sensor': spec.sensor, 'type': spec.type_name, 'state': state, 'ageS': age,
                    'error': self.errors.get(spec.topic),
                    'subscriptionRetryInS': max(0, self.subscription_retry_at.get(spec.topic, now) - now)}
            return dict(active=self.active, state='ERROR' if self.gate_error or self.tick_error or self.retired_subscriptions else
                'DISABLED' if not self.active else 'ERROR' if self.errors else
                'WAITING_UPSTREAM' if all(row['last_seen'] is None for row in self.stats.values()) else 'LISTENING',
                queueMessages=len(self.queue), queueBytes=self.pending_bytes,
                inFlightMessages=self.in_flight,
                capacity=self.queue_capacity, capacityBytes=self.queue_bytes, epoch=self.epoch,
                subscriptionCleanupPending=len(self.retired_subscriptions),
                workerAlive=self.worker is not None and self.worker.is_alive(),
                gateError=self.gate_error, tickError=self.tick_error, topics=topics,
                timestampBasis='recorder wall-clock receive time; original sensor timestamps remain in raw CDR',
                lossCoverage='observed callbacks and reported DDS loss events; upstream loss is not measurable here')


def build_archive_node(store, gate, **options):
    """ROS runtime factory. The caller owns rclpy.init/spin/shutdown and context."""
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from rosidl_runtime_py.utilities import get_message
    try:
        from rclpy.event_handler import SubscriptionEventCallbacks
        events_factory = lambda callback: SubscriptionEventCallbacks(message_lost=callback, incompatible_qos=callback)
    except ImportError:
        events_factory = None

    adapter = ArchiveRosAdapter(store, gate, **options)
    node = Node('garage_raw_archive', enable_rosout=False, start_parameter_services=False)
    def qos(spec):
        try:
            publishers = node.get_publishers_info_by_topic(spec.topic) if spec.latched else ()
        except Exception:
            publishers = ()
        policy = subscription_policy(spec, publishers)
        return QoSProfile(depth=policy['depth'],
            history=HistoryPolicy.KEEP_LAST,
            reliability=getattr(ReliabilityPolicy, policy['reliability']),
            durability=getattr(DurabilityPolicy, policy['durability']))
    adapter.bind(node, get_message, qos, events_factory)
    node.create_timer(.5, adapter.reconcile_gate)
    # Gate check happens before any sensor type load or subscription creation.
    adapter.reconcile_gate()
    return node, adapter
