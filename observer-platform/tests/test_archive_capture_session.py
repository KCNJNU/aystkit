"""Desktop capture ownership checks with all device startup isolated."""
import json
import signal
from types import SimpleNamespace

import pytest

from collector import archive_capture_session as capture
from collector.archive_core import atomic_json


@pytest.fixture(autouse=True)
def isolate_sensor_startup(monkeypatch):
    # Lifecycle tests must not execute hardware helpers. Dedicated startup
    # tests below verify the default startup and the opt-out independently.
    monkeypatch.setattr(capture, 'start_capture_devices',
        lambda _values, **_options: dict(ok=True, hosts={}, incompleteDevices=[]))
    monkeypatch.setattr(capture, 'inspect_capture_radar',
        lambda _values, **_options: dict(ok=True, role='ipc', recordingOnly=True,
            readOnlySources=True, sourceControl=False, devices=[]))
    monkeypatch.setattr(capture, 'stop_capture_cameras',
        lambda _values, **_options: dict(ok=True, devices=[], errors=[]))


class FakeClock:
    def __init__(self):
        self.now = 2_000_000_000.0
        self.elapsed = 0.0

    def time(self):
        return self.now + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


@pytest.fixture
def clock(monkeypatch):
    result = FakeClock()
    monkeypatch.setattr(capture.time, 'time', result.time)
    monkeypatch.setattr(capture.time, 'monotonic', result.monotonic)
    monkeypatch.setattr(capture.time, 'sleep', result.sleep)
    return result


class FakeRunner:
    def __init__(self, *, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, command, **options):
        self.calls.append((command, options))
        return SimpleNamespace(returncode=int(command[-1] == self.fail))


def stopped_ros():
    return dict(active=False, queueMessages=0, queueBytes=0, inFlightMessages=0,
                subscriptionCleanupPending=0)


def sealed_status(control, clock):
    """Write current, affirmative local/remote shutdown evidence."""
    stamp = capture.datetime_iso(clock.time())
    atomic_json(control/'status.json', dict(updatedAt=stamp,
        store=dict(active={}, errors={}), ros=stopped_ros()))
    remote = dict(updatedAt=stamp, store=dict(active={}, errors={}), ros=stopped_ros())
    camera = dict(updatedAt=stamp, state='running', store=dict(active={}, errors={}),
                  sink=dict(enabled=False, queueFrames=0, queueBytes=0, inFlightFrames=0,
                            writerRunning=False, errors={}))
    atomic_json(control/'source.json', dict(updatedAt=stamp, connected=True,
        gateAcknowledged=True, gateEnabled=False, recorder=remote, camera=camera))


def test_helpers_only_start_two_archive_units_without_touching_algorithm_owners(tmp_path):
    runner = FakeRunner()
    session = capture.CaptureSession(tmp_path, runner=runner)
    session.ensure_recorders()
    assert [command for command, _ in runner.calls] == [
        ['systemctl', '--user', 'start', 'robot-native-archive.service'],
        ['systemctl', '--user', 'start', 'robot-native-archive-source-link.service'],
    ]
    assert all(options['timeout'] <= 25 for _, options in runner.calls)
    assert not (tmp_path/'garage.json').exists()


def test_helper_failure_does_not_enable_gate(tmp_path):
    runner = FakeRunner(fail=capture.RECORDER_UNITS[1])
    session = capture.CaptureSession(tmp_path, runner=runner)
    with pytest.raises(capture.CaptureError):
        session.ensure_recorders()
    assert not session.gate.exists() and not session.marker.exists()


def test_existing_web_capture_is_not_claimed_or_changed(tmp_path, clock):
    gate = dict(schemaVersion=1, scope='garage', enabled=True,
                updatedAt=capture.datetime_iso(clock.time()), expiresAt=None, updatedById='web-user')
    atomic_json(tmp_path/'garage.json', gate)
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    with pytest.raises(capture.CaptureError):
        session.claim()
    assert session.read(session.web_gate) == gate
    assert not session.gate.exists()
    assert not session.marker.exists()


