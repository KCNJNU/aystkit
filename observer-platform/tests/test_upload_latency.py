"""Pending-turn fairness, real freshness bounds and ambiguous HTTP acknowledgements."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace as NS

import pytest
from aiohttp import web

from collector.config import Config
from collector.media import MediaStore
from collector.transport import LatestQueue, UploadDiagnostics, Uploader


def envelope(payload=None, *, kind='heartbeat', stamp=None, collected=None):
    return {'schema_version': 1, 'robot_id': 'isolated-robot', 'source_id': 'isolated-source',
            'session_id': str(uuid.uuid4()), 'sequence': 1, 'kind': kind,
            'topic': '/isolated/cloud' if kind == 'media' else '/observer/heartbeat',
            'message_timestamp': stamp, 'collector_timestamp': time.time() if collected is None else collected,
            'payload': {'uptime_s': 0} if payload is None else payload}


def cloud(*, age=0.0, frame=1):
    return {'key': 'registered', 'kind': 'cloud', 'status': 'AVAILABLE', 'ageS': age,
            'frameSeq': frame, 'points': [[0.0, 0.0, 0.0]]}


def test_replacements_keep_pending_turn_and_inflight_ack_yields_to_other_keys():
    now = [0.0]
    queue = LatestQueue(clock=lambda: now[0])
    queue.put('first', envelope())
    queue.put('second', envelope())
    for step in range(1, 10):
        now[0] = step * 0.1
        queue.put('first', envelope({'uptime_s': step}))
        assert list(queue.entries) == ['first', 'second']
    inflight = queue.peek()
    assert inflight.queued_since == 0.0
    queue.put('first', envelope({'uptime_s': 10}))
    queue.remove(inflight)
    assert list(queue.entries) == ['second', 'first']
    assert queue.entries['first'].envelope['payload']['uptime_s'] == 10
    assert queue.entries['first'].queued_since == now[0]
    queue.remove(queue.peek())
    assert queue.peek().key == 'first'


def test_replacing_first_key_with_larger_payload_preserves_bounds_and_key():
    queue = LatestQueue(max_keys=2, max_bytes=1500)
    queue.put('first', envelope())
    queue.put('second', envelope())
    larger = envelope({'key': 'registered', 'kind': 'cloud', 'status': 'ERROR', 'reason': 'x' * 700}, kind='media')
    queue.put('first', larger)
    assert queue.stats()['bytes'] <= queue.max_bytes
    assert queue.entries['first'].envelope['payload']['reason'] == 'x' * 700
    assert queue.stats()['bytes'] == sum(entry.size for entry in queue.entries.values())


@pytest.mark.parametrize('age,elapsed,expected', [(1.5, 0.49, True), (1.5, 0.51, False),
                                                 (0.0, 2.01, False), (2.01, 0.0, False)])
def test_live_media_uses_remaining_two_second_budget(age, elapsed, expected):
    now, wall = [0.0], time.time()
    queue = LatestQueue(clock=lambda: now[0], wall_clock=lambda: wall)
    queue.put('media:registered', envelope(cloud(age=age), kind='media', collected=wall))
    now[0] = elapsed
    assert (queue.peek() is not None) is expected
    assert queue.stats()['expired_live'] == int(not expected)


@pytest.mark.parametrize('field', ['message_timestamp', 'received_timestamp', 'collector_timestamp'])
def test_live_media_also_accounts_for_known_wall_clock_age(field):
    wall = time.time()
    payload = cloud()
    kwargs = {'collected': wall}
    if field == 'message_timestamp':
        kwargs['stamp'] = wall - 3
    elif field == 'collector_timestamp':
        kwargs['collected'] = wall - 3
    else:
        payload[field] = wall - 3
    queue = LatestQueue(clock=lambda: 0.0, wall_clock=lambda: wall)
    queue.put('media:registered', envelope(payload, kind='media', **kwargs))
    assert queue.peek() is None and queue.stats()['expired_live'] == 1


def test_future_clock_cannot_extend_source_age_budget():
    wall, now = time.time(), [0.0]
    queue = LatestQueue(clock=lambda: now[0], wall_clock=lambda: wall)
    queue.put('media:registered', envelope(cloud(age=1.8), kind='media', collected=wall + 30))
    now[0] = 0.21
    assert queue.peek() is None


def test_waiting_status_and_static_maps_do_not_use_live_media_expiry():
    now = [0.0]
    queue = LatestQueue(clock=lambda: now[0])
    queue.put('waiting', envelope({'key': 'registered', 'kind': 'cloud', 'status': 'WAITING_UPSTREAM'}, kind='media'))
    queue.put('map', envelope({'key': 'map', 'kind': 'map', 'status': 'STALE', 'ageS': 100}, kind='media'))
    now[0] = 3
    assert queue.peek().key == 'waiting'
    assert queue.stats()['expired_live'] == 0
    now[0] = 11
    assert queue.peek().key == 'map'
    now[0] = 41
    assert queue.peek() is None


def test_unavailable_media_changes_and_reconnect_publish_without_repeated_age_posts():
    now, sent = [0.0], []
    media = MediaStore(Config('isolated-robot', 'isolated-source', media_keys=('fisheye',)), lambda: now[0])
    uploader = NS(connection_generation=0, enqueue=lambda *values: sent.append(values))
    sensors = {'fisheye': {'topic': '/isolated/image'}}
    media.publish(uploader, sensors)
    assert len(sent) == 1 and sent[-1][2]['status'] == 'WAITING_UPSTREAM'
    now[0] = 5.1
    media.publish(uploader, sensors)
    assert len(sent) == 1
    now[0] = 30.0
    media.publish(uploader, sensors)
    assert len(sent) == 2
    media.errors['fisheye'] = 'DecodeError: first'
    media.dirty.add('fisheye')
    media.publish(uploader, sensors)
    assert len(sent) == 3 and sent[-1][2]['status'] == 'ERROR'
    media.errors['fisheye'] = 'DecodeError: second'
    media.dirty.add('fisheye')
    media.publish(uploader, sensors)
    assert len(sent) == 4 and sent[-1][2]['reason'].endswith('second')
    uploader.connection_generation += 1
    media.publish(uploader, sensors)
    assert len(sent) == 5


def test_upload_diagnostics_are_bounded_aggregates_without_payload_or_credentials():
    queue = LatestQueue()
    queue.put('heartbeat', envelope())
    entry = queue.peek()
    diagnostics = UploadDiagnostics(('registered',))
    for index in range(100):
        diagnostics.record(entry, queue_s=index / 1000, post_s=0.2,
                           age_on_finish_s=None, body_bytes=42, result='accepted')
    result = diagnostics.snapshot()['heartbeat']
    assert result['attempts'] == result['accepted'] == 100
    assert result['bytes'] == 4200 and result['post_ms']['p95'] == 200.0
    assert len(diagnostics.streams['heartbeat']['queue_ms']) == 64
    assert set(diagnostics.streams) == {'heartbeat', 'observation', 'media:registered'}
    assert 'isolated' not in json.dumps(diagnostics.snapshot())


def test_lost_ack_does_not_discard_new_frame_reusing_unconfirmed_sequence():
    async def scenario():
        received, uploader = [], None
        async def handler(request):
            value = await request.json()
            received.append(value)
            if len(received) == 1:
                # The first frame was committed. A newer frame replaces it
                # before the lost-response retry reaches the server.
                uploader.enqueue('media', '/isolated/cloud', cloud(frame=2))
                request.transport.close()
                return web.Response()
            if value['sequence'] == 1:
                return web.json_response({'reason': 'duplicate', 'expected_sequence': 2}, status=409)
            await asyncio.sleep(0.05)
            return web.json_response({'accepted': True})

        app = web.Application()
        app.router.add_post('/api/v1/ingest', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        uploader = Uploader(Config('isolated-robot', 'isolated-source',
                                   cloud_url=f'http://127.0.0.1:{port}', token='test-only-' + 'x' * 32,
                                   allow_insecure_local=True, media_keys=('registered',)))
        uploader.enqueue('media', '/isolated/cloud', cloud(frame=1))
        task = asyncio.create_task(uploader.run())
        try:
            deadline = time.monotonic() + 4
            while uploader.sequence < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert uploader.sequence == 2
            assert [value['sequence'] for value in received] == [1, 1, 2]
            assert [value['payload']['frameSeq'] for value in received] == [1, 2, 2]
            assert uploader.queue.stats()['keys'] == 0
            timing = uploader.diagnostic_snapshot()['streams']['media:registered']
            assert timing['attempts'] == 3 and timing['post_ms']['max'] >= 45
            # A checkpoint for the previous frame does not acknowledge the
            # replacement payload; only the final HTTP 200 does so.
            assert timing['accepted'] == 1
            assert timing['age_on_finish_ms']['max'] >= timing['post_ms']['max']
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())
