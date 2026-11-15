"""Read actual chassis feedback before shutdown; never publish robot commands.

``probe_motion`` accepts an injected zero-argument ``fetch`` returning a snapshot
or a list of snapshots. A snapshot may be wrapped as ``{source, snapshot}`` to
keep independent local endpoints separate. No telemetry means UNKNOWN, not zero.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request


LOCAL_SNAPSHOTS = (
    'http://127.0.0.1:8766/snapshot.json',
    'http://127.0.0.1:8765/snapshot.json',
)
MAX_AGE_S = 2.0
MAX_FUTURE_S = 0.25
MAX_SAMPLE_GAP_S = 1.0
POLL_S = 0.25
MAX_RESPONSE_BYTES = 256 * 1024
SPEED_LIMITS = {
    'leftWheelMps': 0.01,
    'rightWheelMps': 0.01,
    'actualVxMps': 0.01,
    'actualWzRadps': 0.02,
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_snapshots():
    # Do not inherit proxy settings or follow redirects outside these two local
    # read-only endpoints. Authentication failure is unavailable evidence.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    results = []
    for url in LOCAL_SNAPSHOTS:
        request = urllib.request.Request(url, headers={'Accept': 'application/json',
                                                     'Cache-Control': 'no-cache'}, method='GET')
        try:
            with opener.open(request, timeout=1.0) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    continue
                snapshot = json.loads(body)
            if isinstance(snapshot, dict):
                results.append({'source': url, 'snapshot': snapshot})
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return results


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _age_ok(stamp, now):
    return _number(stamp) and stamp > 0 and -MAX_FUTURE_S <= now - stamp <= MAX_AGE_S


def _sample(snapshot, now):
    if not isinstance(snapshot, dict):
        return 'UNKNOWN', 'invalid_snapshot', None
    if type(snapshot.get('schemaVersion')) is not int or snapshot['schemaVersion'] != 2:
        return 'UNKNOWN', 'unsupported_snapshot_schema', None
    if not _age_ok(snapshot.get('timestamp'), now):
        return 'UNKNOWN', 'stale_snapshot', None
    if snapshot.get('serialConnected') is not True or snapshot.get('chassisAlive') is not True:
        return 'UNKNOWN', 'chassis_feedback_unavailable', None
    debug = snapshot.get('debug')
    pages = debug.get('telemetryPages') if isinstance(debug, dict) else None
    motion = pages.get('motion') if isinstance(pages, dict) else None
    if not isinstance(motion, dict) or motion.get('valid') is not True:
        return 'UNKNOWN', 'motion_feedback_unavailable', None
    age = motion.get('ageS')
    stamp = motion.get('lastUpdate')
    if (not _number(age) or not 0 <= age <= MAX_AGE_S or not _age_ok(stamp, now)
            or stamp > snapshot['timestamp'] + MAX_FUTURE_S):
        return 'UNKNOWN', 'stale_motion_feedback', None
    speeds = {key: snapshot.get(key) for key in SPEED_LIMITS}
    sample = {'stamp': stamp, 'sequence': motion.get('sequence'), 'speeds': speeds}
    # One trustworthy actual moving wheel/axis is sufficient to veto shutdown,
    # even if a different speed field is unavailable or malformed.
    if any(_number(value) and abs(value) > SPEED_LIMITS[key] for key, value in speeds.items()):
        return 'MOVING', 'actual_motion_detected', sample
    if not all(_number(value) for value in speeds.values()):
        return 'UNKNOWN', 'actual_speed_unavailable', None
    if type(sample['sequence']) is not int or not 0 <= sample['sequence'] <= 255:
        return 'UNKNOWN', 'invalid_motion_sequence', None
    return 'STATIONARY', 'actual_speeds_below_threshold', sample


def _rows(value):
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for index, row in enumerate(value):
        if isinstance(row, dict) and 'snapshot' in row:
            source = row.get('source')
            if not isinstance(source, str) or not source or len(source) > 256:
                source = 'snapshot:' + str(index)
            result.append((source, row['snapshot']))
        else:
            result.append(('snapshot:' + str(index), row))
    return result


def probe_motion(*, fetch=None, clock=time.time, sleeper=time.sleep, duration=3.0):
    """Return STATIONARY/MOVING/UNKNOWN from fresh STM32 V2 actual feedback.

    STATIONARY requires continuously advancing samples over ``duration`` seconds.
    A duplicate may bridge at most one second between real samples. UNKNOWN is
    intentionally fail closed and never requests, starts, or stops any device.
    This evidence check does not replace the caller's physical-stop confirmation.
    """
    if not _number(duration) or not 0 < duration <= 60:
        raise ValueError('duration must be a finite number in (0, 60]')
    fetch = _fetch_snapshots if fetch is None else fetch
    start = previous_now = clock()
    if not _number(start):
        return {'state': 'UNKNOWN', 'reason': 'invalid_clock', 'samples': 0,
                'checkedSamples': 0, 'elapsedS': 0.0, 'source': None}
    deadline = start + duration + MAX_SAMPLE_GAP_S
    tracks = {}
    total = 0
    latest_reason = 'local_feedback_unavailable'

    def report(state, reason, now, source=None, track=None, sample=None):
        value = {'state': state, 'reason': reason,
                 'samples': track['samples'] if track else 0,
                 'checkedSamples': total, 'elapsedS': round(max(0.0, now - start), 6),
                 'source': source}
        if sample:
            value['speeds'] = sample['speeds']
        return value

    # The iteration bound also fails closed if a custom sleeper/clock is broken.
    for _ in range(math.ceil((duration + MAX_SAMPLE_GAP_S) / POLL_S) + 2):
        now = clock()
        if not _number(now) or now < previous_now:
            return report('UNKNOWN', 'clock_not_monotonic', previous_now)
        previous_now = now
        try:
            rows = _rows(fetch())
        except Exception:
            rows = []
        # Network reads may consume time; validate freshness at receipt time.
        received = clock()
        if not _number(received) or received < now:
            return report('UNKNOWN', 'clock_not_monotonic', now)
        now = previous_now = received
        seen = set()
        ready = []
        for source, snapshot in rows:
            seen.add(source)
            total += 1
            state, reason, sample = _sample(snapshot, now)
            latest_reason = reason
            if state == 'MOVING':
                return report(state, reason, now, source, tracks.get(source), sample)
            if state == 'UNKNOWN':
                tracks.pop(source, None)
                continue
            track = tracks.get(source)
            if track and now - track['lastSeen'] > MAX_SAMPLE_GAP_S:
                tracks.pop(source)
                track = None
            if track and sample['stamp'] == track['lastStamp']:
                latest_reason = 'motion_sample_not_advancing'
                continue
            if track and (sample['stamp'] < track['lastStamp'] or sample['sequence'] == track['lastSequence']):
                tracks.pop(source)
                latest_reason = 'motion_sample_not_advancing'
                continue
            if track is None:
                track = {'firstSeen': now, 'lastSeen': now, 'lastStamp': sample['stamp'],
                         'lastSequence': sample['sequence'], 'samples': 1}
                tracks[source] = track
            else:
                track.update(lastSeen=now, lastStamp=sample['stamp'],
                             lastSequence=sample['sequence'], samples=track['samples'] + 1)
            if now - track['firstSeen'] >= duration and track['samples'] >= 2:
                ready.append((source, track, sample))
        # Missing sources break continuity, rather than borrowing a different
        # source's samples or treating a failed request as a measured zero.
        for source in tuple(tracks):
            if source not in seen:
                tracks.pop(source)
        # Check every endpoint for a moving veto before returning stationary.
        if ready:
            source, track, sample = ready[0]
            return report('STATIONARY', 'fresh_actual_feedback_stable', now, source, track, sample)
        if not rows:
            latest_reason = 'local_feedback_unavailable'
        if now >= deadline:
            break
        sleeper(min(POLL_S, deadline - now))
    best = max(tracks.values(), key=lambda item: item['samples'], default=None)
    source = next((key for key, value in tracks.items() if value is best), None)
    if latest_reason == 'actual_speeds_below_threshold':
        latest_reason = 'insufficient_continuous_feedback'
    return report('UNKNOWN', latest_reason, previous_now, source, best)