def test_lease_renews_and_release_never_changes_web_choice(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    first = session.read(session.gate)
    assert first['desktopSession'] == session.token
    assert first['expiresAt'] == clock.time() + capture.LEASE_SECONDS
    clock.sleep(3)
    assert session.renew()
    assert session.read(session.gate)['expiresAt'] == clock.time() + capture.LEASE_SECONDS
    assert session.read(session.gate)['updatedAt'] == first['updatedAt']
    web = dict(schemaVersion=1, scope='garage', enabled=True, expiresAt=None,
               updatedAt=capture.datetime_iso(clock.time()), updatedById='web-user')
    atomic_json(session.web_gate, web)
    web_bytes = session.web_gate.read_bytes()
    assert session.renew()
    session.release()
    assert session.web_gate.read_bytes() == web_bytes
    assert session.read(session.gate)['enabled'] is False
    assert session.read(session.marker)['state'] == 'stopped'


def test_ten_minute_wan_outage_keeps_local_lease_and_short_crash_expiry(tmp_path, clock, monkeypatch):
    import socket
    from collector.archive_core import GarageGate
    def no_wan(*args, **kwargs):
        raise OSError('WAN disconnected')
    monkeypatch.setattr(socket, 'create_connection', no_wan)
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    gate = GarageGate(session.web_gate)
    revision = gate.snapshot()['updatedAt']
    for _ in range(200):
        clock.sleep(3)
        assert session.renew()
        assert gate.enabled()
        assert gate.snapshot()['updatedAt'] == revision
    assert clock.elapsed == 600
    # WAN recovery is independent of capture. A dead desktop owner still
    # expires promptly instead of leaving an unwanted ten-minute recording.
    clock.sleep(capture.LEASE_SECONDS + 1)
    assert not gate.enabled()
    session.release()


def test_closed_session_restores_closed_choice_and_drops_lease(tmp_path, clock):
    previous = dict(schemaVersion=1, scope='garage', enabled=False, updatedById='web-user', expiresAt=None)
    atomic_json(tmp_path/'garage.json', previous)
    previous_bytes = (tmp_path/'garage.json').read_bytes()
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    session.release()
    gate = session.read(session.gate)
    assert gate['enabled'] is False and gate['expiresAt'] is None
    assert 'desktopSession' not in gate
    assert session.web_gate.read_bytes() == previous_bytes
    assert session.read(session.marker)['state'] == 'stopped'


def test_capture_lock_rejects_second_session_without_gate_changes(tmp_path):
    first, second = capture.CaptureSession(tmp_path), capture.CaptureSession(tmp_path)
    with first.lock():
        with pytest.raises(capture.CaptureError):
            with second.lock():
                pytest.fail('second owner acquired the capture lock')
    assert not first.gate.exists()


def test_noninteractive_capture_without_duration_fails_before_any_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(tmp_path/'control'))
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.CaptureSession, 'ensure_recorders',
                        lambda _: pytest.fail('noninteractive capture started a helper'))
    with pytest.raises(SystemExit) as error:
        capture.main([])
    assert error.value.code == 2
    assert not (tmp_path/'control').exists()


def test_stop_signals_only_the_recorded_pid_with_matching_start_time(tmp_path, monkeypatch):
    session = capture.CaptureSession(tmp_path)
    token = 'f' * 32
    atomic_json(session.marker, dict(state='recording', pid=43210, processStart='start-7', token=token,
                                   bootId='current-boot'))
    monkeypatch.setattr(capture, 'boot_identity', lambda: 'current-boot')
    monkeypatch.setattr(capture, 'process_identity', lambda pid: 'start-7' if pid == 43210 else None)
    signals = []
    def stop_and_confirm(pid, value):
        signals.append((pid, value))
        atomic_json(session.marker, dict(session.read(session.marker), state='stopped',
                                       cameraShutdown=dict(ok=True)))
        atomic_json(session.control/'session-ends'/(token+'.json'),
                    dict(sessionId=token, state='sealed', captureIncomplete=False))
    monkeypatch.setattr(capture.os, 'kill', stop_and_confirm)
    assert session.stop() == 0
    assert signals == [(43210, signal.SIGTERM)]
    assert not session.gate.exists()


@pytest.mark.parametrize('marker', [
    {}, dict(state='stopped', pid=43210, processStart='old'),
    dict(state='recording', pid=43210, processStart='old'),
    dict(state='recording', pid=43210, processStart=None),
])
def test_stop_never_signals_absent_finished_or_reused_pid(tmp_path, monkeypatch, marker):
    session = capture.CaptureSession(tmp_path)
    atomic_json(session.marker, marker)
    monkeypatch.setattr(capture, 'process_identity', lambda _: 'new')
    monkeypatch.setattr(capture.os, 'kill', lambda *_: pytest.fail('signalled unrelated process'))
    assert session.stop() == 0


@pytest.mark.parametrize('marker_boot', [None, 'previous-boot'])
def test_stop_never_signals_a_stale_marker_from_another_boot(tmp_path, monkeypatch, marker_boot):
    session = capture.CaptureSession(tmp_path)
    atomic_json(session.marker, dict(state='recording', pid=43210, processStart='start-7',
                                    token='fixture', bootId=marker_boot))
    monkeypatch.setattr(capture, 'boot_identity', lambda: 'current-boot')
    monkeypatch.setattr(capture, 'process_identity', lambda _: 'start-7')
    monkeypatch.setattr(capture.os, 'kill', lambda *_: pytest.fail('signalled stale marker process'))
    assert session.stop() == 0


