"""Stop confirmation uses persisted evidence, not terminal visibility or a sent signal."""
from types import SimpleNamespace
import signal

import pytest

from collector import archive_capture_session as capture
from collector.archive_core import atomic_json


@pytest.fixture
def owner(tmp_path, monkeypatch):
    session = capture.CaptureSession(tmp_path)
    token = 'a' * 32
    atomic_json(session.marker, dict(state='recording', pid=43210, processStart='start-7',
                                   token=token, bootId='current-boot'))
    monkeypatch.setattr(capture, 'boot_identity', lambda: 'current-boot')
    monkeypatch.setattr(capture, 'process_identity', lambda _: 'start-7')
    signals, time = [], [0.0]
    monkeypatch.setattr(capture.os, 'kill', lambda *args: signals.append(args))
    monkeypatch.setattr(capture.time, 'monotonic', lambda: time[0])
    monkeypatch.setattr(capture.time, 'sleep', lambda delay: time.__setitem__(0, time[0] + delay))
    monkeypatch.setattr(capture, 'stop_capture_cameras',
                        lambda *_args, **_kwargs: pytest.fail('concurrent camera stop during owned cleanup'))
    return SimpleNamespace(session=session, token=token, signals=signals, time=time)


def finish(owner, state='sealed', camera_ok=True):
    session = owner.session
    atomic_json(session.marker, dict(session.read(session.marker), state='stopped',
                                   cameraShutdown=dict(ok=camera_ok)))
    if state:
        atomic_json(session.control/'session-ends'/(owner.token+'.json'),
                    dict(sessionId=owner.token, state=state, captureIncomplete=False))


def test_stop_waits_for_end_proof_even_if_original_terminal_process_remains_alive(owner, monkeypatch, capsys):
    def advance(delay):
        owner.time[0] += delay
        if owner.time[0] >= 16:
            finish(owner)
    monkeypatch.setattr(capture.time, 'sleep', advance)
    assert owner.session.stop(timeout=20) == 0
    assert owner.signals == [(43210, signal.SIGTERM)]
    assert owner.time[0] >= 16
    output = capsys.readouterr().out
    assert '仍在等待' in output and '结束标记已保存' in output


def test_sent_stop_signal_is_not_success_without_completion_evidence(owner, capsys):
    assert owner.session.stop(timeout=1) == 3
    assert owner.signals == [(43210, signal.SIGTERM)]
    assert '等待超时' in capsys.readouterr().err


@pytest.mark.parametrize('proof_state,camera_ok', [('pending', True), ('sealed', False)])
def test_partial_stop_never_claims_complete_success(owner, monkeypatch, proof_state, camera_ok):
    monkeypatch.setattr(capture.os, 'kill', lambda *_: finish(owner, proof_state, camera_ok))
    assert owner.session.stop(timeout=1) == 3


def test_stopped_lease_and_closed_cameras_still_wait_for_durable_end_proof(owner, monkeypatch):
    def advance(delay):
        owner.time[0] += delay
        finish(owner, None)
    monkeypatch.setattr(capture.time, 'sleep', advance)
    assert owner.session.stop(timeout=1) == 3


def test_stop_waits_for_already_finishing_owner_without_sending_another_signal(owner, monkeypatch):
    atomic_json(owner.session.marker, dict(owner.session.read(owner.session.marker), state='stopped'))
    def advance(delay):
        owner.time[0] += delay
        finish(owner)
    monkeypatch.setattr(capture.time, 'sleep', advance)
    assert owner.session.stop(timeout=1) == 0
    assert owner.signals == []


def test_stop_does_not_touch_replacement_session(owner, monkeypatch):
    def replace(_pid, _value):
        atomic_json(owner.session.marker, dict(owner.session.read(owner.session.marker), token='b'*32))
    monkeypatch.setattr(capture.os, 'kill', replace)
    assert owner.session.stop(timeout=1) == 3
    assert owner.session.read(owner.session.marker)['token'] == 'b'*32


def test_passive_stop_and_repeated_stop_accept_skipped_camera_cleanup_without_source_commands(owner, monkeypatch):
    skipped = dict(ok=True, skipped=True, reason='passive_sources')
    def end_passive(_pid, _value):
        finish(owner)
        atomic_json(owner.session.marker, dict(owner.session.read(owner.session.marker),
            deviceStartup=dict(sourceControl=False, readOnlySources=True), cameraShutdown=skipped))
    monkeypatch.setattr(capture.os, 'kill', end_passive)
    monkeypatch.setattr(owner.session, 'close_cameras',
        lambda *_args, **_kwargs: pytest.fail('passive stop attempted camera cleanup'))
    assert owner.session.stop(timeout=1) == 0
    assert owner.session.stop(timeout=1) == 0
    assert owner.session.read(owner.session.marker)['cameraShutdown'] == skipped


def test_close_cameras_uses_full_camera_shutdown_budget(tmp_path, monkeypatch):
    session = capture.CaptureSession(tmp_path)
    timeouts = []
    def close(_values, *, timeout, ownership):
        assert ownership is None
        timeouts.append(timeout)
        return dict(ok=True, devices=[])
    monkeypatch.setattr(capture, 'stop_capture_cameras', close)
    assert session.close_cameras({})
    assert timeouts == [capture.CAMERA_SHUTDOWN_TIMEOUT]
    assert timeouts[0] > 105


@pytest.mark.parametrize('proof_state', ['pending', None])
def test_repeated_stop_does_not_bypass_missing_or_pending_end_proof(owner, monkeypatch, proof_state):
    finish(owner, proof_state)
    monkeypatch.setattr(capture, 'stop_capture_cameras', lambda *_args, **_kwargs: dict(ok=True))
    assert owner.session.stop(timeout=1) == 3
    assert owner.signals == []


def test_fallback_stop_preserves_another_capture_holding_the_lock(owner):
    finish(owner)
    with owner.session.lock():
        assert owner.session.stop(timeout=1) == 3
    assert owner.signals == []


def test_fallback_rechecks_capture_identity_after_acquiring_the_lock(owner, monkeypatch):
    from contextlib import contextmanager
    finish(owner)
    @contextmanager
    def replace_then_lock():
        atomic_json(owner.session.marker, dict(owner.session.read(owner.session.marker), token='b'*32))
        yield
    monkeypatch.setattr(owner.session, 'lock', replace_then_lock)
    assert owner.session.stop(timeout=1) == 3
    assert owner.signals == []
