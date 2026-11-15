"""Opt-in, real DDS coexistence check; no physical devices or domain 0.

Run from the package root after sourcing ROS and the installed Livox messages:
  ROBOT_ARCHIVE_DDS_INTEGRATION=1 /usr/bin/python3 -m pytest -q -s \
      tests/test_archive_dds_coexistence.py

Three independent child processes publish synthetic MID360/IMU data, consume it
like FAST-LIO, and archive it. Only those children get localhost-only DDS and a
random nonzero domain. The test never loads a device/navigation launch file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time

import pytest


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _child(role, directory):
    """Import ROS only after the parent prepared this child's isolated env."""
    if (os.environ.get('ROS_LOCALHOST_ONLY') != '1'
            or not 110 <= int(os.environ.get('ROS_DOMAIN_ID', '0')) <= 200
            or os.environ.get('ROBOT_ARCHIVE_DDS_CHILD') != '1'):
        raise RuntimeError('refusing ROS initialization outside isolated test environment')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Imu
    from livox_ros_driver2.msg import CustomMsg, CustomPoint
    from collector.archive_core import ArchiveStore, GarageGate, atomic_json
    from collector.archive_ros import build_archive_node

    directory = Path(directory)
    report = directory / (role + '.json')
    stop = directory / (role + '.stop')
    node = adapter = store = None
    rclpy.init(args=[], domain_id=int(os.environ['ROS_DOMAIN_ID']))
    observations = {'lidar': [], 'imu': []}
    sequence = imu_sequence = int(os.environ.get('ROBOT_ARCHIVE_DDS_SEQUENCE_BASE', '0'))
    try:
        if role == 'archive':
            gate = GarageGate(directory / 'gate.json')
            store = ArchiveStore(directory / 'spool', 'synthetic-dds-coexistence',
                gate=gate, compression=False, min_free_bytes=1024**2, background_resources=True,
                max_spool_bytes=64 * 1024**2)
            node, adapter = build_archive_node(store, gate.enabled,
                sensors=('雷达MID360',))
        else:
            node = Node('synthetic_archive_test_' + role,
                enable_rosout=False, start_parameter_services=False)
            if role == 'publisher':
                lidar = node.create_publisher(CustomMsg, '/livox/lidar', 5)
                imu = node.create_publisher(Imu, '/livox/imu', 5)
                points = [CustomPoint(offset_time=i * 1000, x=float(i) / 100,
                    y=0.25, z=0.5, reflectivity=100, tag=0, line=i % 4)
                    for i in range(20_000)]
                cloud = CustomMsg(timebase=0, point_num=len(points), points=points)
                cloud.header.frame_id = 'synthetic_archive_dds_test'

                def publish():
                    nonlocal sequence
                    sequence += 1
                    cloud.timebase = sequence
                    cloud.header.stamp = node.get_clock().now().to_msg()
                    lidar.publish(cloud)

                def publish_imu():
                    nonlocal imu_sequence
                    imu_sequence += 1
                    acceleration = Imu()
                    acceleration.header.stamp = node.get_clock().now().to_msg()
                    acceleration.header.frame_id = 'synthetic_archive_dds_test'
                    acceleration.linear_acceleration.x = float(imu_sequence)
                    imu.publish(acceleration)

                node.create_timer(.1, publish)
                node.create_timer(.005, publish_imu)
            elif role == 'algorithm':
                def consume(kind, sequence):
                    observations[kind].append([sequence, time.monotonic()])

                node.create_subscription(CustomMsg, '/livox/lidar',
                    lambda message: consume('lidar', message.timebase), qos_profile_sensor_data)
                node.create_subscription(Imu, '/livox/imu',
                    lambda message: consume('imu', int(message.linear_acceleration.x)),
                    qos_profile_sensor_data)
            else:
                raise ValueError('unknown test child')
        next_report = 0
        deadline = time.monotonic() + 40
        while not stop.exists() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.02)
            if time.monotonic() < next_report:
                continue
            if role == 'archive':
                value = {'ros': adapter.snapshot(), 'activeSegments': len(store.active)}
            elif role == 'publisher':
                value = {'sent': sequence, 'lidarReaders': lidar.get_subscription_count(),
                    'imuReaders': imu.get_subscription_count()}
            else:
                value = {'observations': observations}
            atomic_json(report, dict(value, monotonic=time.monotonic(), pid=os.getpid()))
            next_report = time.monotonic() + .2
    finally:
        if adapter is not None:
            if not adapter.close(timeout=5):
                raise RuntimeError('synthetic archive writer did not drain')
        if store is not None:
            store.close()
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


