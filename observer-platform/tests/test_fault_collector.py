"""Isolated fault evidence and durable retransmission; no ROS/device/network IO."""
import asyncio
import json
import struct
import time
import uuid
from types import SimpleNamespace as NS

import pytest
from aiohttp import web

from collector.config import Config
from collector.faults import FaultMonitor, FaultSpool, RosLogTail, clean_text, custom_message_metadata, source_timestamp
from collector.transport import LatestQueue, Uploader
from shared.faults import validate_event, validate_events_payload
from shared.protocol import ProtocolError, validate_envelope


def event(**updates):
    value = dict(id=str(uuid.uuid4()), timestamp=time.time(), level='ERROR',
                 code='TEST_FAULT', component='test-only-component', message='隔离测试故障', details={})
    return dict(value, **updates)


def envelope(events):
    return dict(schema_version=1, robot_id='test-robot', source_id='test-source',
                session_id=str(uuid.uuid4()), sequence=1, kind='events', topic='/observer/events',
                message_timestamp=None, collector_timestamp=time.time(), payload={'events': events})


def make_monitor(tmp_path, capacity=100):
    clock = [1.0]
    spool = FaultSpool(tmp_path/'faults.sqlite3', capacity)
    return spool, FaultMonitor(spool, lambda: clock[0], lambda: 1700000000 + clock[0]), clock


@pytest.mark.parametrize('updates', [
    {'id': 'bad'}, {'timestamp': float('nan')}, {'timestamp': True}, {'timestamp': 253402300800},
    {'level': {}}, {'level': 'DEBUG'}, {'code': 'lower'}, {'message': 'x'*2049},
    {'component': ''}, {'details': []}, {'details': {'x': float('inf')}},
    {'details': {'x': 10**1000}}, {'details': {'x': 'x'*2049}},
])
def test_event_validation_rejects_bad_or_unbounded_input(updates):
    with pytest.raises((ValueError, TypeError)):
        validate_event(event(**updates))
    with pytest.raises(ProtocolError):
        validate_envelope(envelope([event(**updates)]))


def test_event_batch_validation_and_protocol():
    events = [event(), event(level='INFO')]
    assert validate_events_payload({'events': events})['events'] == events
    assert validate_envelope(envelope(events))['payload']['events'] == events
    for invalid in ([], events*17, [events[0], events[0]]):
        with pytest.raises(ValueError):
            validate_events_payload({'events': invalid})
    packet = envelope(events)
    packet['topic'] = '/wrong'
    with pytest.raises(ProtocolError):
        validate_envelope(packet)


def test_spool_restarts_without_losing_ids_and_ack_only_sent_ids(tmp_path):
    path = tmp_path/'faults.sqlite3'
    spool = FaultSpool(path)
    first, second = event(), event()
    spool.add(first)
    spool.close()
    spool = FaultSpool(path)
    assert spool.batch() == [first]
    spool.add(second)
    spool.ack([first['id']])
    assert spool.batch() == [second]
    spool.ack([first['id']])
    assert spool.batch() == [second]
    assert path.stat().st_mode & 0o777 == 0o600
    spool.close()


def test_spool_overflow_is_bounded_and_explicit(tmp_path):
    spool = FaultSpool(tmp_path/'faults.sqlite3', capacity=3)
    for _ in range(7):
        spool.add(event())
    pending = spool.batch()
    assert len(pending) == 3
    assert any(e['code'] == 'FAULT_LOG_OVERFLOW' for e in pending)
    assert spool.stats()['dropped_total'] > 0
    spool.close()


