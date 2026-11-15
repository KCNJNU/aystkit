"""Serial weak-link scheduling, real age and ambiguous receipts; isolated data only."""
import asyncio
import json
import time
import uuid
from types import SimpleNamespace as NS

import aiohttp
from aiohttp import web
import pytest

from collector.config import Config
from collector.transport import LatestQueue, Uploader, retry_delay, upload_timeout
from collector.transport import UploadNetworkRoute
from cloud.backend.store import Store
from tests.test_upload_latency import cloud, envelope


def observation():
    return {name: {} for name in ('system', 'robot', 'devices', 'ros2', 'pages', 'errors')}


def durable_payload(kind):
    now, identifier = time.time(), str(uuid.uuid4())
    if kind == 'events':
        return {'events': [dict(id=identifier, timestamp=now, level='ERROR', code='FIXTURE_ERROR',
                               component='fixture', message='isolated', details={})]}, identifier
    return {'samples': [dict(id=identifier, run_id=str(uuid.uuid4()), revision=1,
                             start=now-1, end=now-.5, valid_seconds=.5, distance_m=.1)],
            'status': dict(applied_revision=0, run_id=None, state='idle',
                           odom_status='WAITING_UPSTREAM', last_sample_timestamp=None,
                           rejected_samples=0, dropped_samples=0)}, identifier


def uploader_for_test(queue=None):
    return Uploader(Config('isolated-robot', 'isolated-source', cloud_url='https://fixture.invalid',
                           token='isolated-token-'+'x'*32, media_keys=('map', 'registered')), queue)


def enqueue_controls(uploader, cycle=0, collected=None):
    uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': cycle}, collector_timestamp=collected)
    uploader.enqueue('observation', '/observer/observation', observation(), collector_timestamp=collected)


def test_control_turns_and_failed_bulk_turns_are_fair_without_changing_peek():
    uploader = uploader_for_test()
    uploader.enable_events(lambda _ids: None)
    uploader.enable_test_metrics(lambda _ids: None, lambda *_args: None)
    for key, kind in (('map', 'map'), ('registered', 'cloud')):
        uploader.enqueue('media', '/isolated/'+key, {'key': key, 'kind': kind, 'status': 'WAITING_UPSTREAM'})
    for kind in ('events', 'test_metrics'):
        uploader.enqueue(kind, '/observer/'+kind, durable_payload(kind)[0])
    enqueue_controls(uploader)
    assert uploader.queue.peek().key == 'media:map'  # Existing FIFO API is unchanged.
    turns = []
    for cycle in range(8):
        enqueue_controls(uploader, cycle)
        # Intentionally never acknowledge anything: even repeated failed bulk
        # attempts must not starve another media key or durable event/metric.
        turns.extend(uploader.queue.next_upload() for _ in range(3))
    assert [item.key for item in turns[::3]] == ['heartbeat']*8
    assert [item.key for item in turns[1::3]] == ['observation']*8
    assert [item.key for item in turns[2::3]] == ['media:map', 'media:registered', 'events', 'test_metrics']*2
    assert [item.envelope['payload']['uptime_s'] for item in turns[::3]] == list(range(8))


def test_live_request_budget_expires_without_zero_disabling_the_timeout():
    wall, mono = time.time(), [0.0]
    queue = LatestQueue(clock=lambda: mono[0], wall_clock=lambda: wall)
    original = envelope(cloud(age=1.5), kind='media', collected=wall)
    queue.put('media:registered', original)
    entry = queue.next_upload()
    assert upload_timeout(entry, 0).total == pytest.approx(.5)
    assert upload_timeout(entry, .5) is None
    assert upload_timeout(entry, 10) is None
    assert entry.envelope['collector_timestamp'] == original['collector_timestamp']
    mono[0] = .51
    assert queue.next_upload() is None
    assert queue.stats()['expired_live'] == 1


def test_expiry_after_selection_counts_only_the_discarded_generation():
    queue = LatestQueue()
    queue.put('media:registered', envelope(cloud(frame=1), kind='media'))
    expired = queue.next_upload()
    queue.put('media:registered', envelope(cloud(frame=2), kind='media'))
    queue.remove(expired, expired_live=True)
    assert queue.stats()['keys'] == 1
    assert queue.stats()['dropped'] == queue.stats()['expired_live'] == 0
    queue.remove(queue.next_upload(), expired_live=True)
    assert queue.stats()['keys'] == 0
    assert queue.stats()['dropped'] == queue.stats()['expired_live'] == 1