def test_wait_sealed_accepts_fresh_zero_queues_on_both_hosts(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    sealed_status(tmp_path, clock)
    assert session.wait_sealed(seconds=1)


@pytest.mark.parametrize('enabled', [True, None, 0, 'false'])
def test_wait_sealed_requires_usb_admission_to_close_before_empty_queues_prove_shutdown(
        tmp_path, clock, enabled):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.capture_started_at = clock.time() - 10
    session.usb_sink_participated = True
    sealed_status(tmp_path, clock)
    source = session.read(tmp_path/'source.json')
    source['camera']['sink'].update(enabled=enabled, writerRunning=True)
    atomic_json(tmp_path/'source.json', source)
    assert not session.wait_sealed(seconds=1)
    # Shared camera workers may keep running once frame admission has stopped.
    sealed_status(tmp_path, clock)
    source = session.read(tmp_path/'source.json')
    source['camera']['sink']['writerRunning'] = True
    atomic_json(tmp_path/'source.json', source)
    assert session.wait_sealed(seconds=1)


@pytest.mark.parametrize('fault', ['remote-old', 'remote-active', 'remote-queue', 'remote-subscriptions',
                                  'local-subscriptions', 'camera-failed', 'camera-old', 'camera-queue',
                                  'camera-inflight', 'camera-inflight-missing'])
def test_wait_sealed_does_not_confuse_fresh_ssh_probe_with_source_shutdown(tmp_path, clock, fault):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    sealed_status(tmp_path, clock)
    source = session.read(tmp_path/'source.json')
    if fault == 'remote-old':
        source['recorder']['updatedAt'] = capture.datetime_iso(clock.time()-60)
    elif fault == 'remote-active':
        source['recorder']['ros']['active'] = True
    elif fault == 'remote-queue':
        source['recorder']['ros']['inFlightMessages'] = 1
    elif fault == 'remote-subscriptions':
        source['recorder']['ros']['subscriptionCleanupPending'] = 1
    elif fault == 'local-subscriptions':
        local = session.read(tmp_path/'status.json')
        local['ros']['subscriptionCleanupPending'] = 1
        atomic_json(tmp_path/'status.json', local)
    elif fault == 'camera-failed':
        source['camera'].update(state='stop_failed', store=None)
    elif fault == 'camera-old':
        source['camera']['updatedAt'] = capture.datetime_iso(clock.time()-60)
    elif fault == 'camera-queue':
        source['camera']['sink']['queueFrames'] = 2
    elif fault == 'camera-inflight':
        source['camera']['sink']['inFlightFrames'] = 1
    else:
        source['camera']['sink'].pop('inFlightFrames')
    atomic_json(tmp_path/'source.json', source)
    assert not session.wait_sealed(seconds=1)


def running_reports(session, clock, *, lidar=0, image=0, camera=None):
    stamp = capture.datetime_iso(clock.time())
    local = dict(updatedAt=stamp, store=dict(active={}),
        ros={**stopped_ros(), 'topics': {'/livox/lidar': dict(written=lidar),
                                       '/livox/imu': dict(written=lidar)}})
    remote = dict(updatedAt=stamp, store=dict(active={}),
        ros={**stopped_ros(), 'topics': {'/berxel/color/image_raw': dict(written=image)}})
    if camera is None:
        camera = dict(updatedAt=capture.datetime_iso(clock.time()-86400),
            state='stop_failed', store=None,
            sink=dict(queueFrames=0, queueBytes=0, writerRunning=False,
                      counters=dict(written=67714, enqueued=74147)))
    source = dict(updatedAt=stamp, connected=True, gateAcknowledged=True, gateEnabled=True,
        gateRevision=session.read(session.gate).get('updatedAt'), recorder=remote, camera=camera)
    atomic_json(session.control/'status.json', local)
    atomic_json(session.control/'source.json', source)


def test_ros_berxel_video_and_lidar_count_without_waiting_for_yesterdays_absent_usb_process(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    running_reports(session, clock)
    session.claim()
    clock.sleep(3)
    running_reports(session, clock, lidar=10, image=5)
    session.observe_sources()
    assert session.video_sources == {'/berxel/color/image_raw'}
    assert session.lidar_sources == {'/livox/lidar'}
    assert not session.usb_sink_participated
    session.release()
    sealed_status(tmp_path, clock)
    source = session.read(tmp_path/'source.json')
    source['camera'] = dict(updatedAt=capture.datetime_iso(clock.time()-86400),
                            state='stop_failed', store=None)
    atomic_json(tmp_path/'source.json', source)
    before = clock.elapsed
    assert session.wait_sealed(seconds=1)
    assert clock.elapsed == before, 'an unused historic camera failure must not delay stop'
    assert session.missing_data() == []


def test_usb_source_started_during_capture_is_observed_and_still_requires_strict_shutdown(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    running_reports(session, clock)
    session.claim()
    session.observe_sources()
    assert not session.usb_sink_participated and session.missing_data() == ['视频', '雷达']
    clock.sleep(3)
    camera = dict(updatedAt=capture.datetime_iso(clock.time()),
        store=dict(active={'camera1': dict(frames=1)}),
        sink=dict(enabled=True, writerRunning=True, queueFrames=0, queueBytes=0,
                  inFlightFrames=0, counters=dict(written=1, enqueued=1)))
    running_reports(session, clock, lidar=1, camera=camera)
    session.observe_sources()
    assert session.usb_sink_participated and session.missing_data() == []
    session.release()
    sealed_status(tmp_path, clock)
    source = session.read(tmp_path/'source.json')
    source['camera']['state'] = 'stop_failed'
    source['camera']['store'] = None
    atomic_json(tmp_path/'source.json', source)
    assert not session.wait_sealed(seconds=1), 'a participating USB writer must never be skipped'
    sealed_status(tmp_path, clock)
    assert session.wait_sealed(seconds=1)


def test_final_writes_under_own_closing_revision_still_enroll_usb_and_record_data(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    running_reports(session, clock)
    session.claim()
    clock.sleep(3)
    session.release()
    camera = dict(updatedAt=capture.datetime_iso(clock.time()), store=dict(active={}),
        sink=dict(enabled=False, writerRunning=True, queueFrames=0, queueBytes=0,
                  inFlightFrames=0, counters=dict(written=1, enqueued=1)))
    running_reports(session, clock, lidar=1, camera=camera)
    source = session.read(tmp_path/'source.json')
    source['gateEnabled'] = False
    atomic_json(tmp_path/'source.json', source)
    assert session.wait_sealed(seconds=1)
    assert session.usb_sink_participated and session.missing_data() == []
    session.record_result()
    marker = session.read(session.marker)
    assert marker['videoSources'] == ['USB相机']
    assert marker['lidarSources'] == ['/livox/lidar']
    assert marker['missingData'] == []


@pytest.mark.parametrize('segment_age,expected', [(0, []), (60, ['视频', '雷达'])])
def test_first_late_ros_report_needs_current_session_segment_write_evidence(
        tmp_path, clock, segment_age, expected):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    # Neither host had a status/counter baseline when the session was armed.
    session.claim()
    clock.sleep(3)
    running_reports(session, clock, lidar=10, image=5)
    stamp = capture.datetime_iso(clock.time()-segment_age)
    for name, sensor, topic in (('status.json', '雷达MID360', '/livox/lidar'),
                               ('source.json', 'Berxel彩色', '/berxel/color/image_raw')):
        report = session.read(tmp_path/name)
        target = report['recorder'] if name == 'source.json' else report
        target['store']['active'] = {sensor: dict(startedAt=stamp, messages=1,
            topics={topic: dict(count=1, type='fixture/msg/Recorded')})}
        if name == 'status.json':
            target['store']['active'][sensor]['topics']['/livox/imu'] = dict(count=1, type='sensor_msgs/msg/Imu')
        atomic_json(tmp_path/name, report)
    session.observe_sources()
    assert session.missing_data() == expected
    session.release()


@pytest.mark.parametrize('fault', ['unchanged', 'stale-source', 'wrong-revision', 'unacknowledged'])
def test_observation_cannot_count_prior_video_or_unconfirmed_source_reports(tmp_path, clock, fault):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    running_reports(session, clock, image=20)
    session.claim()
    clock.sleep(3)
    running_reports(session, clock, image=20 if fault == 'unchanged' else 25)
    source = session.read(tmp_path/'source.json')
    if fault == 'stale-source':
        source['updatedAt'] = capture.datetime_iso(clock.time()-60)
    elif fault == 'wrong-revision':
        source['gateRevision'] = 'another-session'
    elif fault == 'unacknowledged':
        source['gateAcknowledged'] = False
    atomic_json(tmp_path/'source.json', source)
    session.observe_sources()
    assert not session.video_sources and not session.usb_sink_participated
    session.release()


def test_claim_rolls_back_gate_when_own_marker_cannot_be_persisted(tmp_path, clock, monkeypatch):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    def write(path, value):
        if path == session.marker:
            raise OSError('fixture marker write failed')
        atomic_json(path, value)
    monkeypatch.setattr(capture, 'atomic_json', write)
    with pytest.raises(OSError, match='fixture'):
        session.claim()
    assert session.read(session.gate).get('enabled') is not True


def test_claim_closes_lease_if_initial_publish_raises_after_replacing_the_file(tmp_path, clock, monkeypatch):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    def write(path, value):
        atomic_json(path, value)
        if path == session.gate and value.get('enabled'):
            raise OSError('fixture directory fsync failed after replace')
    monkeypatch.setattr(capture, 'atomic_json', write)
    with pytest.raises(OSError, match='fixture'):
        session.claim()
    assert session.read(session.gate).get('enabled') is not True
    assert not session.marker.exists()


def test_environment_is_parsed_as_data_without_running_shell(tmp_path):
    path = tmp_path/'archive.env'
    path.write_text('ARCHIVE_CONTROL_DIR="/tmp/archive control"\n'
                    'ARCHIVE_VALUE="$(touch /tmp/forbidden-capture-side-effect)"\n')
    env = capture.read_environment(path)
    assert env['ARCHIVE_CONTROL_DIR'] == '/tmp/archive control'
    assert env['ARCHIVE_VALUE'] == '$(touch /tmp/forbidden-capture-side-effect)'


def test_killed_desktop_lease_expires_without_modifying_web_gate(tmp_path, clock):
    from collector.archive_core import GarageGate
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    atomic_json(session.web_gate, dict(schemaVersion=1, scope='garage', enabled=False, expiresAt=None))
    web_bytes = session.web_gate.read_bytes()
    session.claim()
    effective_gate = GarageGate(session.web_gate)
    assert effective_gate.enabled()
    clock.sleep(capture.LEASE_SECONDS + .01)
    assert not effective_gate.enabled()
    assert session.web_gate.read_bytes() == web_bytes


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '86401'])
def test_invalid_duration_is_rejected_without_any_gate_or_helper_action(tmp_path, monkeypatch, value):
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(tmp_path/'control'))
    monkeypatch.setattr(capture.CaptureSession, 'ensure_recorders', lambda _: pytest.fail('started helper'))
    with pytest.raises(SystemExit) as error:
        capture.main(['--duration', value])
    assert error.value.code == 2
    assert not (tmp_path/'control').exists()


@pytest.mark.parametrize('mode', ['--dry-run', '--status'])
def test_readonly_modes_never_start_helper_or_create_control(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(tmp_path/'control'))
    monkeypatch.setattr(capture.CaptureSession, 'ensure_recorders', lambda _: pytest.fail('started helper'))
    monkeypatch.setattr(capture, 'inspect_capture_radar', lambda *_args, **_options: pytest.fail('inspected sources'))
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_options: pytest.fail('started sources'))
    assert capture.main([mode]) == 0
    assert not (tmp_path/'control').exists()