def test_monitor_initial_idle_silent_records_interruption_without_recovery_noise(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    clock[0] += 100
    monitor.tick()
    assert spool.batch() == []  # Never started is not disconnected.
    monitor.observe_stream('/livox/imu', 1000)
    clock[0] += 3
    monitor.tick()
    monitor.tick()
    assert [e['code'] for e in spool.batch()] == ['DEVICE_DATA_INTERRUPTED']
    monitor.observe_stream('/livox/imu', 1003)
    assert [e['code'] for e in spool.batch()] == ['DEVICE_DATA_INTERRUPTED']
    spool.close()


def test_source_timestamp_retains_reset_clock_and_anomaly_event_uses_wall_time(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    assert source_timestamp(NS(header=NS(stamp=NS(sec=0, nanosec=0)))) == 0
    assert source_timestamp(NS()) is None
    monitor.observe_stream('/livox/imu', 1000, timing=True)
    clock[0] += .01
    monitor.observe_stream('/livox/imu', 999, timing=True)
    record = spool.batch()[0]
    assert record['timestamp'] == 1700000000 + clock[0]
    assert record['code'] == 'IMU_TIMESTAMP_ROLLBACK'
    assert record['details']['source_timestamp'] == 999
    clock[0] += .01
    monitor.observe_stream('/livox/imu', 0, timing=True)
    assert len(spool.batch()) == 1
    spool.close()


def test_sample_time_gap_detected_without_claiming_network_cause(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    monitor.observe_stream('/livox/lidar', 1000, timing=True)
    clock[0] += .1
    monitor.observe_stream('/livox/lidar', 1002, timing=True)
    assert spool.batch()[0]['code'] == 'LIDAR_TIMESTAMP_GAP'
    assert spool.batch()[0]['details']['receive_gap_s'] == .1
    spool.close()


def test_point_time_normal_cross_packet_reset_is_not_wrap(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    make = lambda offsets: NS(points=[NS(offset_time=t) for t in offsets], timebase=123)
    monitor.observe_points(make([0, 1000, 2000]))
    monitor.observe_points(make([0, 1000, 2000]))
    assert not spool.batch()
    monitor.observe_points(make([0, 4_000_000_000, 20]))
    assert spool.batch()[0]['code'] == 'LIDAR_POINT_TIME_WRAP'
    clock[0] += 1
    monitor.observe_points(make([0, 1000000, 20]))
    assert spool.batch()[1]['code'] == 'LIDAR_POINT_TIME_NONMONOTONIC'
    spool.close()


def test_point_time_inspection_is_bounded(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    accesses = []
    class Points:
        def __len__(self):
            return 200000
        def __getitem__(self, index):
            accesses.append(index)
            return NS(offset_time=index)
    monitor.observe_points(NS(points=Points()))
    assert len(accesses) <= 1025 and accesses[-1] == 199999
    assert not spool.batch()
    spool.close()


def test_repeated_rosout_warning_throttles_but_tracks_evidence(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    for _ in range(20):
        monitor.observe_log('controller_server', 'Goal execution timed out', 40, 123)
    assert len(spool.batch()) == 1
    assert spool.batch()[0]['code'] == 'NAVIGATION_TIMEOUT'
    clock[0] += 301
    monitor.observe_log('controller_server', 'Goal execution timed out', 40, 124)
    assert spool.batch()[1]['details']['similar_events_suppressed_since_previous'] == 19
    monitor.observe_log('example', 'routine healthy status', 20)
    assert len(spool.batch()) == 2
    monitor.observe_log('fastlio', 'lidar loop back, clear buffer', 20)
    assert spool.batch()[2]['code'] == 'SENSOR_TIME_ROLLBACK_REPORTED'
    assert '尚未' in spool.batch()[2]['details']['cause']
    spool.close()


def test_credentials_are_redacted_before_spool(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    message = 'Bearer secret-value token=abc password="hello world" {"api_key":"sensitive"} https://bob:password@example.org?q=1&key=private'
    monitor.observe_log('test-node', message, 40)
    raw = json.dumps(spool.batch(), ensure_ascii=False)
    for secret in ('secret-value', 'abc', 'hello world', 'sensitive', 'bob:password', 'private'):
        assert secret not in raw
    assert '[已隐藏]' in raw
    spool.close()


def test_diagnostic_and_high_temperature_context(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    monitor.update_context({'thermalZones':[{'name':'cpu', 'temperatureC':87}],
                            'network':[{'name':'eth0', 'up':True, 'rxBytesPerSec':123}]})
    monitor.observe_diagnostics(NS(status=[NS(level=b'\x02', name='sensor', message='bad clock',
                                              hardware_id='test-only', values=[])]))
    records = spool.batch()
    assert records[0]['code'] == 'HOST_TEMPERATURE_HIGH'
    assert records[1]['code'] == 'ROS_DIAGNOSTIC_ERROR'
    assert records[1]['details']['context']['network'][0]['rxBytesPerSec'] == 123
    spool.close()


def test_navigation_abort_and_camera_preview_transitions(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    status = NS(status_list=[NS(status=6, goal_info=NS(goal_id=NS(uuid=list(range(16)))))])
    monitor.observe_action_status('/spin/_action/status', status)
    monitor.observe_action_status('/spin/_action/status', status)
    assert len(spool.batch()) == 1
    monitor.observe_media_status('usbFront', {'status':'WAITING_UPSTREAM'})
    assert len(spool.batch()) == 1
    monitor.observe_media_status('usbFront', {'status':'AVAILABLE'})
    monitor.observe_media_status('usbFront', {'status':'STALE', 'reason':'新帧未更新'})
    monitor.observe_media_status('usbFront', {'status':'STALE'})
    monitor.observe_media_status('usbFront', {'status':'AVAILABLE'})
    assert [r['code'] for r in spool.batch()] == ['NAVIGATION_ABORTED', 'CAMERA_PREVIEW_INTERRUPTED']
    spool.close()


def test_tail_skips_history_retains_new_lines_restart_and_rotation(tmp_path):
    root = tmp_path/'roslogs'
    root.mkdir()
    path = root/'fastlio_mapping.log'
    path.write_text('lidar loop back, clear buffer\n')
    spool, monitor, clock = make_monitor(tmp_path)
    tail = RosLogTail(monitor, root)
    tail.poll()
    assert not spool.batch()
    with path.open('a') as out:
        out.write('IMU and LiDAR not Synced\n')
    tail.poll()
    assert spool.batch()[0]['code'] == 'SENSOR_TIME_SYNC_REPORTED'
    # Persisted initial cursor also survives a collector restart.
    tail = RosLogTail(monitor, root)
    tail.poll()
    assert len(spool.batch()) == 1
    with path.open('a') as out:
        out.write('lidar loop back, clear buffer\n')
    tail.poll()
    assert len(spool.batch()) == 2
    # Actual truncation must reset the cursor; clear the throttle interval.
    clock[0] += 301
    path.write_text('lidar loop back, clear buffer\n')
    tail.poll()
    assert len(spool.batch()) == 3
    spool.close()


def test_tail_does_not_drop_lines_beyond_per_poll_line_cap(tmp_path):
    root = tmp_path/'roslogs';root.mkdir()
    path = root/'node.log';path.write_text('')
    spool, monitor, _ = make_monitor(tmp_path, capacity=1000)
    tail = RosLogTail(monitor, root)
    tail.poll()
    with path.open('a') as out:
        out.write(''.join('[ERROR] unique fault family ' + chr(97 + i//26) + chr(97 + i%26) + '\n' for i in range(300)))
    for _ in range(4):
        tail.poll()
    assert spool.stats()['pending'] == 300
    spool.close()


def test_ack_in_flight_does_not_drop_replacement_ids(tmp_path):
    spool = FaultSpool(tmp_path/'faults.sqlite3')
    first, second = event(), event()
    spool.add(first)
    uploader = Uploader(Config('test-robot','test-source', cloud_url='https://isolated.invalid', token='x'*32))
    uploader.enable_events(spool.ack)
    uploader.enqueue('events','/observer/events',{'events':spool.batch()})
    old = uploader.queue.peek()
    spool.add(second)
    uploader.enqueue('events','/observer/events',{'events':spool.batch()})
    uploader._ack_events(old)
    uploader.queue.remove(old)
    assert spool.batch() == [second]
    assert uploader.queue.peek() is not None
    spool.close()


def test_real_loopback_http_requires_event_receipt_and_preserves_retry_ids(tmp_path):
    async def run():
        spool = FaultSpool(tmp_path/'faults.sqlite3')
        fault = event(); spool.add(fault)
        received = []
        uploader = None
        async def handler(request):
            payload = await request.json()
            received.append(payload['payload']['events'][0]['id'])
            if len(received) == 1:
                return web.json_response({'accepted':False})
            uploader.stop_event.set()
            return web.json_response({'accepted':True})
        app = web.Application(); app.router.add_post('/api/v1/ingest',handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        uploader = Uploader(Config('test-robot','test-source',cloud_url=f'http://127.0.0.1:{port}', token='x'*32,allow_insecure_local=True))
        uploader.enable_events(spool.ack)
        uploader.enqueue('events','/observer/events',{'events':spool.batch()})
        async def fast_pause(_):
            await asyncio.sleep(0)
        uploader._pause=fast_pause
        await asyncio.wait_for(uploader.run(),5)
        assert received == [fault['id'],fault['id']]
        assert not spool.batch()
        await runner.cleanup();spool.close()
    asyncio.run(run())


def test_repeat_summary_flushes_even_if_the_fault_stops(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    for _ in range(12):
        monitor.observe_log('test_node', 'identical severe failure', 40)
    clock[0] += 301
    monitor.tick()
    rows = spool.batch()
    assert len(rows) == 2 and rows[-1]['code'] == rows[0]['code'] == 'ROS_ERROR'
    assert rows[-1]['details']['occurrences'] == 11
    assert rows[-1]['details']['suppressed_count'] == 11
    monitor.tick()
    assert spool.stats()['pending'] == 2
    spool.close()


def test_ros_fatal_level_is_preserved(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    monitor.observe_log('fastlio', 'lidar loop back, clear buffer', 50)
    assert spool.batch()[0]['level'] == 'FATAL'
    spool.close()


@pytest.mark.parametrize('endian', ['<', '>'])
def test_cdr_metadata_alignment_and_point_offset_endianness(endian):
    for frame in ('', 'livox_frame', 'x' * 32, 'x' * 33):
        raw = bytearray(b'\x00\x01\x00\x00' if endian == '<' else b'\x00\x00\x00\x00')
        raw.extend(struct.pack(endian + 'iII', 1700000000, 123, len(frame) + 1))
        raw.extend(frame.encode() + b'\x00')
        while (len(raw)-4) % 8:
            raw.append(0)
        raw.extend(struct.pack(endian + 'QIB3xI', 12345678, 3, 0, 3))
        for index, offset in enumerate((0,1000,2000)):
            if index:
                raw.append(0)
            raw.extend(struct.pack(endian + 'IfffBBB', offset,1.,2.,3.,10,0,0))
        parsed = custom_message_metadata(raw)
        assert parsed.timebase == 12345678
        assert len(parsed.points) == 3
        assert [parsed.points[i] for i in range(3)] == [0,1000,2000]
        with pytest.raises(ValueError):
            custom_message_metadata(raw[:-1])


def test_log_tail_rejects_symlink_roots_and_directories(tmp_path):
    actual=tmp_path/'actual';actual.mkdir()
    nested=actual/'nested';nested.mkdir()
    (nested/'launch.log').write_text('')
    root=tmp_path/'symlink';root.symlink_to(actual, target_is_directory=True)
    spool, monitor, _ = make_monitor(tmp_path)
    assert RosLogTail(monitor, root)._candidates() == []
    secondary=tmp_path/'allowed';secondary.mkdir()
    (secondary/'linked').symlink_to(nested, target_is_directory=True)
    assert RosLogTail(monitor, secondary)._candidates() == []
    spool.close()


def test_routine_warning_startup_and_recovery_never_fill_spool(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    for index in range(100):
        monitor.observe_log('node', f'Waiting for optional device {index}', 30)
        monitor.observe_log('node', 'healthy startup', 20)
        monitor.emit('FAULT_MONITOR_STARTED', 'collector', '故障监测已启动', 'INFO')
        monitor.emit('DEVICE_DATA_RECOVERED', 'lidar', '数据已恢复', 'INFO')
    spool.add(event(code='ROS_WARNING', level='WARN'))
    assert not spool.batch()
    assert not monitor.recent and not monitor.templates
    spool.close()


@pytest.mark.parametrize('component,message,level,code', [
    ('fastlio', 'IMU timestamp gap 0.8 seconds', 30, 'SENSOR_TIME_GAP_REPORTED'),
    ('fastlio', 'lidar loop back, clear buffer', 20, 'SENSOR_TIME_ROLLBACK_REPORTED'),
    ('controller_server', 'Controller oscillation: repeated direction changes', 30, 'CONTROLLER_OSCILLATION'),
    ('navigation_odometry', 'Dropping invalid odometry', 30, 'LOCALIZATION_WARNING'),
    ('rtabmap', 'Localization lost due to insufficient matches', 30, 'LOCALIZATION_WARNING'),
    ('controller_server', 'Failed to make progress', 30, 'NAVIGATION_FAILURE'),
    ('collision_monitor', 'Obstacle detected; stopping', 20, 'OBSTACLE_STOP_REPORTED'),
    ('controller_server', 'Goal execution timed out', 30, 'NAVIGATION_TIMEOUT'),
    ('controller_server', '导航动作执行超时', 30, 'NAVIGATION_TIMEOUT'),
    ('localization_manager', '定位不稳定，停止导航', 30, 'LOCALIZATION_WARNING'),
    ('livox', '雷达时间同步失败', 30, 'SENSOR_TIME_SYNC_REPORTED'),
    ('livox', '雷达时间回退', 30, 'SENSOR_TIME_ROLLBACK_REPORTED'),
    ('device_driver', 'Hardware operation failed', 50, 'ROS_ERROR'),
])
def test_key_reported_failures_retained_with_raw_evidence(tmp_path, component, message, level, code):
    spool, monitor, _ = make_monitor(tmp_path)
    monitor.observe_log(component, message, level, 123.45)
    row = spool.batch()[0]
    assert row['code'] == code
    assert row['details']['raw_message'] == message
    assert row['details']['source_timestamp'] == 123.45
    assert row['details']['occurrences'] == 1
    if level == 50:
        assert row['level'] == 'FATAL'
    spool.close()


def test_network_loopback_and_ordinary_node_timeout_are_not_sensor_faults(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    monitor.observe_log('network', 'Configured loopback interface', 30)
    monitor.observe_log('optional_preview', 'Connection timeout while waiting', 30)
    monitor.observe_log('collision_monitor', 'Collision monitor is ready', 20)
    assert not spool.batch()
    spool.close()


def test_dynamic_sync_values_share_one_incident_and_keep_latest_evidence(tmp_path):
    spool, monitor, clock = make_monitor(tmp_path)
    for index in range(51):
        clock[0] += .01
        monitor.observe_log('fastlio', f'IMU and LiDAR not Synced, IMU time: {1000 + index}, lidar header time: {900 + index}', 20)
    assert len(spool.batch()) == 1
    clock[0] += 301
    monitor.tick()
    rows = spool.batch()
    assert len(rows) == 2
    assert all(row['code'] == 'SENSOR_TIME_SYNC_REPORTED' for row in rows)
    assert sum(row['details']['occurrences'] for row in rows) == 51
    assert '1050' in rows[-1]['details']['raw_message']
    assert rows[-1]['timestamp'] == rows[-1]['details']['last_suppressed_timestamp']
    assert rows[-1]['details']['first_suppressed_timestamp'] < rows[-1]['timestamp']
    monitor.flush_suppressed(force=True)
    assert len(spool.batch()) == 2
    spool.close()


def test_force_flush_retains_short_burst_once_on_clean_shutdown(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    for index in range(4):
        monitor.observe_log('controller_server', f'Goal {index} execution timed out', 40)
    monitor.flush_suppressed(force=True)
    assert len(spool.batch()) == 2
    assert sum(row['details']['occurrences'] for row in spool.batch()) == 4
    spool.close()


def test_diagnostic_warning_classifies_key_fault_and_omits_generic_warning(tmp_path):
    spool, monitor, _ = make_monitor(tmp_path)
    monitor.observe_diagnostics(NS(status=[
        NS(level=1, name='localization_manager', message='Localization lost', values=[], hardware_id='pose'),
        NS(level=1, name='optional_node', message='Waiting for data', values=[], hardware_id='unused'),
    ]))
    rows = spool.batch()
    assert len(rows) == 1 and rows[0]['code'] == 'LOCALIZATION_WARNING'
    assert rows[0]['details']['origin'] == '/diagnostics'
    assert rows[0]['details']['hardware_id'] == 'pose'
    spool.close()
