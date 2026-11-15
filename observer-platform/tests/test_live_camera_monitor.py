"""Isolated continuous-monitor tests; never touch real credentials or endpoints."""
import argparse
import asyncio
import ast
from pathlib import Path
import time

import aiohttp
import pytest

from deploy.scripts import observe_live_cameras as monitor
from tests.test_protocol import jpeg


class Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        yield self.raw


class Response:
    def __init__(self, status=200, identity='isolated-session:1', raw=None, age='0.2', ttl='2'):
        self.status = status
        self.content_type = 'image/jpeg'
        self.headers = {'X-Frame-Sequence': identity, 'X-Frame-Age-S': age, 'X-Frame-TTL-S': ttl}
        self.content = Content(jpeg() if raw is None else raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize('value', ('9', '301', '0', '-1', '10.5', 'bad', None))
def test_duration_rejects_out_of_range_before_any_network(value):
    with pytest.raises(argparse.ArgumentTypeError):
        monitor.seconds_value(value)


@pytest.mark.parametrize('value', ('10', '120', '300'))
def test_duration_boundaries(value):
    assert monitor.seconds_value(value) == int(value)


@pytest.mark.parametrize('value', ('', None, 'usbFront,usbFront', '../status',
                                 'usbHub2,http://example.com', 'usbHub4', 'usbHub2,'))
def test_camera_subset_rejects_unknown_duplicates_or_paths(value):
    with pytest.raises(argparse.ArgumentTypeError):
        monitor.camera_keys_value(value)


def test_camera_subset_supports_explicit_hub_keys_without_impersonating_rear():
    assert monitor.camera_keys_value('usbHub2,usbHub3') == ('usbHub2', 'usbHub3')
    assert monitor.camera_keys_value('usbFront, usbLeft,usbHub2,usbHub3') == (
        'usbFront', 'usbLeft', 'usbHub2', 'usbHub3')
    assert monitor.HTTP_CAMERAS == ('usbFront', 'usbRear', 'usbLeft')
    assert monitor.camera_keys_value('usbFront,usbLeft,usbPort21') == (
        'usbFront', 'usbLeft', 'usbPort21')


def test_percentiles_and_empty_samples():
    assert monitor.distribution([])['p95'] is None
    assert monitor.distribution([1, 2, 3, 4]) == {
        'samples': 4, 'min': 1, 'median': 2.5, 'p95': 4, 'max': 4}


def test_camera_classification_and_advancing_identity_no_pixels_in_summary():
    async def run():
        session = Session([Response(), Response(identity='isolated-session:2'), Response(status=204),
                           Response(status=503), Response(status=429), asyncio.TimeoutError(),
                           aiohttp.ClientConnectionError()])
        stats = monitor.CameraStats()
        for _ in range(7):
            await monitor.sample_camera(session, 'usbFront', stats, time.monotonic() + 10)
        summary = stats.summary()
        assert summary['jpeg200'] == 2
        assert summary['noFrame204'] == 1
        assert summary['errors'] == 4
        assert summary['serverErrors'] == summary['clientHttpErrors'] == 1
        assert summary['timeouts'] == summary['transportErrors'] == 1
        assert summary['distinctFrames'] == 2 and summary['sequenceAdvances'] == 1
        assert summary['jpegBytes']['median'] == len(jpeg())
        assert stats.last_frame is None
        serialized = monitor.json.dumps(summary)
        assert 'last_frame' not in serialized and 'isolated-session' not in serialized
        assert all(url == monitor.ORIGIN + '/camera/usbFront.jpg' and opts['allow_redirects'] is False
                   for url, opts in session.requests)
    asyncio.run(run())


@pytest.mark.parametrize('response', (
    Response(status=401), Response(status=403), Response(status=302),
    Response(raw=b'not-an-image'), Response(identity='missing-counter'),
    Response(age='NaN'), Response(age='-1'), Response(age='3', ttl='2'), Response(ttl='0'),
))
def test_invalid_auth_jpeg_and_headers_are_fatal(response):
    async def run():
        stats = monitor.CameraStats()
        with pytest.raises(monitor.VerificationError):
            await monitor.sample_camera(Session([response]), 'usbFront', stats, time.monotonic() + 10)
        assert stats.errors == 1
    asyncio.run(run())


def test_same_frame_does_not_count_as_progress_and_regression_is_fatal():
    async def run():
        stats = monitor.CameraStats()
        session = Session([Response(identity='s:2'), Response(identity='s:2'), Response(identity='s:1')])
        for _ in range(2):
            await monitor.sample_camera(session, 'usbFront', stats, time.monotonic() + 10)
        assert stats.sequenceAdvances == 0 and len(stats.identities) == 1
        with pytest.raises(monitor.VerificationError, match='camera_sequence_regressed'):
            await monitor.sample_camera(session, 'usbFront', stats, time.monotonic() + 10)
    asyncio.run(run())


def test_read_snapshots_tracks_available_ratio_and_size_without_source_data():
    class WS:
        count = 0

        async def receive(self):
            self.count += 1
            if self.count > 2:
                await asyncio.sleep(1)
            payload = {'meta': {'readOnly': True, 'timestamp': self.count}, 'devices': {'cameras': {
                'usbFront': {'status': 'AVAILABLE' if self.count == 1 else 'STALE',
                             'ageS': self.count, 'frameIdentity': 's:1'}}}}
            return type('Message', (), {'type': aiohttp.WSMsgType.TEXT,
                                       'data': monitor.json.dumps(payload)})()

    async def run():
        stats = {key: monitor.CameraStats() for key in monitor.HTTP_CAMERAS}
        ws_stats = {'timeouts': 0, 'jsonBytes': []}
        await monitor.read_snapshots(WS(), stats, ws_stats, time.monotonic() + .03)
        assert stats['usbFront'].summary()['metadataAvailableRatio'] == .5
        assert stats['usbFront'].summary()['metadataAgeS']['median'] == 1.5
        assert len(ws_stats['jsonBytes']) == 2
        assert stats['usbRear'].metadataStates == {'UNKNOWN': 2}
    asyncio.run(run())


def test_script_syntax_fixed_origin_and_read_only_request_surface():
    source = Path(monitor.__file__).read_text()
    ast.parse(source)
    assert "trust_env=False" in source and 'ssl.create_default_context()' in source
    assert source.count('session.ws_connect(') == 1
    assert '/api/v1/ingest' not in source
    assert 'send_json(' not in source and 'send_bytes(' not in source
    assert "ORIGIN + '/api/login'" in source and "ORIGIN + '/api/logout'" in source
    assert "choices" not in source  # No operator-supplied origin or credential path.