@pytest.mark.parametrize('arguments', [
    ['--with-camera-yolo'], ['--no-device-start', '--start-cameras'],
    ['--no-device-start', '--with-camera-yolo'],
])
def test_source_control_flags_reject_invalid_scope_before_reading_configuration(monkeypatch, arguments):
    monkeypatch.setattr(capture, 'read_environment',
        lambda *_args: pytest.fail('invalid source control flags reached configuration'))
    with pytest.raises(SystemExit) as error:
        capture.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('failure', [
    capture.DeviceStartupError('invalid read-only source report'), OSError('local helper unavailable'),
])
def test_passive_source_check_failure_keeps_source_control_disabled(tmp_path, monkeypatch, failure):
    session = capture.CaptureSession(tmp_path)
    def inspect(*_args, **_options):
        raise failure
    monkeypatch.setattr(capture, 'inspect_capture_radar', inspect)
    monkeypatch.setattr(capture, 'start_capture_devices',
        lambda *_args, **_options: pytest.fail('passive check started camera sources'))
    session.ensure_passive_sources({}, cancelled=lambda: False)
    startup = session.device_startup
    assert startup['ok'] is False and startup['sourceControl'] is False
    assert startup['readOnlySources'] is True and set(startup['hosts']) == {'ipc'}
    assert startup['hosts']['ipc']['error'] == str(failure)
    assert session.recording_dds == dict(state='unchanged', reason='no_verified_driver_graph')
    assert not session.gate.exists()


