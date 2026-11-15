"""Per-device capture evidence; no ROS, hardware, services or network required."""
import json
from types import SimpleNamespace

import pytest

from collector import archive_capture_session as capture
from collector.archive_core import atomic_json


TOPICS = {
    topic: kind for _, _, _, required in capture.LIDAR_DEVICES for topic, kind in required
}


@pytest.fixture
def rig(tmp_path):
    now = [2_000_000_000.0]
    session = capture.CaptureSession(tmp_path/'control', clock=lambda: now[0])
    return session, now


def startup(**states):
    rows = [dict(key=key, state=state) for key, state in states.items()]
    return dict(ok=all(state in ('started', 'reused') for state in states.values()),
                hosts=dict(ipc=dict(devices=rows,
                    available=[key for key, state in states.items()
                               if state in ('started', 'reused', 'partial')])) )


def report(session, now, counts=None, *, overrides=None, active=None):
    topics = {topic: dict(type=kind, received=0, written=0, dropped=0,
                         dds_lost=0, state='WAITING_UPSTREAM', ageS=None, error=None, reasons={})
              for topic, kind in TOPICS.items()}
    for topic, count in (counts or {}).items():
        topics[topic].update(received=count, written=count,
                             state='AVAILABLE' if count else 'WAITING_UPSTREAM', ageS=0 if count else None)
    for topic, values in (overrides or {}).items():
        topics[topic].update(values)
    value = dict(updatedAt=capture.datetime_iso(now[0]),
                 ros=dict(topics=topics, gateError=None, tickError=None),
                 store=dict(active=active or {}))
    atomic_json(session.control/'status.json', value)
    return value


def start(session, now, states=None, initial=None):
    session.device_startup = startup(**(states or dict(mid360='reused')))
    report(session, now, initial)
    session.claim()
    session.video_sources.add('test-video')
    now[0] += 3


def devices(session):
    return {row['key']: row for row in session.lidar_diagnostics()}


@pytest.mark.parametrize('only,missing', [
    ('/livox/lidar', '/livox/imu'), ('/livox/imu', '/livox/lidar'),
    ('/scan', '/livox/lidar'), ('/bluesea_new/scan', '/livox/lidar'),
])
def test_one_topic_never_substitutes_for_mid360_pointcloud_and_imu(rig, only, missing):
    session, now = rig
    start(session, now)
    report(session, now, {only: 10})
    session.observe_sources()
    mid = devices(session)['mid360']
    assert mid['expected'] and not mid['complete']
    assert missing in mid['missingTopics']
    assert any(missing in value for value in session.missing_data())


def test_each_started_two_dimensional_radar_requires_its_own_topic(rig):
    session, now = rig
    start(session, now, dict(mid360='reused', stl19p='started', bluesea='started'))
    report(session, now, {'/livox/lidar': 3, '/livox/imu': 20, '/scan': 8})
    session.observe_sources()
    rows = devices(session)
    assert rows['mid360']['complete'] and rows['stl19p']['complete']
    assert not rows['bluesea']['complete']
    assert session.missing_data() == ['雷达 蓝海二维雷达 (/bluesea_new/scan)']
    now[0] += 3
    report(session, now, {'/livox/lidar': 5, '/livox/imu': 40, '/scan': 16, '/bluesea_new/scan': 2})
    session.observe_sources()
    assert session.missing_data() == []
    assert all(row['state'] == 'receiving' for row in devices(session).values())


@pytest.mark.parametrize('state', ['failed', 'skipped', 'inactive'])
def test_unconnected_source_has_its_own_reason_without_imposing_another_lidar(rig, state):
    session, now = rig
    start(session, now, dict(mid360='reused', stl19p=state))
    session.device_startup['hosts']['ipc']['devices'][1]['error'] = '固定串口未连接，未使用其他串口'
    report(session, now, {'/livox/lidar': 3, '/livox/imu': 20})
    session.observe_sources()
    rows = devices(session)
    assert session.missing_data() == []
    assert not rows['stl19p']['expected'] and rows['stl19p']['state'] == 'not_connected'
    assert rows['stl19p']['topics'][0]['reason'] == '固定串口未连接，未使用其他串口'
    assert session.device_startup['ok'] is False


