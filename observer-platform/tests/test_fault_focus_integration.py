"""Focused evidence through a durable collector queue and isolated cloud history."""
import sqlite3

from cloud.backend.fault_logs import FaultLogs
from collector.faults import FaultMonitor, FaultSpool


def test_dynamic_key_failure_burst_reaches_cloud_as_one_incident_with_exact_count(tmp_path):
    time = [1700000000.0]
    spool = FaultSpool(tmp_path/'spool.sqlite3')
    monitor = FaultMonitor(spool, lambda: time[0], lambda: time[0])
    db = sqlite3.connect(':memory:')
    logs = FaultLogs(db, clock=lambda: time[0])
    try:
        for index in range(51):
            time[0] += .01
            monitor.observe_log('fastlio', f'IMU and LiDAR not Synced, IMU time: {1000 + index}, lidar header time: {900 + index}', 20)
            monitor.observe_log('node', f'Waiting for optional service {index}', 30)
        first = spool.batch()
        assert len(first) == 1
        with db:
            assert logs.add_batch('robot', 'ipc', first) == 1
        spool.ack([e['id'] for e in first])
        time[0] += 301
        monitor.tick()
        rest = spool.batch()
        assert len(rest) == 1 and rest[0]['details']['occurrences'] == 50
        with db:
            assert logs.add_batch('robot', 'ipc', rest) == 1
            assert logs.add_batch('robot', 'ipc', rest) == 0
        spool.ack([e['id'] for e in rest])
        rows = logs.query({})
        assert rows['total'] == 1
        incident = rows['items'][0]
        assert incident['code'] == 'SENSOR_TIME_SYNC_REPORTED'
        assert incident['count'] == 51
        assert incident['timestamp'] == first[0]['timestamp']
        assert incident['last_timestamp'] == rest[0]['timestamp']
        assert '1050' in incident['details']['raw_message']
        assert not spool.batch()
        assert logs.summary()['total'] == 1
        time[0] += 601
        monitor.observe_log('fastlio', 'IMU and LiDAR not Synced, IMU time: 2000, lidar header time: 1900', 20)
        with db:
            logs.add_batch('robot', 'ipc', spool.batch())
        assert logs.query({})['total'] == 2
    finally:
        spool.close()
        db.close()


def test_escalating_severity_is_recorded_immediately_while_repeats_are_throttled(tmp_path):
    spool = FaultSpool(tmp_path/'spool.sqlite3')
    monitor = FaultMonitor(spool, lambda: 1.0, lambda: 1700000000.0)
    try:
        for level in (30, 40, 50):
            monitor.observe_log('controller_server', 'Goal execution timed out', level)
            monitor.observe_log('controller_server', 'Goal execution timed out', level)
        assert [event['level'] for event in spool.batch()] == ['WARN', 'ERROR', 'FATAL']
        monitor.flush_suppressed(force=True)
        assert sum(event['details']['occurrences'] for event in spool.batch()) == 6
    finally:
        spool.close()