def test_passive_dds_alignment_failure_keeps_other_available_sources_recordable(tmp_path, monkeypatch):
    from collector import archive_graph
    session = capture.CaptureSession(tmp_path)
    def align(*_args, **_options):
        raise archive_graph.RecordingGraphError('unverifiable DDS graph')
    monkeypatch.setattr(archive_graph, 'ensure_recording_dds', align)
    session.ensure_passive_sources({}, cancelled=lambda: False)
    assert session.device_startup['ok'] is False
    assert session.device_startup['sourceControl'] is False
    assert session.recording_dds['state'] == 'unavailable'
    assert session.recording_dds['reason'] == 'alignment_failed'
    session.claim()
    assert session.read(session.gate)['enabled'] is True
    session.release()


def test_absent_stop_serializes_camera_cleanup_without_starting_recorders(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('ARCHIVE_CONTROL_DIR', str(tmp_path/'control'))
    monkeypatch.setattr(capture.CaptureSession, 'ensure_recorders', lambda _: pytest.fail('started helper'))
    assert capture.main(['--stop']) == 0
    assert (tmp_path/'control/desktop-capture.lock').is_file()
    assert not (tmp_path/'control/desktop-lease.json').exists()


@pytest.mark.parametrize('failure', [False, True])
def test_timed_noninteractive_main_closes_lease_on_completion_and_runtime_failure(
        tmp_path, clock, monkeypatch, failure):
    runner = FakeRunner()
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=runner)
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    if failure:
        renew = session.renew
        def fail_after_claim(*, first=False):
            if first:
                return renew(first=True)
            raise OSError('fixture renewal failed')
        monkeypatch.setattr(session, 'renew', fail_after_claim)
    assert capture.main(['--duration', '.1']) == (2 if failure else 3)
    assert session.read(session.gate)['enabled'] is False
    assert session.read(session.marker)['state'] == 'stopped'
    assert not session.web_gate.exists()
    # Archive helper ownership is independent of the isolated source-start
    # step. In particular, cleanup never stops or restarts any device.
    assert [command for command, _ in runner.calls] == [
        ['systemctl', '--user', 'start', 'robot-native-archive.service'],
        ['systemctl', '--user', 'start', 'robot-native-archive-source-link.service'],
    ]