def test_available_only_report_still_requires_mid360_both_topics(rig):
    session, now = rig
    start(session, now)
    session.device_startup = dict(ok=True, hosts=dict(ipc=dict(available=['mid360'], devices=[])))
    report(session, now, {'/scan': 5, '/livox/lidar': 2})
    session.observe_sources()
    assert session.missing_data() == ['雷达 MID360 (/livox/imu)']


@pytest.mark.parametrize('startup_value', [None, dict(ok=True, hosts={})])
def test_no_device_start_and_legacy_reports_enroll_only_observed_device(rig, startup_value):
    session, now = rig
    session.device_startup = startup_value
    report(session, now)
    session.claim()
    session.video_sources.add('test-video')
    now[0] += 3
    report(session, now, {'/scan': 2})
    session.observe_sources()
    assert session.missing_data() == []
    assert {key for key, row in devices(session).items() if row['expected']} == {'stl19p'}


@pytest.mark.parametrize('received_topic,missing_topic', [
    ('/livox/lidar', '/livox/imu'), ('/livox/imu', '/livox/lidar'),
])
def test_late_mid360_enrolls_both_required_topics_after_inactive_startup(
        rig, received_topic, missing_topic):
    session, now = rig
    start(session, now, dict(mid360='inactive', stl19p='inactive', bluesea='inactive'))
    report(session, now, {received_topic: 5})
    session.observe_sources()
    mid = devices(session)['mid360']
    assert mid['expected'] and not mid['complete']
    assert mid['missingTopics'] == [missing_topic]
    assert session.missing_data() == ['雷达 MID360 (' + missing_topic + ')']


def test_late_mid360_is_complete_only_after_pointcloud_and_imu_are_written(rig):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now, {'/livox/lidar': 5, '/livox/imu': 20})
    session.observe_sources()
    mid = devices(session)['mid360']
    assert mid['expected'] and mid['complete'] and mid['state'] == 'receiving'
    assert session.missing_data() == []


@pytest.mark.parametrize('fault,expected_state,reason', [
    ('import', 'error', 'subscription:ImportError'),
    ('disabled', 'disabled', 'recorder_subscription_disabled'),
    ('stale_report', 'status_unavailable', 'recorder_status_missing_or_stale'),
    ('received', 'not_written', 'received_but_not_written'),
])
def test_inactive_startup_preserves_current_recorder_diagnostics(rig, fault, expected_state, reason):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    values = (dict(error='subscription:ImportError') if fault == 'import' else
              dict(state='DISABLED') if fault == 'disabled' else
              dict(received=3) if fault == 'received' else {})
    value = report(session, now, overrides={'/livox/lidar': values})
    if fault == 'stale_report':
        value['updatedAt'] = capture.datetime_iso(now[0] - 60)
        atomic_json(session.control/'status.json', value)
    session.observe_sources()
    mid = devices(session)['mid360']
    assert mid['topics'][0]['state'] == expected_state
    assert mid['topics'][0]['reason'] == reason
    assert mid['expected'] is (fault == 'received')


def test_old_counter_totals_do_not_enroll_inactive_device(rig):
    session, now = rig
    initial = {'/livox/lidar': 100, '/livox/imu': 500}
    start(session, now, dict(mid360='inactive'), initial=initial)
    report(session, now, initial)
    session.observe_sources()
    mid = devices(session)['mid360']
    assert not mid['expected'] and not mid['complete']
    assert all(row['received'] == row['written'] == 0 for row in mid['topics'])
    assert session.missing_data() == ['雷达']


