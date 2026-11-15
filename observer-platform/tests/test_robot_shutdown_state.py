"""Pure status decisions: no SSH, systemd, ROS, or shutdown calls."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from collector.robot_shutdown_state import (CAMERA_QUEUES, ROS_QUEUES,
    _TERMINAL_DERIVATIVE_ERRORS, check_archive_state, check_camera_state)


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def archive():
    return dict(updatedAt=iso(100), ros=dict(active=False, workerAlive=False, topics={},
        **dict.fromkeys(ROS_QUEUES, 0)), store=dict(active={}, errors={}),
        video=dict(pendingSegments=0, running=False, errors={}))


def camera():
    return dict(updatedAt=iso(100), sink=dict(enabled=False, writerRunning=False,
        **dict.fromkeys(CAMERA_QUEUES, 0), errors={}), store=dict(active={}, errors={}))


def check(report, **kwargs):
    return check_archive_state(report, **dict(now=103, since=99, **kwargs))


def check_camera(report, **kwargs):
    values = dict(now=103, boot_time=90, service_active=False, process_count=0)
    values.update(kwargs)
    return check_camera_state(report, **values)


def terminal(report):
    report['video'] = dict(pendingSegments=2, terminalPendingSegments=2, running=False,
        errors={'bag:old/one': 'bag_metadata_or_message_count_invalid',
                'video:old/two': 'video_source_file_invalid'})
    report['store']['videoPendingSegments'] = 2
    report['store']['videoFinalizing'] = False
    report['store']['videoTerminalPending'] = True
    report['store']['errors']['videoFinalization'] = 'original retained'
    return report


def test_archive_accepts_fresh_locally_sealed_without_upload_requirement():
    report = archive()
    report['upload'] = dict(pendingSegments=14, lastError='network_offline')
    original = deepcopy(report)
    result = check(report)
    assert result == dict(state='READY', reason='archive_locally_sealed',
                          warnings=['archive_upload_deferred'])
    assert report == original


@pytest.mark.parametrize('stamp,state,reason', [
    (99, 'READY', 'archive_locally_sealed'),
    (95, 'WAIT', 'archive_status_stale'),
    (98.999, 'WAIT', 'archive_status_stale'),
    (103.001, 'ERROR', 'archive_timestamp_future'),
    ('2001-01-01T00:00:00', 'ERROR', 'archive_timestamp_invalid'),
    ('bad', 'ERROR', 'archive_timestamp_invalid'),
    (None, 'ERROR', 'archive_timestamp_invalid'),
])
def test_archive_timestamp_bounds(stamp, state, reason):
    report = archive()
    report['updatedAt'] = iso(stamp) if type(stamp) in (int, float) else stamp
    assert check(report)['state'] == state
    assert check(report)['reason'] == reason


def test_exact_eight_second_archive_observation_is_valid():
    assert check_archive_state(archive(), now=108, since=100)['state'] == 'READY'
    assert check_archive_state(archive(), now=108.001, since=100)['state'] == 'WAIT'


@pytest.mark.parametrize('field', ROS_QUEUES)
@pytest.mark.parametrize('value,state', [(0, 'READY'), (1, 'WAIT'), (False, 'ERROR'),
    (0.0, 'ERROR'), (-1, 'ERROR'), (None, 'ERROR'), ('0', 'ERROR')])
def test_ros_queues_require_nonnegative_integers(field, value, state):
    report = archive()
    report['ros'][field] = value
    assert check(report)['state'] == state


@pytest.mark.parametrize('part,field,value,state', [
    ('ros', 'active', True, 'WAIT'), ('ros', 'active', None, 'ERROR'),
    ('ros', 'active', 0, 'ERROR'), ('ros', 'tickError', 'I/O', 'ERROR'),
    ('ros', 'gateError', 'Invalid', 'ERROR'), ('store', 'active', {'lidar': {}}, 'WAIT'),
    ('store', 'active', [], 'ERROR'), ('store', 'errors', [], 'ERROR'),
    ('store', 'errors', {'storage': 'full'}, 'ERROR'),
    ('store', 'errors', {'lidar': 'write failed'}, 'ERROR'),
])
def test_active_and_storage_failures(part, field, value, state):
    report = archive()
    report[part][field] = value
    assert check(report)['state'] == state


def test_historical_errors_are_warnings_but_current_errors_still_block():
    report = archive()
    report['store']['errors'] = dict(recovery='old retained', sourceTransfer='offline')
    assert check(report)['state'] == 'READY'
    assert len(check(report)['warnings']) == 2
    report['store']['errors']['resourceMonitor'] = 'timeout'
    assert check(report)['state'] == 'ERROR'


def test_only_explicit_terminal_derivative_backlog_can_be_deferred():
    report = terminal(archive())
    assert check(report)['state'] == 'READY'
    assert check(report)['warnings'] == ['archive_terminal_derivatives_preserved']
    report['video']['terminalPendingSegments'] = 1
    assert check(report)['reason'] == 'archive_derivative_pending'
    report['video']['terminalPendingSegments'] = 2
    report['video']['errors']['worker:bag'] = 'bag_source_file_invalid'
    assert check(report)['state'] == 'WAIT'
    report['video']['errors'] = {}
    assert check(report)['state'] == 'WAIT'


@pytest.mark.parametrize('value', [True, -1, 0.0, None, '2'])
def test_malformed_derivative_counts_never_confirm_closure(value):
    report = terminal(archive())
    report['video']['pendingSegments'] = value
    assert check(report)['state'] == 'ERROR'


def test_summary_terminal_flag_without_details_cannot_hide_pending_source():
    report = terminal(archive())
    del report['video']
    assert check(report)['state'] == 'WAIT'


def test_raw_queue_failure_takes_precedence_over_derivative_state():
    report = terminal(archive())
    report['ros']['inFlightMessages'] = 1
    assert check(report)['reason'] == 'archive_raw_unsealed'


def test_stopped_requires_final_report_and_dead_writer():
    report = archive()
    assert check(report, stopped=True)['state'] == 'WAIT'
    report['state'] = 'stopped'
    assert check(report, stopped=True)['state'] == 'READY'
    report['ros']['workerAlive'] = True
    assert check(report, stopped=True)['state'] == 'ERROR'
    report['ros']['workerAlive'] = False
    report['state'] = 'stop_failed'
    assert check(report, stopped=True)['reason'] == 'archive_stop_failed'


def test_real_finish_archive_terminal_backlog_is_stopped_with_warning():
    from collector.archive_main import finish_archive, TERMINAL_DERIVATIVE_ERRORS
    assert _TERMINAL_DERIVATIVE_ERRORS == TERMINAL_DERIVATIVE_ERRORS
    raw = archive()
    final = terminal(archive())
    # Actual store.snapshot lacks the added status_with_transfer summary error.
    raw['store']['errors'] = {'recovery': 'old retained'}
    class Adapter:
        def close(self, timeout): return True
        def snapshot(self): return raw['ros']
    class Store:
        def close(self): pass
        def snapshot(self): return raw['store']
    class Video:
        closed = False
        def refresh_pending(self): pass
        def snapshot(self): return final['video']
        def close(self): self.closed = True
        def drain(self, timeout): raise AssertionError('terminal backlog must not drain')
    video = Video()
    report = finish_archive(Adapter(), Store(), video)
    assert report['state'] == 'stopped' and video.closed
    assert report['warnings']['videoFinalize'] == 'DeferredTerminalSource'
    report['updatedAt'] = iso(100)
    assert check(report, stopped=True)['state'] == 'READY'


@pytest.mark.parametrize('report,state', [(None, 'WAIT'), ({}, 'WAIT'), ([], 'ERROR'),
    (False, 'ERROR'), ('bad', 'ERROR')])
def test_archive_missing_and_malformed_reports(report, state):
    assert check(report)['state'] == state


@pytest.mark.parametrize('value', [True, 2, '1', None])
def test_explicit_incompatible_schema_rejected(value):
    report = archive()
    report['schemaVersion'] = value
    assert check(report)['state'] == 'ERROR'
    report = camera()
    report['schemaVersion'] = value
    assert check_camera(report)['state'] == 'ERROR'


@pytest.mark.parametrize('now,since', [(float('inf'), 99), (103, float('nan')),
    (103, True), (False, 99), (103, 104), (103, -1)])
def test_archive_clock_invalid(now, since):
    assert check_archive_state(archive(), now=now, since=since)['state'] == 'ERROR'


def test_oversized_integer_clock_never_raises():
    assert check_archive_state(archive(), now=10**10000, since=99)['state'] == 'ERROR'
    assert check_camera(camera(), boot_time=10**10000)['state'] == 'ERROR'


def test_unknown_archive_state_never_confirms_shutdown():
    report = archive()
    report['state'] = 'corrupt'
    assert check(report)['reason'] == 'archive_state_invalid'


def test_current_boot_closed_camera_status_need_not_keep_heartbeating():
    report = camera()
    report['updatedAt'] = iso(91)
    original = deepcopy(report)
    assert check_camera(report)['state'] == 'READY'
    assert report == original


@pytest.mark.parametrize('field', CAMERA_QUEUES)
@pytest.mark.parametrize('value,state', [(0, 'READY'), (1, 'WAIT'), (False, 'ERROR'),
    (0.0, 'ERROR'), (-1, 'ERROR'), (None, 'ERROR')])
def test_camera_queue_exact_types(field, value, state):
    report = camera()
    report['sink'][field] = value
    assert check_camera(report)['state'] == state


@pytest.mark.parametrize('kwargs,state', [
    ({'service_active': True}, 'WAIT'), ({'process_count': 1}, 'WAIT'),
    ({'service_active': 0}, 'ERROR'), ({'process_count': False}, 'ERROR'),
    ({'process_count': -1}, 'ERROR'), ({'process_count': None}, 'ERROR'),
    ({'now': float('nan')}, 'ERROR'), ({'boot_time': 104}, 'ERROR')])
def test_camera_independent_activity_probe(kwargs, state):
    assert check_camera(camera(), **kwargs)['state'] == state


def old_failed_camera():
    report = camera()
    report.update(updatedAt=iso(89), state='stop_failed', store=None, shutdownError='RuntimeError')
    return report


def test_previous_boot_legacy_failure_is_preserved_and_never_claimed_complete():
    report = old_failed_camera()
    original = deepcopy(report)
    assert check_camera(report) == dict(state='READY',
        reason='camera_previous_boot_writer_absent',
        warnings=['camera_previous_boot_capture_unconfirmed'])
    assert report == original


@pytest.mark.parametrize('part,field,value', [
    ('sink', 'enabled', True), ('sink', 'writerRunning', True),
    ('sink', 'queueFrames', 1), ('sink', 'queueBytes', 1),
    ('sink', 'inFlightFrames', 1), ('sink', 'errors', {'writer': 'OSError'}),
    ('sink', 'queueFrames', False), ('sink', 'errors', []),
    (None, 'store', {}), (None, 'state', 'stopped'),
    (None, 'errors', {'shutdown': 'error'}), (None, 'updatedAt', iso(90)),
])
def test_previous_boot_exception_is_narrow(part, field, value):
    report = old_failed_camera()
    (report[part] if part else report)[field] = value
    assert check_camera(report)['state'] != 'READY'


@pytest.mark.parametrize('change,state,reason', [
    ({'updatedAt': iso(104)}, 'ERROR', 'camera_timestamp_future'),
    ({'updatedAt': 'bad'}, 'ERROR', 'camera_timestamp_invalid'),
    ({'state': 'stop_failed'}, 'ERROR', 'camera_stop_failed'),
    ({'store': None}, 'ERROR', 'camera_store_invalid'),
    ({'store': {'active': {}, 'errors': {'storage': 'full'}}}, 'ERROR', 'camera_storage_errors'),
    ({'store': {'active': {'camera': {}}, 'errors': {}}}, 'WAIT', 'camera_segments_unsealed'),
])
def test_current_camera_storage_and_failure(change, state, reason):
    report = camera()
    report.update(change)
    result = check_camera(report)
    assert (result['state'], result['reason']) == (state, reason)


def test_camera_historical_recovery_is_warning():
    report = camera()
    report['store']['errors']['recovery'] = 'old retained'
    report['captureComplete'] = False
    result = check_camera(report)
    assert result['state'] == 'READY'
    assert set(result['warnings']) == {'camera_historical_recovery_preserved', 'camera_capture_had_warnings'}