@pytest.mark.parametrize('lidar,image,expected', [(0, 0, 3), (1, 0, 3), (0, 1, 3), (1, 1, 0)])
def test_main_reports_complete_only_with_this_sessions_video_and_lidar_writes(
        tmp_path, clock, monkeypatch, capsys, lidar, image, expected):
    runner = FakeRunner()
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=runner)
    running_reports(session, clock)
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    def sleep_with_data(seconds):
        clock.elapsed += seconds
        running_reports(session, clock, lidar=lidar, image=image)
    monkeypatch.setattr(capture.time, 'sleep', sleep_with_data)
    assert capture.main(['--duration', '.1']) == expected
    errors = capsys.readouterr().err
    missing = errors.partition('未确认')[2].partition('数据已写盘')[0]
    assert ('视频' in missing) == (not image)
    assert ('雷达' in missing) == (not lidar)
    assert session.read(session.gate)['enabled'] is False
    assert [command for command, _ in runner.calls] == [
        ['systemctl', '--user', 'start', 'robot-native-archive.service'],
        ['systemctl', '--user', 'start', 'robot-native-archive-source-link.service'],
    ]


def test_main_rejects_web_owner_before_starting_even_helpers(tmp_path, clock, monkeypatch):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time)
    atomic_json(session.web_gate, dict(schemaVersion=1, scope='garage', enabled=True, expiresAt=None))
    web_bytes = session.web_gate.read_bytes()
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'ensure_recorders', lambda: pytest.fail('started helper for existing web owner'))
    assert capture.main(['--duration', '.1']) == 2
    assert session.web_gate.read_bytes() == web_bytes
    assert not session.gate.exists()


@pytest.mark.parametrize('ros_state,writer_alive', [('DISABLED', False), ('LISTENING', True)])
def test_wait_ready_accepts_fresh_standby_without_starting_lazy_writer(tmp_path, clock, ros_state, writer_alive):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    atomic_json(tmp_path/'status.json', dict(updatedAt=capture.datetime_iso(clock.time()),
        store=dict(active={}, errors={}), ros=dict(state=ros_state, active=writer_alive,
            workerAlive=writer_alive, gateError=None, tickError=None,
            subscriptionCleanupPending=0)))
    session.wait_ready(seconds=1)
    assert not session.gate.exists() and not session.marker.exists()


@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'stopped', 'failed', 'no-ros', 'gate-error'])
def test_wait_ready_rejects_missing_stale_or_failed_recorder_without_enabling_lease(tmp_path, clock, fault):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    status = dict(updatedAt=capture.datetime_iso(clock.time()),
        ros=dict(state='DISABLED', active=False, workerAlive=False,
                 gateError=None, tickError=None, subscriptionCleanupPending=0))
    if fault == 'stale':
        status['updatedAt'] = capture.datetime_iso(clock.time()-9)
    elif fault == 'future':
        status['updatedAt'] = capture.datetime_iso(clock.time()+10)
    elif fault == 'stopped':
        status['state'] = 'stopped'
    elif fault == 'failed':
        status['state'] = 'stop_failed'
    elif fault == 'no-ros':
        status.pop('ros')
    elif fault == 'gate-error':
        status['ros'].update(workerAlive=True, state='ERROR', gateError='fixture gate failure')
    if fault != 'missing':
        atomic_json(tmp_path/'status.json', status)
    with pytest.raises(capture.CaptureError):
        session.wait_ready(seconds=1)
    assert not session.gate.exists() and not session.marker.exists()


@pytest.mark.parametrize('bad_ros', [True, ['broken'], 'broken'])
def test_wait_ready_treats_malformed_nested_status_as_not_ready(tmp_path, clock, bad_ros):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    atomic_json(tmp_path/'status.json', dict(updatedAt=capture.datetime_iso(clock.time()), ros=bad_ros))
    with pytest.raises(capture.CaptureError, match='未确认就绪'):
        session.wait_ready(seconds=1)
    assert not session.gate.exists()


@pytest.mark.parametrize('field', ['local-store', 'local-ros', 'remote', 'remote-store',
                                  'remote-ros', 'camera', 'camera-store', 'camera-sink'])
def test_wait_sealed_never_crashes_or_confirms_malformed_nested_status(tmp_path, clock, field):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    sealed_status(tmp_path, clock)
    local, source = session.read(tmp_path/'status.json'), session.read(tmp_path/'source.json')
    if field.startswith('local-'):
        local[field.removeprefix('local-')] = ['broken']
    elif field == 'remote':
        source['recorder'] = ['broken']
    elif field.startswith('remote-'):
        source['recorder'][field.removeprefix('remote-')] = ['broken']
    elif field == 'camera':
        source['camera'] = ['broken']
    else:
        source['camera'][field.removeprefix('camera-')] = ['broken']
    atomic_json(tmp_path/'status.json', local)
    atomic_json(tmp_path/'source.json', source)
    assert not session.wait_sealed(seconds=1)


def test_main_closes_lease_even_if_final_source_observation_fails(tmp_path, clock, monkeypatch):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    def failed_observation():
        raise OSError('fixture observation failure')
    monkeypatch.setattr(session, 'observe_sources', failed_observation)
    assert capture.main(['--duration', '.1']) == 2
    assert session.read(session.gate)['enabled'] is False
    assert session.read(session.marker)['state'] == 'stopped'
    assert not session.web_gate.exists()