def radar_observation(now, state, *, graph='aligned'):
    return dict(checkedAt=now[0], readOnlySources=True,
        hosts=dict(ipc=dict(devices=[dict(key='mid360', state=state)])),
        graph=dict(state=graph, reason='test observation'))


def test_current_driver_availability_enrolls_device_without_losing_it_on_exit(rig):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now)
    session.radar_observation = radar_observation(now, 'reused')
    mid = devices(session)['mid360']
    assert mid['availability'] == 'reused' and mid['expected']
    now[0] += 1
    session.radar_observation = radar_observation(now, 'inactive')
    report(session, now)
    mid = devices(session)['mid360']
    assert mid['availability'] == 'inactive' and mid['expected']
    assert session.missing_data() == ['雷达 MID360 (/livox/lidar)', '雷达 MID360 (/livox/imu)']


def test_driver_exit_observation_does_not_cancel_startup_expectations(rig):
    session, now = rig
    start(session, now)
    report(session, now)
    session.radar_observation = radar_observation(now, 'inactive')
    mid = devices(session)['mid360']
    assert mid['expected'] and mid['availability'] == 'inactive'


@pytest.mark.parametrize('invalid', ['stale', 'before_session', 'not_read_only'])
def test_invalid_or_old_observation_cannot_enroll_an_inactive_device(rig, invalid):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now)
    session.radar_observation = radar_observation(now, 'reused', graph='mismatch')
    if invalid == 'stale':
        now[0] += 31
        report(session, now)
    elif invalid == 'before_session':
        session.radar_observation['checkedAt'] = session.capture_started_at - 1
    else:
        session.radar_observation['readOnlySources'] = False
    mid = devices(session)['mid360']
    assert mid['availability'] == 'inactive' and not mid['expected']
    assert all(row['state'] == 'not_connected' for row in mid['topics'])


def test_graph_mismatch_only_explains_waiting_topics_without_session_evidence(rig):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    session.radar_observation = radar_observation(now, 'reused', graph='mismatch')
    report(session, now, {'/livox/lidar': 2})
    session.observe_sources()
    mid = devices(session)['mid360']
    assert mid['topics'][0]['state'] == 'receiving'
    assert mid['topics'][1]['state'] == 'error'
    assert mid['topics'][1]['reason'] == 'recording_dds_graph_mismatch'
    report(session, now, {'/livox/lidar': 2},
           overrides={'/livox/imu': dict(error='subscription:ImportError')})
    assert devices(session)['mid360']['topics'][1]['reason'] == 'subscription:ImportError'


@pytest.mark.parametrize('device_state,summary_state,expected_state', [
    ('aligned', 'mismatch', 'waiting_upstream'),
    ('unknown', 'mismatch', 'waiting_upstream'),
    ('mismatch', 'aligned', 'error'),
])
def test_per_device_graph_diagnostic_takes_precedence_over_other_radars(
        rig, device_state, summary_state, expected_state):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now)
    session.radar_observation = radar_observation(now, 'reused', graph=summary_state)
    session.radar_observation['hosts']['ipc']['devices'][0]['recordingGraph'] = dict(state=device_state)
    assert all(row['state'] == expected_state for row in devices(session)['mid360']['topics'])


@pytest.mark.parametrize('availability', ['inactive', 'failed', 'skipped'])
def test_global_graph_mismatch_cannot_describe_an_unverified_device(rig, availability):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now)
    session.radar_observation = radar_observation(now, availability, graph='mismatch')
    mid = devices(session)['mid360']
    assert not mid['expected'] and mid['availability'] == availability
    assert all(row['state'] == 'not_connected' for row in mid['topics'])


def test_global_graph_mismatch_cannot_describe_an_unobserved_device(rig):
    session, now = rig
    start(session, now)
    report(session, now)
    session.radar_observation = radar_observation(now, 'reused', graph='mismatch')
    session.radar_observation['hosts']['ipc']['devices'][0]['key'] = 'stl19p'
    rows = devices(session)
    assert all(row['state'] == 'waiting_upstream' for row in rows['mid360']['topics'])
    assert rows['stl19p']['topics'][0]['reason'] == 'recording_dds_graph_mismatch'