def _wait(check, timeout, children, directory):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for role, process in children.items():
            if process.poll() is not None:
                raise AssertionError(role + ' exited: ' +
                    (directory / (role + '.log')).read_text()[-6000:])
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('isolated DDS condition timed out; reports: ' +
        repr({role: _read(directory / (role + '.json')) for role in children}))


def _isolated_environment(directory):
    environment = dict(os.environ)
    for key in tuple(environment):
        if (key.startswith('ROS_SECURITY') or key in {
                'ROS_DISCOVERY_SERVER', 'ROS_SUPER_CLIENT', 'ROS_NAMESPACE',
                'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE',
                'RMW_FASTRTPS_USE_QOS_FROM_XML', 'RMW_FASTRTPS_PUBLICATION_MODE',
                'CYCLONEDDS_URI'}):
            environment.pop(key, None)
    domain = 110 + secrets.randbelow(91)
    environment.update(ROS_DOMAIN_ID=str(domain), ROS_LOCALHOST_ONLY='1',
        RMW_IMPLEMENTATION='rmw_fastrtps_cpp', ROBOT_ARCHIVE_DDS_CHILD='1',
        ROS_LOG_DIR=str(directory / 'ros-logs'))
    return environment, domain


@pytest.mark.skipif(os.environ.get('ROBOT_ARCHIVE_DDS_INTEGRATION') != '1',
    reason='opt-in real DDS check; requires ROS/Livox environment and local test sockets')
def test_synthetic_lidar_continues_across_archive_gate_transitions(tmp_path):
    from collector.archive_core import GarageGate

    environment, domain = _isolated_environment(tmp_path)
    children, handles = {}, []
    gate = GarageGate(tmp_path / 'gate.json')
    gate.set(False)

    def report(role):
        return _read(tmp_path / (role + '.json'))

    def archive_idle():
        value = report('archive')
        ros = value.get('ros', {})
        return (ros.get('active') is False and ros.get('queueMessages') == 0
            and ros.get('inFlightMessages') == 0 and value.get('activeSegments') == 0)

    try:
        for role in ('publisher', 'algorithm', 'archive'):
            handle = (tmp_path / (role + '.log')).open('w')
            handles.append(handle)
            children[role] = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--dds-child', role, str(tmp_path)], env=environment,
                stdout=handle, stderr=subprocess.STDOUT)
        _wait(lambda: archive_idle() and all(
            len(report('algorithm').get('observations', {}).get(kind, [])) >= 10
            for kind in ('lidar', 'imu')), 9, children, tmp_path)
        before = time.monotonic()
        _wait(lambda: time.monotonic() >= before + 3, 4, children, tmp_path)
        assert archive_idle()
        assert not list((tmp_path / 'spool').rglob('*.db3'))
        assert all(row['written'] == 0
            for row in report('archive')['ros']['topics'].values())

        enabled_wall = time.time()
        gate.set(True)
        _wait(lambda: report('archive').get('ros', {}).get('active') is True,
            2, children, tmp_path)
        enabled = time.monotonic()
        _wait(lambda: time.monotonic() >= enabled + 7, 8, children, tmp_path)
        gate.set(False)
        _wait(archive_idle, 3, children, tmp_path)
        disabled_wall = time.time()
        disabled = time.monotonic()
        settled = report('archive')['ros']['topics']
        _wait(lambda: time.monotonic() >= disabled + 3, 4, children, tmp_path)
        assert archive_idle()
        assert {key: row['written'] for key, row in settled.items()} == {
            key: row['written'] for key, row in report('archive')['ros']['topics'].items()}
        assert all(process.poll() is None for process in children.values())
        assert report('publisher')['lidarReaders'] == 1
        assert report('publisher')['imuReaders'] == 1

        observations = report('algorithm')['observations']
        phase_counts, maximum_gaps = {}, {}
        for kind, rows in observations.items():
            rows = [(sequence, when) for sequence, when in rows if when >= before]
            assert all(right[0] > left[0] for left, right in zip(rows, rows[1:]))
            maximum_gaps[kind] = max(right[1] - left[1] for left, right in zip(rows, rows[1:]))
            assert maximum_gaps[kind] < .75, (kind, maximum_gaps[kind])
            phase_counts[kind] = [sum(begin <= when < end for _, when in rows)
                for begin, end in ((before, enabled), (enabled, disabled),
                    (disabled, disabled + 2.7))]
            assert all(count >= limit for count, limit in zip(phase_counts[kind], (20, 45, 18)))

        # Read the actual ROS bag SQLite records and deserialize their CDR bytes.
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import Imu
        from livox_ros_driver2.msg import CustomMsg
        bag_counts = {'/livox/lidar': 0, '/livox/imu': 0}
        for path in (tmp_path / 'spool').rglob('*.db3'):
            with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as database:
                records = database.execute('SELECT topics.name, messages.timestamp, messages.data '
                    'FROM messages JOIN topics ON topics.id=messages.topic_id ORDER BY messages.id')
                for topic, timestamp, data in records:
                    assert enabled_wall <= timestamp / 1e9 <= disabled_wall
                    message = deserialize_message(data, CustomMsg if topic == '/livox/lidar' else Imu)
                    assert message.header.frame_id == 'synthetic_archive_dds_test'
                    if topic == '/livox/lidar':
                        assert message.point_num == len(message.points) == 20_000
                        assert message.points[-1].reflectivity == 100
                    bag_counts[topic] += 1
        assert all(count >= 45 for count in bag_counts.values())
        assert bag_counts == {topic: row['written'] for topic, row in settled.items()}
        assert all(row['dropped'] == 0 and row['dds_lost'] == 0 for row in settled.values())
        print(json.dumps(dict(domain=domain, pointsPerMessage=20_000, publishHz=10, imuPublishHz=200,
            algorithmPhaseCounts=phase_counts, maximumReceiveGapS=maximum_gaps,
            archivedMessages=bag_counts, publisherAliveAfterArchiveStop=True), ensure_ascii=False))
    finally:
        # These are only Popen handles created above, never ROS process-name kills.
        failure_active = sys.exc_info()[0] is not None
        for role in children:
            (tmp_path / (role + '.stop')).touch()
        for process in children.values():
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        for handle in handles:
            handle.close()
        if not failure_active:
            assert all(process.returncode == 0 for process in children.values()), {
                role: (tmp_path / (role + '.log')).read_text()[-6000:]
                for role, process in children.items() if process.returncode != 0}