@pytest.mark.parametrize('stage', ['helpers', 'readiness'])
def test_cancelled_startup_never_arms_a_capture_lease(tmp_path, clock, monkeypatch, stage):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    handlers = {}
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda number, handler: handlers.setdefault(number, handler))
    def cancel(*_):
        handlers[signal.SIGTERM]()
    monkeypatch.setattr(session, 'ensure_recorders', cancel if stage == 'helpers' else lambda: None)
    monkeypatch.setattr(session, 'wait_ready', cancel if stage == 'readiness' else
                        lambda _: pytest.fail('waited for readiness after startup was cancelled'))
    monkeypatch.setattr(session, 'wait_sealed', lambda _: pytest.fail('capture started after cancellation'))
    assert capture.main(['--duration', '.1']) == 0
    assert not session.gate.exists() and not session.marker.exists()


def test_progress_reports_waiting_immediately_and_every_fifteen_seconds(tmp_path, clock, capsys):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    web_before = session.read(session.web_gate)
    session.show_progress()
    first = capsys.readouterr().out
    assert '已运行 0 秒' in first and '脚本仍在运行' in first
    assert '视频输入：等待数据，尚未确认写盘' in first
    assert '雷达：等待数据，尚未确认写盘' in first
    clock.sleep(3)
    session.show_progress()
    assert capsys.readouterr().out == ''
    clock.sleep(12)
    session.show_progress()
    assert '已运行 15 秒' in capsys.readouterr().out
    assert session.read(session.web_gate) == web_before
    assert not session.web_gate.exists()


def test_progress_reports_real_data_change_without_waiting_for_next_heartbeat(tmp_path, clock, capsys):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    running_reports(session, clock)
    session.claim()
    session.show_progress()
    capsys.readouterr()
    clock.sleep(3)
    running_reports(session, clock, lidar=1)
    session.observe_sources()
    session.show_progress()
    progress = capsys.readouterr().out
    assert '已运行 3 秒' in progress
    assert '视频输入：等待数据，尚未确认写盘' in progress
    assert '雷达：本次各路已确认写盘' in progress
    assert '相机主机：已连接，保存开关已同步' in progress
    assert '已连接并确认采集' not in progress


def test_preview_without_archive_heartbeat_reports_missing_recording_and_keeps_lease(tmp_path, clock, capsys):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.device_startup = dict(hosts=dict(jetson=dict(devices=[
        dict(key='usb-cameras', dataVerified=True, state='reused')])))
    session.claim()
    clock.sleep(16)
    session.renew()
    stamp = capture.datetime_iso(clock.time())
    atomic_json(tmp_path/'source.json', dict(updatedAt=stamp, connected=True,
        gateAcknowledged=True, gateEnabled=True, gateRevision=next(iter(session._gate_revisions)),
        recorder=dict(updatedAt=stamp, store=dict(enabled=True)),
        camera=dict(updatedAt=capture.datetime_iso(clock.time()-60))))
    session.show_progress()
    output = capsys.readouterr().out
    assert '录制异常：USB 相机画面已就绪，但原始视频归档状态缺失或过期' in output
    assert '视频输入：等待数据，尚未确认写盘' in output
    assert session.read(session.gate)['enabled'] is True
    assert session.read(session.marker)['recordingWarnings']
    source = session.read(tmp_path/'source.json')
    source['camera']['updatedAt'] = stamp
    atomic_json(tmp_path/'source.json', source)
    assert session.recording_warnings() == []


def test_synced_gate_does_not_hide_disabled_recorders_or_storage_rejection(tmp_path, clock):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    clock.sleep(16)
    session.renew()
    stamp = capture.datetime_iso(clock.time())
    atomic_json(tmp_path/'status.json', dict(updatedAt=stamp,
        store=dict(enabled=False, errors=dict(storage='disk protected'))))
    atomic_json(tmp_path/'source.json', dict(updatedAt=stamp, connected=True,
        gateEnabled=True, gateAcknowledged=True,
        recorder=dict(updatedAt=stamp, store=dict(enabled=False))))
    warnings = session.recording_warnings()
    assert any('本机录制开关尚未生效' in message for message in warnings)
    assert any('相机主机已收到开关' in message for message in warnings)
    assert any('本机写盘保护：disk protected' in message for message in warnings)
    session.release()
    assert session.recording_warnings() == []


def test_progress_does_not_treat_stale_connection_as_current_or_stop_the_lease(tmp_path, clock, capsys):
    session = capture.CaptureSession(tmp_path, clock=clock.time)
    session.claim()
    atomic_json(tmp_path/'source.json', dict(updatedAt=capture.datetime_iso(clock.time()-60),
                                           connected=True, gateAcknowledged=True))
    session.show_progress()
    assert '相机主机：等待状态同步' in capsys.readouterr().out
    assert session.read(session.gate)['enabled'] is True


def test_main_announces_startup_waiting_and_shutdown_before_blocking(tmp_path, clock, monkeypatch, capsys):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    def ready(_):
        assert '正在等待归档进程就绪' in capsys.readouterr().out
    def sealed(_):
        assert '正在结束本次采集并收尾' in capsys.readouterr().out
        return True
    monkeypatch.setattr(session, 'wait_ready', ready)
    monkeypatch.setattr(session, 'wait_sealed', sealed)
    assert capture.main(['--duration', '.1']) == 3
    assert session.read(session.marker)['missingData'] == ['视频', '雷达']