def test_read_only_status_uses_marker_observation_and_preserves_seen_expectations(rig):
    session, now = rig
    start(session, now, dict(mid360='inactive'))
    report(session, now)
    marker = session.read(session.marker)
    marker['radarObservation'] = radar_observation(now, 'inactive', graph='mismatch')
    marker['lidarDevices'][0]['expected'] = True
    readonly = capture.CaptureSession(session.control, clock=lambda: now[0],
        runner=lambda *_args, **_kwargs: pytest.fail('diagnostics called a service'))
    mid = readonly.lidar_diagnostics(marker=marker)[0]
    assert mid['expected'] and mid['availability'] == 'inactive'
    assert all(row['reason'] == 'no_this_session_messages' for row in mid['topics'])


def test_old_counter_totals_and_remote_same_topic_cannot_satisfy_ipc_device(rig):
    session, now = rig
    start(session, now, initial={'/livox/lidar': 100, '/livox/imu': 500})
    report(session, now, {'/livox/lidar': 100, '/livox/imu': 500})
    atomic_json(session.control/'source.json', dict(updatedAt=capture.datetime_iso(now[0]),
        connected=True, gateAcknowledged=True, gateEnabled=True,
        gateRevision=session.read(session.gate)['updatedAt'],
        recorder=dict(updatedAt=capture.datetime_iso(now[0]),
                      ros=dict(topics={'/livox/lidar': dict(written=900), '/livox/imu': dict(written=901)}))))
    session.observe_sources()
    assert devices(session)['mid360']['missingTopics'] == ['/livox/lidar', '/livox/imu']
    assert len(session.missing_data()) == 2


def test_counter_reset_proves_only_new_positive_current_session_counts(rig):
    session, now = rig
    start(session, now, initial={'/livox/lidar': 100, '/livox/imu': 500})
    report(session, now, {'/livox/lidar': 2, '/livox/imu': 10})
    session.observe_sources()
    assert session.missing_data() == []
    assert [row['written'] for row in devices(session)['mid360']['topics']] == [2, 10]
    assert all(row['countsAreLowerBounds'] for row in devices(session)['mid360']['topics'])


@pytest.mark.parametrize('current_segment', [False, True])
def test_late_first_report_needs_current_segment_and_each_topic_count(rig, current_segment):
    session, now = rig
    session.device_startup = startup(mid360='reused')
    session.claim()
    session.video_sources.add('test-video')
    now[0] += 3
    active = {'雷达MID360': dict(startedAt=capture.datetime_iso(now[0] - (0 if current_segment else 60)),
        messages=12, topics={'/livox/lidar': dict(count=2), '/livox/imu': dict(count=10)})}
    report(session, now, {'/livox/lidar': 2, '/livox/imu': 10}, active=active)
    session.observe_sources()
    assert devices(session)['mid360']['complete'] is current_segment


@pytest.mark.parametrize('fault,reason', [
    ('subscription', 'subscription:ImportError'),
    ('qos', 'subscription:IncompatibleQoS'),
    ('type', 'subscription_type_mismatch'),
    ('not_selected', 'sensor_not_selected'),
    ('writer', 'received_but_not_written'),
    ('stale_report', 'recorder_status_missing_or_stale'),
])
def test_missing_topic_exposes_existing_subscription_writer_or_status_reason(rig, fault, reason):
    session, now = rig
    start(session, now)
    values = dict(received=3, error='subscription:ImportError') if fault == 'subscription' else (
        dict(error='subscription:IncompatibleQoS') if fault == 'qos' else
        dict(type='sensor_msgs/msg/PointCloud2') if fault == 'type' else
        dict(received=3, dropped=3, reasons={'store_rejected': 3}) if fault == 'writer' else {})
    value = report(session, now, {'/livox/imu': 10}, overrides={'/livox/lidar': values})
    if fault == 'not_selected':
        value['ros']['topics'].pop('/livox/lidar')
        value['recordingPolicy'] = dict(rosSensors=['二维雷达scan'])
    if fault == 'stale_report':
        value['updatedAt'] = capture.datetime_iso(now[0]-60)
    atomic_json(session.control/'status.json', value)
    session.observe_sources()
    row = devices(session)['mid360']['topics'][0]
    assert row['written'] == 0 and row['state'] != 'receiving'
    assert row['reason'] == reason
    assert any('/livox/lidar' in item for item in session.missing_data())