@pytest.mark.parametrize('kind,seconds', [('heartbeat', 2), ('observation', 3), ('map', 4), ('events', 3), ('test_metrics', 3)])
def test_request_budgets_allow_maps_more_time_without_ten_second_blocking(kind, seconds):
    uploader = uploader_for_test()
    if kind == 'map':
        uploader.enqueue('media', '/map', {'key': 'map', 'kind': 'map', 'status': 'WAITING_UPSTREAM'})
    elif kind in ('events', 'test_metrics'):
        uploader.enqueue(kind, '/observer/'+kind, durable_payload(kind)[0])
    else:
        uploader.enqueue(kind, '/observer/'+kind, {'uptime_s': 0} if kind == 'heartbeat' else observation())
    timeout = upload_timeout(uploader.queue.peek(), uploader.queue.clock())
    assert timeout.total == seconds and timeout.connect <= 2
    assert 0 < retry_delay(100, 1) <= 3


@pytest.mark.parametrize('error', [asyncio.TimeoutError(), aiohttp.SocketTimeoutError('fixture read timeout')])
def test_short_live_media_total_or_read_timeout_does_not_switch_a_working_route(error):
    uploader = uploader_for_test()
    uploader.queue.put('media:registered', envelope(cloud(age=1.9), kind='media'))
    entry = uploader.queue.next_upload()
    route = UploadNetworkRoute('https://fixture.invalid/api/v1/ingest', {'HTTPS_PROXY': 'http://127.0.0.1:17890'})
    route.failed(None, error, entry=entry)
    assert route.proxy_for_request() is None
    # Control timeout still probes a configured fallback route.
    enqueue_controls(uploader)
    heartbeat = uploader.queue.next_upload()
    assert heartbeat.envelope['kind'] == 'heartbeat'
    route.failed(None, asyncio.TimeoutError(), entry=heartbeat)
    assert route.proxy_for_request() == route.proxy_url


@pytest.mark.parametrize('error', [aiohttp.ConnectionTimeoutError('fixture connection timeout'),
                                 aiohttp.ClientConnectionError('fixture connection failed'), OSError()])
def test_live_media_connection_failures_still_try_configured_fallback(error):
    uploader = uploader_for_test()
    uploader.queue.put('media:registered', envelope(cloud(age=1.9), kind='media'))
    route = UploadNetworkRoute('https://fixture.invalid/api/v1/ingest', {'HTTPS_PROXY': 'http://127.0.0.1:17890'})
    route.failed(None, error, entry=uploader.queue.next_upload())
    assert route.proxy_for_request() == route.proxy_url


def test_live_deadline_classification_handles_older_aiohttp_without_connection_timeout_class(monkeypatch):
    monkeypatch.delattr(aiohttp, 'ConnectionTimeoutError')
    uploader = uploader_for_test()
    uploader.queue.put('media:registered', envelope(cloud(age=1.9), kind='media'))
    route = UploadNetworkRoute('https://fixture.invalid/api/v1/ingest', {'HTTPS_PROXY': 'http://127.0.0.1:17890'})
    route.failed(None, asyncio.TimeoutError(), entry=uploader.queue.next_upload())
    assert route.proxy_for_request() is None