@pytest.mark.parametrize('source_mode', [[], ['--no-device-start'], ['--start-cameras']])
def test_default_is_passive_and_camera_start_requires_explicit_option(
        tmp_path, clock, monkeypatch, source_mode):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    order = []
    original_claim = session.claim
    def claim():
        order.append('record')
        original_claim()
    monkeypatch.setattr(session, 'claim', claim)
    def devices(values, *, cancelled):
        assert not session.gate.exists() and not cancelled()
        order.append('devices')
        assert values['ARCHIVE_SOURCE_HOST'] == 'private-source.test'
        return dict(ok=True, hosts={}, incompleteDevices=[])
    monkeypatch.setattr(capture, 'start_capture_devices', devices)
    def inspect(values, *, cancelled):
        assert not session.gate.exists() and not cancelled()
        order.append('inspect-radar')
        assert values['ARCHIVE_SOURCE_HOST'] == 'private-source.test'
        return dict(ok=True, role='ipc', recordingOnly=True,
                    readOnlySources=True, sourceControl=False, devices=[])
    monkeypatch.setattr(capture, 'inspect_capture_radar', inspect)
    monkeypatch.setenv('ARCHIVE_SOURCE_HOST', 'private-source.test')
    args = ['--duration', '.1', *source_mode]
    assert capture.main(args) == 3
    assert order == (['devices', 'record'] if source_mode == ['--start-cameras'] else ['inspect-radar', 'record'])
    marker = session.read(session.marker)
    if source_mode == ['--start-cameras']:
        assert marker['deviceStartup'] == dict(ok=True, hosts={}, incompleteDevices=[])
    else:
        assert marker['deviceStartup']['readOnlySources'] is True
        assert marker['deviceStartup']['sourceControl'] is False
        assert marker['cameraShutdown'] == dict(ok=True, skipped=True, reason='passive_sources')


def test_partially_started_devices_record_available_data_but_report_incomplete(
        tmp_path, clock, monkeypatch):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    summary = dict(ok=False, hosts={}, incompleteDevices=[dict(host='jetson', key='fisheye')])
    monkeypatch.setattr(capture, 'start_capture_devices', lambda *_args, **_options: summary)
    def writes():
        session.video_sources.add('test-video')
        session.lidar_sources.add('/livox/lidar')
    monkeypatch.setattr(session, 'observe_sources', writes)
    assert capture.main(['--duration', '.1', '--start-cameras']) == 3
    marker = session.read(session.marker)
    assert marker['missingData'] == [] and marker['deviceStartup'] == summary
    assert session.read(session.gate)['enabled'] is False


@pytest.mark.parametrize('complete_late', [True, False])
def test_passive_late_source_uses_written_evidence_instead_of_initial_inactive_state(
        tmp_path, clock, monkeypatch, complete_late):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    running_reports(session, clock)
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    monkeypatch.setattr(session, 'persist_session_end', lambda *_a, **_k: None)
    monkeypatch.setattr(capture, 'inspect_capture_radar', lambda *_a, **_k:
        dict(ok=False, role='ipc', recordingOnly=True, readOnlySources=True, sourceControl=False,
            devices=[dict(key=key, state='inactive') for key in ('mid360', 'stl19p', 'bluesea')]))
    monkeypatch.setattr(session, 'close_cameras', lambda *_a, **_k: pytest.fail('passive capture stopped cameras'))
    def sleep_with_late_data(seconds):
        clock.elapsed += seconds
        running_reports(session, clock, lidar=4, image=5)
        if not complete_late:
            report = session.read(session.control/'status.json')
            report['ros']['topics']['/livox/imu']['written'] = 0
            atomic_json(session.control/'status.json', report)
    monkeypatch.setattr(capture.time, 'sleep', sleep_with_late_data)
    assert capture.main(['--duration', '.1']) == (0 if complete_late else 3)
    marker = session.read(session.marker)
    assert marker['deviceStartup']['ok'] is False  # Original check remains in the audit evidence.
    assert (marker['missingData'] == []) is complete_late
    assert marker['lidarDevices'][0]['expected'] is True
    assert marker['lidarDevices'][0]['complete'] is complete_late
    assert marker['cameraShutdown']['reason'] == 'passive_sources'
    assert session.read(session.gate)['enabled'] is False


@pytest.mark.parametrize('raise_cancel', [False, True])
def test_cancelling_device_start_never_claims_recording_lease(
        tmp_path, clock, monkeypatch, raise_cancel):
    session = capture.CaptureSession(tmp_path/'control', clock=clock.time, runner=FakeRunner())
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    handlers = {}
    monkeypatch.setattr(capture.signal, 'signal', lambda number, handler: handlers.setdefault(number, handler))
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    def devices(_values, *, cancelled):
        handlers[signal.SIGTERM]()
        assert cancelled()
        if raise_cancel:
            raise capture.DeviceStartupCancelled()
        return dict(ok=True, hosts={}, incompleteDevices=[])
    monkeypatch.setattr(capture, 'start_capture_devices', devices)
    assert capture.main(['--duration', '.1', '--start-cameras']) == 0
    assert not session.gate.exists() and not session.marker.exists()
