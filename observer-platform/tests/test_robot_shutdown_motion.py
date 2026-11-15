import copy
import json
import socket
import urllib.request

import pytest

from collector import robot_shutdown_motion as motion


@pytest.fixture(autouse=True)
def prohibit_real_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('tests must not create a real socket')
    monkeypatch.setattr(socket, 'socket', denied)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def snapshot(clock, **speeds):
    value = {
        'schemaVersion': 2, 'timestamp': clock(), 'serialConnected': True,
        'chassisAlive': True, 'actualVxMps': 0.0, 'actualWzRadps': 0.0,
        'leftWheelMps': 0.0, 'rightWheelMps': 0.0,
        'debug': {'telemetryPages': {'motion': {
            'valid': True, 'ageS': 0.0, 'lastUpdate': clock(),
            'sequence': int((clock() - 1000) * 4) % 256,
        }}},
    }
    value.update(speeds)
    return value


def probe(fetch, clock=None, **kwargs):
    clock = clock or Clock()
    return motion.probe_motion(fetch=fetch, clock=clock, sleeper=clock.sleep, **kwargs)


def test_continuously_fresh_actual_feedback_proves_stationary():
    clock = Clock()
    result = probe(lambda: snapshot(clock), clock)
    assert result['state'] == 'STATIONARY'
    assert result['reason'] == 'fresh_actual_feedback_stable'
    assert result['elapsedS'] == 3.0
    assert result['samples'] == 13


@pytest.mark.parametrize('field,speed', [
    ('actualVxMps', .011), ('actualWzRadps', -.021),
    ('leftWheelMps', .5), ('rightWheelMps', -.5),
])
def test_any_fresh_actual_motion_vetoes_immediately(field, speed):
    clock = Clock()
    result = probe(lambda: snapshot(clock, **{field: speed}), clock)
    assert result['state'] == 'MOVING'
    assert result['elapsedS'] == 0
    assert result['speeds'][field] == speed


def test_valid_motion_vetoes_even_when_another_speed_is_unavailable():
    clock = Clock()
    result = probe(lambda: snapshot(clock, actualVxMps=None, leftWheelMps=.02), clock)
    assert result['state'] == 'MOVING'


@pytest.mark.parametrize('value', [None, True, False, float('nan'), float('inf'), '0'])
def test_missing_or_malformed_actual_speed_is_unknown(value):
    clock = Clock()
    result = probe(lambda: snapshot(clock, actualVxMps=value), clock)
    assert result['state'] == 'UNKNOWN'
    assert result['reason'] == 'actual_speed_unavailable'


def test_threshold_is_inclusive():
    clock = Clock()
    result = probe(lambda: snapshot(clock, actualVxMps=-.01, actualWzRadps=.02,
                                    leftWheelMps=.01, rightWheelMps=-.01), clock)
    assert result['state'] == 'STATIONARY'


def test_repeated_snapshot_does_not_prove_stationary():
    clock = Clock()
    saved = snapshot(clock)
    result = probe(lambda: saved, clock)
    assert result['state'] == 'UNKNOWN'
    assert result['samples'] < 2


def test_new_snapshot_timestamp_cannot_refresh_an_old_motion_frame():
    clock = Clock()
    saved = snapshot(clock)
    def fetch():
        value = copy.deepcopy(saved)
        value['timestamp'] = clock()
        return value
    assert probe(fetch, clock)['state'] == 'UNKNOWN'


def test_sequence_must_advance_even_with_new_receive_timestamps():
    clock = Clock()
    def fetch():
        value = snapshot(clock)
        value['debug']['telemetryPages']['motion']['sequence'] = 1
        return value
    assert probe(fetch, clock)['state'] == 'UNKNOWN'


