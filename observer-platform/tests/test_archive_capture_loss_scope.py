"""Per-session loss evidence uses temporary status files, never live services."""
from types import SimpleNamespace

import pytest

from collector import archive_capture_session as capture
from collector.archive_capture_session import CaptureSession
from collector.archive_core import atomic_json

ROBOT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


def reports(control, *, dropped=0, dds_lost=0, usb_dropped=0, written=100):
    ros = dict(topics={'/berxel/color/image_raw': dict(
        written=written, received=written, dropped=dropped, dds_lost=dds_lost)})
    atomic_json(control/'status.json', dict(ros=ros, store=dict(writtenBytes=written)))
    atomic_json(control/'source.json', dict(recorder=dict(ros=ros,
        store=dict(writtenBytes=written)), camera=dict(
            sink=dict(counters=dict(written=written, writerDropped=usb_dropped)),
            store=dict(cameras={'front': dict(sequenceGaps=0)}))))


def finish(session):
    return session.persist_session_end(dict(ARCHIVE_ROBOT_ID=ROBOT),
        sealed=True, incomplete=False)['captureIncomplete']


def test_unchanged_losses_from_previous_sessions_do_not_mark_new_capture(tmp_path):
    reports(tmp_path, dropped=5, dds_lost=2, usb_dropped=3)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, dropped=5, dds_lost=2, usb_dropped=3, written=200)
    assert finish(session) is False


def test_new_usb_loss_still_marks_capture_incomplete(tmp_path):
    reports(tmp_path, usb_dropped=3)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, usb_dropped=4, written=200)
    assert finish(session) is True


def test_new_dds_loss_is_not_missed_when_no_callback_was_dropped(tmp_path):
    reports(tmp_path)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, dds_lost=2, written=200)
    assert finish(session) is True


def test_counter_reset_is_uncertain_even_when_current_loss_count_is_zero(tmp_path):
    reports(tmp_path, dropped=5, usb_dropped=3)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, written=10)
    assert finish(session) is True


def test_missing_final_source_snapshot_is_not_treated_as_zero_loss(tmp_path):
    reports(tmp_path)
    session = CaptureSession(tmp_path)
    session.claim()
    (tmp_path/'source.json').unlink()
    assert finish(session) is True


def test_source_without_a_claim_time_baseline_remains_uncertain(tmp_path):
    reports(tmp_path)
    (tmp_path/'source.json').unlink()
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, written=200)
    assert finish(session) is True


def test_loss_observed_mid_capture_cannot_be_erased_by_later_restart(tmp_path):
    reports(tmp_path)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, dropped=1, written=200)
    session.observe_sources()
    reports(tmp_path, written=10)
    assert finish(session) is True


def test_normal_sampling_and_inactive_drops_are_not_capture_loss(tmp_path):
    reports(tmp_path)
    session = CaptureSession(tmp_path)
    session.claim()
    reports(tmp_path, written=200)
    source = session.read(tmp_path/'source.json')
    source['camera']['sink']['counters'].update(sampledDropped=30, inactiveDropped=20)
    atomic_json(tmp_path/'source.json', source)
    assert finish(session) is False


@pytest.mark.parametrize('loss,expected', [('unchanged', 0), ('usb', 3), ('dds', 3), ('reset', 3)])
def test_desktop_main_reports_persisted_capture_loss_as_incomplete(
        tmp_path, monkeypatch, capsys, loss, expected):
    control = tmp_path/'control'
    reports(control, dropped=5, dds_lost=2, usb_dropped=3)
    elapsed = [0.0]
    session = CaptureSession(control, clock=lambda: 2_000_000_000.0 + elapsed[0])
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'read_environment', lambda _: {'ARCHIVE_ROBOT_ID': ROBOT})
    monkeypatch.setattr(capture, 'CaptureSession', lambda _: session)
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(capture.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(capture.time, 'sleep', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    monkeypatch.setattr(session, 'ensure_recorders', lambda: None)
    monkeypatch.setattr(capture, 'inspect_capture_radar',
        lambda *_args, **_kwargs: dict(ok=True, devices=[]))
    monkeypatch.setattr(session, 'wait_ready', lambda _: None)
    monkeypatch.setattr(session, 'wait_sealed', lambda _: True)
    monkeypatch.setattr(session, 'show_progress', lambda: None)
    monkeypatch.setattr(capture, 'stop_capture_cameras', lambda *_args, **_kwargs: dict(ok=True))

    def observe():
        session.video_sources.add('USB相机')
        session.lidar_sources.add('/livox/lidar')
        reports(control, dropped=0 if loss == 'reset' else 5,
            dds_lost=3 if loss == 'dds' else (0 if loss == 'reset' else 2),
            usb_dropped=4 if loss == 'usb' else (0 if loss == 'reset' else 3),
            written=10 if loss == 'reset' else 200)
    monkeypatch.setattr(session, 'observe_sources', observe)

    assert capture.main(['--duration', '.1', '--no-device-start']) == expected
    proof = session.read(control/'session-ends'/(session.token+'.json'))
    assert proof['state'] == 'sealed'
    assert proof['captureIncomplete'] is (expected == 3)
    output = capsys.readouterr()
    assert ('结果标记为未完成' in output.err) is (expected == 3)
    assert session.read(session.gate)['enabled'] is False
