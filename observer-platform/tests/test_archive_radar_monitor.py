"""Read-only observation with fake procfs; no ROS, devices, systemd or network."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from collector.archive_capture_session import CaptureSession, datetime_iso
from collector.archive_core import atomic_json
from collector.archive_graph import UNIT
from collector.archive_radar_monitor import RadarObserver, observe_recording_graph
from collector import archive_radar_monitor as monitor


def process(root, pid, domain='0', *, executable='python3', ticks='10001'):
    path = root / str(pid)
    path.mkdir(parents=True, exist_ok=True)
    (path / 'cmdline').write_bytes(('/fixture/' + executable).encode() + b'\0')
    (path / 'stat').write_text(str(pid) + ' (' + executable + ') ' +
        ' '.join(['S'] + ['0'] * 18 + [ticks, '0']) + '\n')
    (path / 'environ').write_bytes(('ROS_DOMAIN_ID=' + domain + '\0SECRET=never-report-this\0').encode())


def driver(pid=101, domain='0', *, key='mid360', ticks='10001'):
    return dict(key=key, state='reused', pids=[pid], processStartTicks=ticks,
        dds=dict(domain=domain, rmw='rmw_fastrtps_cpp', localhostOnly=False,
                 discoveryServer='', namespace=''))


@pytest.fixture
def graph(tmp_path):
    proc = tmp_path / 'proc'
    process(proc, 201)
    process(proc, 101, executable='livox_ros_driver2_node')
    calls = []
    def runner(command, **options):
        calls.append(command)
        assert command == ['systemctl', '--user', 'show', UNIT, '--property=MainPID', '--value']
        assert options == dict(capture_output=True, text=True, timeout=2)
        return SimpleNamespace(returncode=0, stdout='201\n')
    return proc, runner, calls


def test_absent_source_does_not_even_query_recorder_service(graph):
    proc, runner, calls = graph
    rows, summary = observe_recording_graph(dict(devices=[dict(key='mid360', state='inactive')]),
                                           runner=runner, proc_root=proc)
    assert summary['state'] == 'waiting' and rows[0]['state'] == 'inactive'
    assert calls == []


@pytest.mark.parametrize('domain,helper_match,state', [('0', False, 'aligned'), ('17', True, 'mismatch')])
def test_actual_running_receiver_wins_over_stale_config_comparison(graph, domain, helper_match, state):
    proc, runner, calls = graph
    process(proc, 101, domain, executable='livox_ros_driver2_node')
    row = dict(driver(domain=domain), ddsCompatible=helper_match)
    original = json.dumps(row, sort_keys=True)
    rows, summary = observe_recording_graph(dict(devices=[row]), runner=runner, proc_root=proc)
    assert rows[0]['recordingGraph']['state'] == summary['state'] == state
    assert len(calls) == 1 and 'SECRET' not in json.dumps(summary)
    assert 'never-report-this' not in json.dumps(rows)
    assert json.dumps(row, sort_keys=True) == original


@pytest.mark.parametrize('fault', ['restarted', 'different_executable', 'graph_changed', 'pid_not_single', 'no_dds'])
def test_unverified_driver_cannot_be_reported_aligned_or_reconfigured(graph, fault):
    proc, runner, calls = graph
    row = driver()
    if fault == 'restarted':
        process(proc, 101, executable='livox_ros_driver2_node', ticks='99999')
    elif fault == 'different_executable':
        process(proc, 101, executable='other_process')
    elif fault == 'graph_changed':
        process(proc, 101, '19', executable='livox_ros_driver2_node')
    elif fault == 'pid_not_single':
        row['pids'] = [101, 102]
    else:
        row.pop('dds')
    rows, summary = observe_recording_graph(dict(devices=[row]), runner=runner, proc_root=proc)
    assert rows[0]['recordingGraph']['state'] == summary['state'] == 'unknown'
    assert len(calls) == 1


def test_each_source_keeps_its_own_graph_result(graph):
    proc, runner, _ = graph
    process(proc, 102, '8', executable='bluesea2_node')
    rows, summary = observe_recording_graph(dict(devices=[driver(), driver(102, '8', key='bluesea')]),
                                           runner=runner, proc_root=proc)
    assert [row['recordingGraph']['state'] for row in rows] == ['aligned', 'mismatch']
    assert summary['state'] == 'mismatch'


def test_inactive_source_does_not_inherit_another_sources_graph_mismatch(graph):
    proc, runner, _ = graph
    process(proc, 101, '8', executable='livox_ros_driver2_node')
    rows, summary = observe_recording_graph(dict(devices=[driver(domain='8'),
        dict(key='stl19p', state='inactive')]), runner=runner, proc_root=proc)
    assert summary['state'] == 'mismatch'
    assert rows[1]['recordingGraph']['state'] == 'unknown'


def test_receiver_restart_at_end_invalidates_every_preceding_row(graph, monkeypatch):
    proc, runner, _ = graph
    process(proc, 102, executable='bluesea2_node')
    identity, archive_checks = monitor._identity, []
    def restarted(pid, proc_root, executable=None):
        if pid == 201:
            archive_checks.append(pid)
            if len(archive_checks) == 5:
                process(proc, 201, ticks='20002')
        return identity(pid, proc_root, executable)
    monkeypatch.setattr(monitor, '_identity', restarted)
    rows, summary = observe_recording_graph(dict(devices=[driver(), driver(102, key='bluesea')]),
                                           runner=runner, proc_root=proc)
    assert summary['state'] == 'unknown'
    assert all(row['recordingGraph'] == dict(state='unknown', reason='recorder_process_changed') for row in rows)


def test_failed_receiver_lookup_preserves_source_availability_and_exposes_unknown(graph):
    proc, _, _ = graph
    rows, summary = observe_recording_graph(dict(devices=[driver()]), proc_root=proc,
        runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout='private error'))
    assert rows[0]['state'] == 'reused'
    assert rows[0]['recordingGraph']['state'] == summary['state'] == 'unknown'
    assert 'private error' not in json.dumps((rows, summary))


def test_short_capture_stops_observer_without_any_source_check():
    observer = RadarObserver({}, interval=1,
        inspector=lambda *_a, **_k: pytest.fail('short capture should use startup snapshot'))
    observer.start()
    assert observer.close(timeout=1)
    assert observer.snapshot() is None


def test_blocked_read_only_check_does_not_block_lease_renewal_or_release(tmp_path):
    entered, leave = threading.Event(), threading.Event()
    calls = []
    def inspector(values, *, cancelled):
        calls.append(dict(values))
        entered.set()
        assert leave.wait(3)
        return dict(readOnlySources=True, sourceControl=False, devices=[])
    observer = RadarObserver({}, interval=1, inspector=inspector,
        graph_checker=lambda report: (report['devices'], dict(state='waiting')))
    session = CaptureSession(tmp_path / 'control')
    session.claim()
    observer.start()
    try:
        assert entered.wait(2)
        before = time.monotonic()
        assert session.renew()
        session.release()
        assert observer.close(timeout=.05) is False
        assert time.monotonic() - before < .75
        assert session.read(session.gate)['enabled'] is False
        assert len(calls) == 1
    finally:
        leave.set()
        assert observer.close(timeout=2)
    assert observer.snapshot() is None  # A canceled check cannot publish late status.


def test_session_persists_observations_without_touching_sources(tmp_path):
    session = CaptureSession(tmp_path)
    session.claim()
    observation = dict(readOnlySources=True, sourceControl=False,
        checkedAt=time.time(), hosts=dict(ipc=dict(devices=[])), graph=dict(state='waiting'))
    closed = []
    session._radar_observer = SimpleNamespace(snapshot=lambda: observation,
        close=lambda: closed.append(True) or True)
    session.refresh_radar_observation()
    session.record_result()
    assert session.read(session.marker)['radarObservation'] == observation
    session.release()
    session.stop_radar_observer()
    assert closed == [True] and session._radar_observer is None
    assert session.read(session.gate)['enabled'] is False


def test_cleanup_retains_observer_handle_if_bounded_shutdown_is_not_confirmed(tmp_path):
    session = CaptureSession(tmp_path)
    observer = SimpleNamespace(snapshot=lambda: None, close=lambda: False)
    session._radar_observer = observer
    session.stop_radar_observer()
    assert session._radar_observer is observer


def test_graph_mismatch_warning_expires_and_does_not_change_recording_gate(tmp_path):
    now = [2_000_000_000.0]
    session = CaptureSession(tmp_path, clock=lambda: now[0])
    session.claim()
    now[0] += 20
    session.renew()
    atomic_json(tmp_path / 'status.json', dict(updatedAt=datetime_iso(now[0])))
    session.radar_observation = dict(checkedAt=now[0], graph=dict(state='mismatch'))
    assert any('DDS' in text for text in session.recording_warnings())
    assert session.read(session.gate)['enabled']
    now[0] += 31
    session.renew()
    assert not any('DDS' in text for text in session.recording_warnings())