@pytest.mark.parametrize('mutation,reason', [
    (lambda v: v.update(serialConnected=False), 'chassis_feedback_unavailable'),
    (lambda v: v.update(chassisAlive=False), 'chassis_feedback_unavailable'),
    (lambda v: v.update(schemaVersion=True), 'unsupported_snapshot_schema'),
    (lambda v: v['debug']['telemetryPages']['motion'].update(valid=False), 'motion_feedback_unavailable'),
    (lambda v: v['debug']['telemetryPages']['motion'].update(ageS=True), 'stale_motion_feedback'),
    (lambda v: v['debug']['telemetryPages']['motion'].update(ageS=2.1), 'stale_motion_feedback'),
    (lambda v: v.update(timestamp=v['timestamp'] - 3), 'stale_snapshot'),
    (lambda v: v.update(timestamp=v['timestamp'] + 1), 'stale_snapshot'),
])
def test_stale_or_disconnected_sources_cannot_prove_motion(mutation, reason):
    clock = Clock()
    def fetch():
        value = snapshot(clock, actualVxMps=.5)
        mutation(value)
        return value
    result = probe(fetch, clock)
    assert result['state'] == 'UNKNOWN'
    assert result['reason'] == reason


def test_missing_data_breaks_continuity():
    clock = Clock()
    def fetch():
        return None if clock() == 1001.5 else snapshot(clock)
    result = probe(fetch, clock)
    assert result['state'] == 'UNKNOWN'
    assert result['reason'] == 'insufficient_continuous_feedback'


def test_unavailable_endpoint_does_not_borrow_another_source_samples():
    clock = Clock()
    def fetch():
        source = 'one' if clock() < 1002 else 'two'
        return [{'source': source, 'snapshot': snapshot(clock)}]
    assert probe(fetch, clock)['state'] == 'UNKNOWN'


def test_a_second_source_moving_overrules_a_ready_stationary_source():
    clock = Clock()
    def fetch():
        rows = [{'source': 'one', 'snapshot': snapshot(clock)}]
        if clock() >= 1003:
            rows.append({'source': 'two', 'snapshot': snapshot(clock, rightWheelMps=.5)})
        return rows
    assert probe(fetch, clock)['state'] == 'MOVING'


def test_no_feedback_and_fetch_errors_are_unknown():
    clock = Clock()
    def fetch():
        raise ConnectionRefusedError('offline')
    result = probe(fetch, clock)
    assert result['state'] == 'UNKNOWN'
    assert result['reason'] == 'local_feedback_unavailable'
    assert result['checkedSamples'] == 0


def test_clock_regression_is_unknown():
    clock = Clock()
    def sleeper(seconds):
        clock.now -= seconds
    result = motion.probe_motion(fetch=lambda: snapshot(clock), clock=clock, sleeper=sleeper)
    assert result['state'] == 'UNKNOWN'
    assert result['reason'] == 'clock_not_monotonic'


def test_broken_nonadvancing_clock_is_bounded():
    clock = Clock()
    result = motion.probe_motion(fetch=lambda: snapshot(clock), clock=clock, sleeper=lambda _: None)
    assert result['state'] == 'UNKNOWN'
    assert result['checkedSamples'] <= 18


@pytest.mark.parametrize('duration', [0, -1, True, float('nan'), 61])
def test_invalid_duration_rejected(duration):
    with pytest.raises(ValueError):
        probe(lambda: None, duration=duration)


def test_default_fetch_only_reads_local_endpoints_without_proxies_or_redirects(monkeypatch):
    calls = []
    clock = Clock()
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == motion.MAX_RESPONSE_BYTES + 1
            return json.dumps(snapshot(clock)).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_method(), timeout))
            return Response()
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], motion._NoRedirect)
        return Opener()
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    result = motion.probe_motion(clock=clock, sleeper=clock.sleep)
    assert result['state'] == 'STATIONARY'
    assert {url for url, _, _ in calls} == set(motion.LOCAL_SNAPSHOTS)
    assert all(method == 'GET' and timeout == 1 for _, method, timeout in calls)


def test_default_fetch_rejects_large_or_unparseable_responses(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return b' ' * limit
    class Opener:
        def open(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    assert motion._fetch_snapshots() == []


def test_redirect_handler_refuses_redirect():
    assert motion._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://remote/') is None