def test_real_short_media_timeout_yields_to_heartbeat_without_selecting_a_dead_proxy(monkeypatch):
    async def scenario():
        import collector.transport as transport
        route = UploadNetworkRoute('https://fixture.invalid/api/v1/ingest',
                                   {'HTTPS_PROXY': 'http://127.0.0.1:17890'})
        monkeypatch.setattr(transport, 'UploadNetworkRoute', lambda _url: route)
        proxies, received = [], []
        original_proxy = route.proxy_for_request
        def selected_proxy():
            value = original_proxy();proxies.append(value);return value
        monkeypatch.setattr(route, 'proxy_for_request', selected_proxy)
        uploader = None

        async def ingest(request):
            packet = await request.json();received.append(packet['kind'])
            if packet['kind'] == 'media':
                enqueue_controls(uploader)
                await asyncio.sleep(.15)  # Longer than the frame's remaining .05s budget.
            else:
                asyncio.get_running_loop().call_later(.01, uploader.stop_event.set)
            return web.json_response({'accepted': True})

        app=web.Application();app.router.add_post('/api/v1/ingest',ingest)
        runner=web.AppRunner(app);await runner.setup()
        try:
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            port=site._server.sockets[0].getsockname()[1]
            uploader=Uploader(Config('isolated-robot','isolated-source',
                cloud_url='http://127.0.0.1:'+str(port),allow_insecure_local=True,
                token='isolated-token-'+'x'*32,media_keys=('registered',)))
            uploader.queue.put('media:registered',envelope(cloud(age=1.95),kind='media'))
            async def no_pause(_seconds):await asyncio.sleep(0)
            monkeypatch.setattr(uploader,'_pause',no_pause)
            await asyncio.wait_for(uploader.run(),2)
            assert received[:2]==['media','heartbeat']
            assert proxies and all(proxy is None for proxy in proxies)
            assert uploader.diagnostic_snapshot()['streams']['heartbeat']['accepted']>=1
            assert uploader.diagnostic_snapshot()['streams']['media:registered']['last_result']=='transport_error'
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['events', 'test_metrics'])
def test_timeout_then_control_duplicate_does_not_ack_unsent_replacement(monkeypatch, kind):
    async def scenario():
        uploader = uploader_for_test()
        acknowledged, calls, pauses = [], [], []
        if kind == 'events':
            uploader.enable_events(acknowledged.extend)
        else:
            uploader.enable_test_metrics(acknowledged.extend, lambda *_args: None)
        payload, identifier = durable_payload(kind)
        uploader.enqueue(kind, '/observer/'+kind, payload)

        class Response:
            def __init__(self, number):
                self.number = number
                self.status = 409 if number == 2 else 200
                self.content = self

            async def read(self, _limit):
                if self.number == 2:
                    assert acknowledged == []
                    return b'{"reason":"duplicate","expected_sequence":2}'
                return b'{"accepted":true}'

        class Attempt:
            def __init__(self, number): self.number = number

            async def __aenter__(self):
                if self.number == 1:
                    enqueue_controls(uploader)
                    raise asyncio.TimeoutError()  # Durable batch committed; response lost.
                return Response(self.number)

            async def __aexit__(self, *_args):
                if self.number == 4:
                    uploader.stop_event.set()

        class Session:
            def __init__(self, **kwargs):
                self.connector = kwargs['connector']
                assert self.connector.limit == 1

            async def __aenter__(self): return self
            async def __aexit__(self, *_args): await self.connector.close()

            def post(self, _url, **kwargs):
                calls.append(json.loads(kwargs['data']))
                return Attempt(len(calls))

        async def pause(seconds):
            pauses.append(seconds)
            await asyncio.sleep(0)

        monkeypatch.setattr(aiohttp, 'ClientSession', Session)
        monkeypatch.setattr(uploader, '_pause', pause)
        await asyncio.wait_for(uploader.run(), 2)
        assert [packet['kind'] for packet in calls] == [kind, 'heartbeat', 'observation', kind]
        assert [packet['sequence'] for packet in calls] == [1, 1, 2, 3]
        assert len({packet['session_id'] for packet in calls}) == 1
        assert acknowledged == [identifier]
        assert max(pauses) <= 3
        assert uploader.diagnostic_snapshot()['streams']['heartbeat']['last_result'] == 'duplicate_checkpoint'
    asyncio.run(scenario())


def test_fresh_transport_does_not_refresh_old_observation_in_real_cloud_store(tmp_path):
    now = time.time()
    uploader = uploader_for_test()
    config = NS(robot_id='isolated-robot', data_dir=tmp_path,
                device_credentials=[NS(robot_id='isolated-robot', source_id='isolated-source', role='primary')],
                heartbeat_timeout_s=10, observation_timeout_s=5, upstream_timeout_s=2)
    store = Store(config, clock=lambda: now)
    try:
        uploader.enqueue('observation', '/observer/observation', observation(), collector_timestamp=now-20)
        first = uploader.queue.peek()
        store.ingest(dict(first.envelope, sequence=1))
        uploader.queue.remove(first)
        assert store.cloud_status()['status'] == 'OFFLINE'
        uploader.enqueue('observation', '/observer/observation', observation(), collector_timestamp=now-12)
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 1}, collector_timestamp=now)
        for sequence in (2, 3):
            packet = uploader.queue.next_upload()
            store.ingest(dict(packet.envelope, sequence=sequence))
            uploader.queue.remove(packet)
        assert store.source_online('isolated-source') is True
        assert store.cloud_status()['status'] == 'STALE'
        assert store.cloud_status()['ageS'] == pytest.approx(12)
        uploader.enqueue('observation', '/observer/observation', observation(), collector_timestamp=now)
        store.ingest(dict(uploader.queue.next_upload().envelope, sequence=4))
        assert store.cloud_status()['status'] == 'AVAILABLE'
    finally:
        store.close()