def test_progress_never_calls_partial_lidar_complete_and_persists_diagnostics(rig, capsys):
    session, now = rig
    start(session, now)
    report(session, now, {'/livox/lidar': 2})
    session.observe_sources()
    session.show_progress()
    output = capsys.readouterr().out
    assert '雷达：本次尚有话题未确认写盘' in output
    assert '/livox/imu 本轮写盘至少 0 条' in output
    marker = session.read(session.marker)
    assert marker['lidarDevices'][0]['missingTopics'] == ['/livox/imu']


def test_status_reads_current_session_delta_without_mutating_marker_or_services(rig, monkeypatch, capsys):
    session, now = rig
    start(session, now, initial={'/livox/lidar': 100, '/livox/imu': 500})
    report(session, now, {'/livox/lidar': 102, '/livox/imu': 510})
    before = session.marker.read_bytes()
    readonly = capture.CaptureSession(session.control, clock=lambda: now[0],
        runner=lambda *_args, **_kwargs: pytest.fail('status called a service'))
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: readonly)
    monkeypatch.setattr(capture.Path, 'home', lambda: session.control.parent)
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(session.control))
    assert capture.main(['--status']) == 0
    result = json.loads(capsys.readouterr().out)
    assert [row['written'] for row in result['lidarDevices'][0]['topics']] == [2, 10]
    assert result['lidarDevices'][0]['complete'] is True
    assert session.marker.read_bytes() == before


def test_stopped_status_cannot_enroll_new_session_or_lose_final_proof(rig):
    session, now = rig
    start(session, now)
    report(session, now, {'/livox/lidar': 3})
    session.observe_sources()
    session.release()
    session.record_result()
    now[0] += 3
    report(session, now, {'/livox/lidar': 30, '/livox/imu': 300})
    readonly = capture.CaptureSession(session.control, clock=lambda: now[0])
    mid = readonly.lidar_diagnostics()[0]
    assert not mid['complete'] and mid['missingTopics'] == ['/livox/imu']
    assert mid['topics'][0]['written'] == 3 and mid['topics'][1]['written'] == 0


@pytest.mark.parametrize('changed', ['expired', 'replacement', 'closed'])
def test_abandoned_recording_marker_cannot_claim_later_counter_increases(rig, changed):
    session, now = rig
    start(session, now)
    lease = session.read(session.lease)
    if changed == 'expired':
        now[0] += capture.LEASE_SECONDS + 1
    elif changed == 'replacement':
        lease['desktopSession'] = 'another-session'
    else:
        lease['enabled'] = False
    atomic_json(session.lease, lease)
    report(session, now, {'/livox/lidar': 3, '/livox/imu': 30})
    readonly = capture.CaptureSession(session.control, clock=lambda: now[0])
    mid = readonly.lidar_diagnostics()[0]
    assert not mid['complete']
    assert all(row['reason'] == 'recording_lease_not_active' for row in mid['topics'])


def test_existing_writes_do_not_claim_current_reception_after_upstream_becomes_stale(rig):
    session, now = rig
    start(session, now)
    report(session, now, {'/livox/lidar': 3, '/livox/imu': 12})
    session.observe_sources()
    report(session, now, {'/livox/lidar': 3, '/livox/imu': 12},
           overrides={'/livox/lidar': dict(state='STALE', ageS=8)})
    mid = devices(session)['mid360']
    assert mid['complete'] is True and mid['state'] != 'receiving'
    assert mid['topics'][0]['state'] == 'stale'