@pytest.mark.skipif(os.environ.get('ROBOT_ARCHIVE_DDS_INTEGRATION') != '1',
    reason='opt-in real DDS check; requires ROS/Livox environment and local test sockets')
def test_late_publisher_restarts_without_archive_or_algorithm_restart(tmp_path):
    """Passive subscribers wait for, and rediscover, independently owned sources."""
    from collector.archive_core import GarageGate

    environment, domain = _isolated_environment(tmp_path)
    children, handles, completed = {}, [], []
    gate = GarageGate(tmp_path / 'gate.json')
    gate.set(True)

    def report(role):
        return _read(tmp_path / (role + '.json'))

    def spawn(role, sequence_base=0):
        (tmp_path / (role + '.stop')).unlink(missing_ok=True)
        (tmp_path / (role + '.json')).unlink(missing_ok=True)
        handle = (tmp_path / (role + '.log')).open('a')
        handles.append(handle)
        child_environment = dict(environment,
            ROBOT_ARCHIVE_DDS_SEQUENCE_BASE=str(sequence_base))
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
            '--dds-child', role, str(tmp_path)], env=child_environment,
            stdout=handle, stderr=subprocess.STDOUT)
        children[role] = process
        return process

    def stop_child(role):
        # Only handles created in this test; never use process-name or group kills.
        process = children[role]
        (tmp_path / (role + '.stop')).touch()
        process.wait(timeout=6)
        assert process.returncode == 0, (tmp_path / (role + '.log')).read_text()[-6000:]
        completed.append(process)
        del children[role]

    def topic_counts():
        return {topic: int(row['written']) for topic, row in
            report('archive').get('ros', {}).get('topics', {}).items()}

    def phase_received(sequence_base, baseline):
        rows = report('algorithm').get('observations', {})
        return (all(sum(sequence_base < number < sequence_base + 100_000
                for number, _ in rows.get(kind, [])) >= 10 for kind in ('lidar', 'imu'))
            and all(topic_counts().get(topic, 0) >= count + 10
                for topic, count in baseline.items()))

    try:
        archive = spawn('archive')
        algorithm = spawn('algorithm')
        _wait(lambda: report('archive').get('ros', {}).get('active') is True
            and bool(report('algorithm')), 8, children, tmp_path)
        before = topic_counts()
        assert set(before) == {'/livox/lidar', '/livox/imu'}
        assert all(value == 0 for value in before.values())
        assert all(row['state'] == 'WAITING_UPSTREAM'
            for row in report('archive')['ros']['topics'].values())
        assert not list((tmp_path / 'spool').rglob('*.db3'))

        first_publisher = spawn('publisher', 1_000_000)
        _wait(lambda: phase_received(1_000_000, before), 9, children, tmp_path)
        first_counts = topic_counts()
        _wait(lambda: report('publisher').get('lidarReaders') == 2
            and report('publisher').get('imuReaders') == 2, 3, children, tmp_path)
        stop_child('publisher')
        assert archive.poll() is None and algorithm.poll() is None

        # Its owner restarts the source. The recorder and algorithm are untouched.
        second_publisher = spawn('publisher', 2_000_000)
        assert second_publisher.pid != first_publisher.pid
        _wait(lambda: phase_received(2_000_000, first_counts), 9, children, tmp_path)
        assert report('archive')['pid'] == archive.pid
        assert report('algorithm')['pid'] == algorithm.pid
        _wait(lambda: report('publisher').get('lidarReaders') == 2
            and report('publisher').get('imuReaders') == 2, 3, children, tmp_path)

        gate.set(False)
        _wait(lambda: report('archive').get('ros', {}).get('active') is False
            and report('archive')['ros']['queueMessages'] == 0
            and report('archive')['ros']['inFlightMessages'] == 0
            and report('archive').get('activeSegments') == 0, 4, children, tmp_path)
        settled = topic_counts()
        stop_child('archive')
        counts_after_archive_exit = {kind: len(rows) for kind, rows in
            report('algorithm')['observations'].items()}
        _wait(lambda: all(len(report('algorithm')['observations'].get(kind, []))
                >= count + 10 for kind, count in counts_after_archive_exit.items())
            and report('publisher').get('lidarReaders') == 1
            and report('publisher').get('imuReaders') == 1, 4, children, tmp_path)
        assert algorithm.poll() is None and second_publisher.poll() is None

        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import Imu
        from livox_ros_driver2.msg import CustomMsg
        phase_counts = {base: {'/livox/lidar': 0, '/livox/imu': 0}
            for base in (1_000_000, 2_000_000)}
        for path in (tmp_path / 'spool').rglob('*.db3'):
            with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as database:
                for topic, data in database.execute('SELECT topics.name, messages.data '
                        'FROM messages JOIN topics ON topics.id=messages.topic_id'):
                    message = deserialize_message(data,
                        CustomMsg if topic == '/livox/lidar' else Imu)
                    assert message.header.frame_id == 'synthetic_archive_dds_test'
                    sequence = (message.timebase if topic == '/livox/lidar'
                        else int(message.linear_acceleration.x))
                    base = (sequence // 1_000_000) * 1_000_000
                    assert base in phase_counts
                    if topic == '/livox/lidar':
                        assert message.point_num == len(message.points) == 20_000
                    phase_counts[base][topic] += 1
        assert all(count >= 10 for row in phase_counts.values() for count in row.values())
        assert settled == {topic: sum(row[topic] for row in phase_counts.values())
            for topic in settled}
        print(json.dumps(dict(domain=domain, archivePid=archive.pid,
            algorithmPid=algorithm.pid,
            publisherPids=[first_publisher.pid, second_publisher.pid],
            archivedMessagesByPublisher=phase_counts,
            archiveStartedBeforePublisher=True, archiveSurvivedPublisherRestart=True,
            algorithmAliveAndReceivingAfterArchiveExit=True), ensure_ascii=False))
    finally:
        failure_active = sys.exc_info()[0] is not None
        for role in children:
            (tmp_path / (role + '.stop')).touch()
        for process in children.values():
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        for handle in handles:
            handle.close()
        if not failure_active:
            assert all(process.returncode == 0
                for process in [*children.values(), *completed])


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--dds-child':
        raise SystemExit('This helper is launched by its opt-in pytest test.')
    _child(sys.argv[2], sys.argv[3])