def test_dds_alignment_is_after_device_preparation_before_claim_and_is_recorded(rig, monkeypatch):
    from collector import archive_graph
    session, now = rig
    calls = []
    summary = startup(mid360='reused')
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_kwargs: calls.append('devices') or summary)
    def align(values, control, report, *, runner):
        calls.append('align')
        assert report is summary and control == session.control and runner is session.runner
        assert not session.gate.exists()
        assert values['ARCHIVE_ENV_FILE'] == '/isolated/archive.env'
        return dict(state='aligned', driver='mid360')
    monkeypatch.setattr(archive_graph, 'ensure_recording_dds', align)
    session.ensure_devices(dict(ARCHIVE_ENV_FILE='/isolated/archive.env'), cancelled=lambda: False)
    session.claim()
    assert calls == ['devices', 'align']
    assert session.read(session.marker)['recordingDds'] == dict(state='aligned', driver='mid360')


@pytest.mark.parametrize('failure', ['graph', 'filesystem', 'value', 'subprocess'])
def test_dds_alignment_failure_is_capture_error_and_never_claims(rig, monkeypatch, failure):
    from collector import archive_graph
    session, _now = rig
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_kwargs: startup(mid360='reused'))
    def fail(*_args, **_kwargs):
        error = {'graph': archive_graph.RecordingGraphError, 'filesystem': FileNotFoundError,
                 'value': ValueError, 'subprocess': capture.subprocess.SubprocessError}[failure]
        raise error('archive not idle')
    monkeypatch.setattr(archive_graph, 'ensure_recording_dds', fail)
    with pytest.raises(capture.CaptureError, match='DDS.*archive not idle'):
        session.ensure_devices({}, cancelled=lambda: False)
    assert not session.gate.exists() and not session.marker.exists()


def test_cancellation_after_device_helper_skips_dds_reconfiguration(rig, monkeypatch):
    from collector import archive_graph
    session, _now = rig
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_kwargs: startup(mid360='reused'))
    monkeypatch.setattr(archive_graph, 'ensure_recording_dds',
        lambda *_args, **_kwargs: pytest.fail('cancelled startup aligned DDS'))
    session.ensure_devices({}, cancelled=lambda: True)
    assert session.recording_dds is None and not session.gate.exists()


@pytest.mark.parametrize('imu,expected', [(0, 3), (10, 0)])
def test_main_requires_mid360_imu_and_pointcloud_before_success(rig, monkeypatch, capsys, imu, expected):
    from collector import archive_graph
    session, now = rig
    report(session, now)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.Path, 'home', lambda: session.control.parent)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(session, 'ensure_recorders', lambda: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _seconds: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _seconds: True)
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_kwargs: startup(mid360='reused'))
    monkeypatch.setattr(capture, 'stop_capture_cameras', lambda *_args, **_kwargs: dict(ok=True, devices=[]))
    monkeypatch.setattr(archive_graph, 'ensure_recording_dds', lambda *_args, **_kwargs: dict(state='unchanged'))
    monkeypatch.setattr(capture.time, 'monotonic', lambda: now[0])
    def sleep(seconds):
        now[0] += seconds
        report(session, now, {'/livox/lidar': 2, '/livox/imu': imu})
        session.video_sources.add('test-video')
    monkeypatch.setattr(capture.time, 'sleep', sleep)
    assert capture.main(['--start-cameras', '--duration', '.1']) == expected
    result = session.read(session.marker)
    assert result['lidarDevices'][0]['complete'] is bool(imu)
    assert result['missingData'] == ([] if imu else ['雷达 MID360 (/livox/imu)'])
    assert ('/livox/imu' in capsys.readouterr().err) is (not bool(imu))
