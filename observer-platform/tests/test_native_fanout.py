"""Fixed-quality fanout uses isolated sources; never opens production devices."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from cloud.backend.app import ACCOUNTS, SESSIONS
from cloud.backend.native_video import (
    Fragment, Subscriber, MAX_PENDING_BYTES, MAX_PENDING_FRAGMENTS, NATIVE_VIDEO,
    VideoHub, _children, _next_box, fragment_random_access, mp4_initialization,
    stream_clock,
)
from tests import test_native_video as fixtures


def split_boxes(data):
    """Preserve exact box bytes including offsets/timestamps for decoder checks."""
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('truncated fixture box')
        size = int.from_bytes(data[offset:offset + 4], 'big')
        if size == 1:
            size = int.from_bytes(data[offset + 8:offset + 16], 'big')
        if size < 8 or offset + size > len(data):
            raise ValueError('truncated fixture box body')
        yield data[offset + 4:offset + 8], data[offset:offset + size]
        offset += size


class FixedFanoutTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the original Unix fixture plumbing, not its test methods.
    new_client = fixtures.NativeVideoTests.new_client
    login = fixtures.NativeVideoTests.login
    upstream_status = fixtures.NativeVideoTests.upstream_status
    stream = fixtures.NativeVideoTests.stream
    ended = fixtures.NativeVideoTests.ended
    asyncTearDown = fixtures.NativeVideoTests.asyncTearDown

    async def asyncSetUp(self):
        self.video_schedule = None
        self.video_session = 'a' * 32
        self.video_entered = asyncio.Event()
        self.active_sources = 0
        await fixtures.NativeVideoTests.asyncSetUp(self)

    async def upstream_video(self, request):
        self.seen.append((request.path_qs, dict(request.headers)))
        self.active_sources += 1
        response = web.StreamResponse(headers={'Content-Type': 'video/mp4',
                                               'X-Video-Session': self.video_session})
        await response.prepare(request)
        self.video_entered.set()
        try:
            await response.write(self.initial)
            if self.video_schedule is not None:
                while True:
                    data = await asyncio.wait_for(self.video_schedule.get(), 3)
                    if data is None:
                        break
                    await response.write(data)
            else:
                await response.write(fixtures.FRAGMENT)
                while self.keep_streaming and not request.transport.is_closing():
                    await asyncio.sleep(.05)
                    await response.write(fixtures.FRAGMENT)
            await response.write_eof()
        except (ConnectionError, RuntimeError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            self.active_sources -= 1
        return response

    def cookie_headers(self, cookie=None):
        cookie = cookie or self.app[SESSIONS].issue('viewer')
        return {'Cookie': 'observer_session=' + cookie}

    async def until(self, condition, seconds=2):
        deadline = stream_clock() + seconds
        while not condition():
            if stream_clock() >= deadline:
                self.fail('condition did not become true')
            await asyncio.sleep(.02)

    async def test_two_sessions_four_cameras_use_four_sources_eight_subscribers(self):
        self.keep_streaming = True
        for _ in range(2):
            headers = self.cookie_headers()
            for key in ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3'):
                response = await self.stream(key, headers=headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['X-Video-Session'], self.video_session)
                self.assertEqual(await response.content.readexactly(len(fixtures.INITIAL)), fixtures.INITIAL)
        fanout = self.app[NATIVE_VIDEO].fanout_status()
        self.assertEqual((fanout['upstreams'], fanout['subscribers']), (4, 8))
        self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 4)
        self.assertTrue(all(row['upstreams'] == 1 and row['subscribers'] == 2
                            for row in fanout['cameras'].values()))
        self.assertGreater(fanout['upstreamBytes'], 0)
        self.assertNotIn('nonce', json.dumps(fanout))
        await self.login()
        status = await (await self.client.get('/native-camera/status')).json()
        self.assertEqual(status['fanout']['subscribers'], 8)

    async def test_split_sockets_share_one_source_and_preserve_four_per_camera_limit(self):
        for key in ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3'):
            await web.UnixSite(self.runner, self.temp.name + '/jetson-' + key + '.sock').start()
        app, client = await self.new_client(split_sockets=True)
        self.extra_clients.append(client)
        self.keep_streaming = True
        for _ in range(4):
            cookie = app[SESSIONS].issue('viewer')
            response = await self.stream(client=client, headers=self.cookie_headers(cookie))
            self.assertEqual(response.status, 200)
        native = app[NATIVE_VIDEO]
        self.assertEqual(native.fanout_status()['upstreams'], 1)
        self.assertEqual(native.fanout_status()['subscribers'], 4)
        self.assertEqual(native.video_clients['usbFront'].connector.limit, 1)
        response = await self.stream(client=client,
            headers=self.cookie_headers(app[SESSIONS].issue('viewer')))
        self.assertEqual(response.status, 429)
        self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 1)

    async def test_pending_user_cannot_join_shared_camera_or_read_statistics(self):
        self.keep_streaming = True
        response = await self.stream(headers=self.cookie_headers())
        self.assertEqual(response.status, 200)
        account = self.app[ACCOUNTS].register('isolated.pending', 'unused-test-hash')
        cookie = self.app[SESSIONS].issue('isolated.pending', account_id=account['id'])
        before = len(self.seen)
        for path in ('/native-camera/status', '/native-camera/usbFront/video.mp4'):
            denied = await self.client.get(path, headers=self.cookie_headers(cookie))
            self.assertEqual(denied.status, 403)
        self.assertEqual(len(self.seen), before)
        self.assertEqual(self.app[NATIVE_VIDEO].fanout_status()['subscribers'], 1)

    async def test_permission_removal_ends_only_affected_subscriber(self):
        self.keep_streaming = True
        account = self.app[ACCOUNTS].register('isolated.approved', 'unused-test-hash')
        self.app[ACCOUNTS].approve(account['id'], 'viewer')
        cookie = self.app[SESSIONS].issue('isolated.approved', account_id=account['id'])
        first = await self.stream(headers=self.cookie_headers(cookie))
        second = await self.stream(headers=self.cookie_headers())
        self.assertEqual((first.status, second.status), (200, 200))
        with self.app[ACCOUNTS].db:
            self.app[ACCOUNTS].db.execute("UPDATE users SET status='pending' WHERE id=?", (account['id'],))
        await self.ended(first)
        await self.until(lambda: self.app[NATIVE_VIDEO].fanout_status()['subscribers'] == 1)
        self.assertEqual((await self.stream(headers=self.cookie_headers(cookie))).status, 403)
        self.assertEqual(await second.content.readexactly(len(fixtures.INITIAL)), fixtures.INITIAL)
        self.assertEqual(await second.content.readexactly(len(fixtures.FRAGMENT)), fixtures.FRAGMENT)
        self.assertTrue(self.app[NATIVE_VIDEO].hubs['usbFront'].accepting)

    async def test_late_join_starts_at_latest_idr_preserving_all_pts_and_session(self):
        self.video_schedule = asyncio.Queue()
        fragments = [fixtures.fragment(True, b'OLD_IDR', 0),
                     fixtures.fragment(False, b'OLD_DELTA', 200),
                     fixtures.fragment(True, b'LATEST_IDR', 400),
                     fixtures.fragment(False, b'LATEST_DELTA', 600)]
        self.video_schedule.put_nowait(fragments[0])
        self.video_schedule.put_nowait(fragments[1])
        first = await self.stream(headers=self.cookie_headers())
        size = len(fixtures.INITIAL + b''.join(fragments[:2]))
        self.assertEqual(await first.content.readexactly(size), fixtures.INITIAL + b''.join(fragments[:2]))
        for data in fragments[2:]:
            self.video_schedule.put_nowait(data)
        size = len(b''.join(fragments[2:]))
        self.assertEqual(await first.content.readexactly(size), b''.join(fragments[2:]))
        second = await self.stream(headers=self.cookie_headers())
        expected = fixtures.INITIAL + b''.join(fragments[2:])
        self.assertEqual(await second.content.readexactly(len(expected)), expected)
        self.assertEqual(second.headers['X-Video-Session'], self.video_session)
        self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 1)
        self.video_schedule.put_nowait(None)

    async def test_delta_fragment_never_bootstraps_new_viewer(self):
        self.video_schedule = asyncio.Queue()
        self.video_schedule.put_nowait(fixtures.fragment(False, b'UNDECODABLE', 0))
        task = asyncio.create_task(self.stream(headers=self.cookie_headers()))
        await self.video_entered.wait()
        await asyncio.sleep(.05)
        self.assertFalse(task.done())
        fresh = fixtures.fragment(True, b'FRESH_IDR', 500)
        self.video_schedule.put_nowait(fresh)
        response = await asyncio.wait_for(task, 2)
        self.assertEqual(response.status, 200)
        expected = fixtures.INITIAL + fresh
        self.assertEqual(await response.content.readexactly(len(expected)), expected)
        self.video_schedule.put_nowait(None)

    async def test_last_subscriber_releases_source_even_when_encoder_stalls(self):
        self.video_schedule = asyncio.Queue()
        self.video_schedule.put_nowait(fixtures.FRAGMENT)
        response = await self.stream(headers=self.cookie_headers())
        hub = self.app[NATIVE_VIDEO].hubs['usbFront']
        response.close()
        await self.until(lambda: not self.app[NATIVE_VIDEO].streams and hub.task.done())
        self.assertTrue(hub.upstream.closed)
        self.assertEqual(self.app[NATIVE_VIDEO].hubs, {})
        self.assertEqual(hub.subscribers, set())
        self.video_schedule.put_nowait(None)

    async def test_revoke_one_session_cannot_close_another_shared_source(self):
        self.keep_streaming = True
        cookies = [self.app[SESSIONS].issue('viewer') for _ in range(2)]
        first = await self.stream(headers=self.cookie_headers(cookies[0]))
        second = await self.stream(headers=self.cookie_headers(cookies[1]))
        await second.content.readexactly(len(fixtures.INITIAL))
        nonce = self.app[SESSIONS].read(cookies[0])['n']
        self.app[SESSIONS].revoke(cookies[0])
        self.app[NATIVE_VIDEO].revoke(nonce)
        await self.ended(first)
        await self.until(lambda: self.app[NATIVE_VIDEO].fanout_status()['subscribers'] == 1)
        self.assertEqual(await second.content.readexactly(len(fixtures.FRAGMENT)), fixtures.FRAGMENT)
        self.assertTrue(self.app[NATIVE_VIDEO].hubs['usbFront'].accepting)
        self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 1)

    async def test_revoke_during_initial_wait_is_401_not_503(self):
        self.video_schedule = asyncio.Queue()
        cookie = self.app[SESSIONS].issue('viewer')
        task = asyncio.create_task(self.stream(headers=self.cookie_headers(cookie)))
        await self.video_entered.wait()
        nonce = self.app[SESSIONS].read(cookie)['n']
        self.app[SESSIONS].revoke(cookie)
        self.app[NATIVE_VIDEO].revoke(nonce)
        response = await asyncio.wait_for(task, 2)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})
        self.video_schedule.put_nowait(None)

    async def test_invalid_fragment_before_admission_is_503_and_not_200_or_401(self):
        self.video_schedule = asyncio.Queue()
        self.video_schedule.put_nowait(fixtures.box(b'moof'))
        self.video_schedule.put_nowait(fixtures.box(b'mdat', b'bad'))
        response = await self.stream(headers=self.cookie_headers())
        self.assertEqual(response.status, 503)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})
        self.video_schedule.put_nowait(None)

    async def test_encoder_restart_never_reuses_previous_session_or_cache(self):
        cookie = self.app[SESSIONS].issue('viewer')
        self.keep_streaming = True
        first = await self.stream(headers=self.cookie_headers(cookie))
        hub = self.app[NATIVE_VIDEO].hubs['usbFront']
        self.keep_streaming = False
        await self.ended(first)
        self.video_session = 'b' * 32
        self.keep_streaming = True
        second = await self.stream(headers=self.cookie_headers(cookie))
        self.assertEqual(second.status, 200)
        self.assertEqual(second.headers['X-Video-Session'], 'b' * 32)
        self.assertIsNot(self.app[NATIVE_VIDEO].hubs['usbFront'], hub)
        self.assertEqual(list(hub.cache), [])
        self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 2)

    async def test_slow_writer_queue_overflow_only_disconnects_its_subscriber(self):
        self.keep_streaming = True
        blocked, release = asyncio.Event(), asyncio.Event()
        first_response = [None]
        original_write = web.StreamResponse.write

        async def write(response, data):
            if response.headers.get('X-Accel-Buffering') == 'no':
                if first_response[0] is None:
                    first_response[0] = response
                if response is first_response[0] and data[4:8] == b'moof':
                    blocked.set()
                    await release.wait()
            return await original_write(response, data)

        try:
            with patch.object(web.StreamResponse, 'write', write), patch(
                    'cloud.backend.native_video.MAX_PENDING_FRAGMENTS', 2):
                first = await self.stream(headers=self.cookie_headers())
                await asyncio.wait_for(blocked.wait(), 2)
                second = await self.stream(headers=self.cookie_headers())
                self.assertEqual(second.status, 200)
                await asyncio.wait_for(second.content.readexactly(
                    len(fixtures.INITIAL) + len(fixtures.FRAGMENT) * 8), 2)
                await self.ended(first)
                self.assertEqual(self.app[NATIVE_VIDEO].fanout_status()['slowDisconnects'], 1)
                self.assertEqual(self.app[NATIVE_VIDEO].fanout_status()['subscribers'], 1)
                self.assertEqual(await second.content.readexactly(len(fixtures.FRAGMENT)), fixtures.FRAGMENT)
                self.assertTrue(self.app[NATIVE_VIDEO].hubs['usbFront'].accepting)
                self.assertEqual(sum('/video.mp4' in path for path, _ in self.seen), 1)
        finally:
            release.set()

    async def test_slow_queue_aborts_only_slow_prepared_transport(self):
        self.keep_streaming = True
        blocked, release = asyncio.Event(), asyncio.Event()
        slow_response = [None]
        original_write = web.StreamResponse.write
        cookies = [self.app[SESSIONS].issue('viewer') for _ in range(2)]

        async def write(response, data):
            if response.headers.get('X-Accel-Buffering') == 'no':
                if slow_response[0] is None:
                    slow_response[0] = response
                if response is slow_response[0] and data[4:8] == b'moof':
                    blocked.set()
                    await release.wait()
            return await original_write(response, data)

        try:
            with patch.object(web.StreamResponse, 'write', write), patch(
                    'cloud.backend.native_video.MAX_PENDING_FRAGMENTS', 3):
                first = await self.stream(headers=self.cookie_headers(cookies[0]))
                await asyncio.wait_for(blocked.wait(), 2)
                second = await self.stream(headers=self.cookie_headers(cookies[1]))
                native = self.app[NATIVE_VIDEO]
                nonces = [self.app[SESSIONS].read(cookie)['n'] for cookie in cookies]
                slow_transport = native.streams[nonces[0]]['usbFront'].transport
                fast_transport = native.streams[nonces[1]]['usbFront'].transport
                with patch.object(slow_transport, 'abort', wraps=slow_transport.abort) as slow_abort, patch.object(
                        fast_transport, 'abort', wraps=fast_transport.abort) as fast_abort:
                    await self.ended(first)
                    await asyncio.wait_for(second.content.readexactly(
                        len(fixtures.INITIAL) + len(fixtures.FRAGMENT) * 8), 2)
                    self.assertEqual(slow_abort.call_count, 1)
                    fast_abort.assert_not_called()
                    self.assertTrue(slow_transport.is_closing())
                    self.assertFalse(fast_transport.is_closing())
                self.assertEqual(native.fanout_status()['subscribers'], 1)
                self.assertEqual(native.fanout_status()['upstreams'], 1)
        finally:
            release.set()

    async def test_write_deadline_only_closes_slow_subscriber_without_waiting_for_queue(self):
        self.keep_streaming = True
        first_response = [None]
        original_write = web.StreamResponse.write

        async def write(response, data):
            if response.headers.get('X-Accel-Buffering') == 'no':
                if first_response[0] is None:
                    first_response[0] = response
                if response is first_response[0] and data[4:8] == b'moof':
                    await asyncio.Event().wait()
            return await original_write(response, data)

        with patch.object(web.StreamResponse, 'write', write), patch(
                'cloud.backend.native_video.WRITE_TIMEOUT', .12):
            first = await self.stream(headers=self.cookie_headers())
            second = await self.stream(headers=self.cookie_headers())
            self.assertEqual(second.status, 200)
            await self.ended(first)
            await self.until(lambda: self.app[NATIVE_VIDEO].fanout_status()['subscribers'] == 1)
            self.assertEqual(self.app[NATIVE_VIDEO].fanout_status()['slowDisconnects'], 1)
            self.assertEqual(await second.content.readexactly(len(fixtures.INITIAL)), fixtures.INITIAL)
            self.assertEqual(await second.content.readexactly(len(fixtures.FRAGMENT)), fixtures.FRAGMENT)

    async def test_same_session_reconnect_can_replace_closing_transport_without_losing_new_slot(self):
        self.keep_streaming = True
        cookie = self.app[SESSIONS].issue('viewer')
        first = await self.stream(headers=self.cookie_headers(cookie))
        native = self.app[NATIVE_VIDEO]
        old = next(iter(native.streams.values()))['usbFront']
        old.transport.close()  # Server observes a closed connection before finally ran.
        second = await self.stream(headers=self.cookie_headers(cookie))
        self.assertEqual(second.status, 200)
        await self.until(lambda: old.subscriber not in old.hub.subscribers)
        self.assertEqual(len(native.streams), 1)
        self.assertIsNot(next(iter(native.streams.values()))['usbFront'], old)
        self.assertEqual((await self.stream(headers=self.cookie_headers(cookie))).status, 429)
        self.assertEqual(await second.content.readexactly(len(fixtures.INITIAL)), fixtures.INITIAL)
        first.close()

    async def test_old_hub_cleanup_cannot_delete_replacement_hub(self):
        self.keep_streaming = True
        first = await self.stream(headers=self.cookie_headers())
        native = self.app[NATIVE_VIDEO]
        old_hub = native.hubs['usbFront']
        old_hub.stop()
        second = await self.stream(headers=self.cookie_headers())
        self.assertEqual(second.status, 200)
        new_hub = native.hubs['usbFront']
        native._hub_closed(old_hub)
        self.assertIs(native.hubs['usbFront'], new_hub)
        self.assertIsNot(new_hub, old_hub)
        first.close()


class SubscriberBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_limits_count_bytes_and_drop_only_slow_queue(self):
        calls = []
        slow, fast = Subscriber(lambda: calls.append('slow')), Subscriber()
        for index in range(MAX_PENDING_FRAGMENTS + 1):
            item = Fragment(b'x', True, stream_clock())
            slow.offer(item)
            fast.offer(item)
            self.assertIs(await fast.get(), item)
        self.assertTrue(slow.failed)
        self.assertFalse(fast.failed)
        self.assertEqual(slow.pending_bytes, 0)
        self.assertEqual(await slow.get(), None)
        self.assertEqual(calls, ['slow'])
        large = Subscriber()
        large.offer(Fragment(b'x' * (MAX_PENDING_BYTES + 1), True, stream_clock()))
        self.assertTrue(large.failed)
        self.assertEqual(large.pending_bytes, 0)

    async def test_stale_queue_is_rejected_instead_of_playing_old_frames(self):
        subscriber = Subscriber()
        subscriber.offer(Fragment(b'x', True, stream_clock() - 2.01))
        with self.assertRaises(RuntimeError):
            await subscriber.get()
        self.assertTrue(subscriber.failed)

    async def test_hub_cache_is_bounded_and_new_viewer_waits_after_overflow(self):
        # Exercise cache bookkeeping without opening even an isolated source.
        hub = object.__new__(VideoHub)
        from collections import deque
        hub.cache, hub.cache_bytes, hub.subscribers = deque(), 0, set()
        hub.accepting = True
        hub.publish(Fragment(b'idr', True, 0))
        hub.publish(Fragment(b'delta', False, 2.1))
        self.assertEqual(list(hub.cache), [])
        self.assertEqual(hub.cache_bytes, 0)
        newcomer = hub.subscribe()
        self.assertFalse(newcomer.ready.is_set())
        hub.publish(Fragment(b'new', True, stream_clock()))
        self.assertTrue(newcomer.ready.is_set())
        self.assertEqual((await newcomer.get()).data, b'new')
        with patch('cloud.backend.native_video.MAX_GOP_BYTES', 3):
            hub.publish(Fragment(b'oversize', True, stream_clock()))
        self.assertEqual(list(hub.cache), [])
        self.assertEqual(hub.cache_bytes, 0)

    async def test_complete_box_parser_rejects_truncation_even_if_missing_body_is_empty(self):
        for raw in (b'\0\0\0\x10mdat', b'\0\0\0\x01mdat', b'\0\0'):
            content = asyncio.StreamReader()
            content.feed_data(raw)
            content.feed_eof()
            with self.assertRaises(asyncio.IncompleteReadError):
                await _next_box(content)
        content = asyncio.StreamReader()
        content.feed_eof()
        self.assertIsNone(await _next_box(content))


class NativeFragmentParserTests(unittest.TestCase):
    def test_flags_require_random_access_and_relative_addressing(self):
        key, _ = list(split_boxes(fixtures.fragment(True)))[0]
        self.assertEqual(key, b'moof')
        self.assertTrue(fragment_random_access(list(split_boxes(fixtures.fragment(True)))[0][1]))
        self.assertFalse(fragment_random_access(list(split_boxes(fixtures.fragment(False)))[0][1]))
        raw = list(split_boxes(fixtures.fragment(True)))[0][1]
        absolute = raw.replace(b'\0\x02\0\x20', b'\0\0\0\x21')
        with self.assertRaises(ValueError):
            fragment_random_access(absolute)
        for invalid in (fixtures.box(b'moof'), fixtures.box(b'mdat'), raw[:-1], raw + raw):
            with self.assertRaises(ValueError):
                fragment_random_access(invalid)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is needed for real decoder contract')
    def test_real_ffmpeg_fragments_bootstrap_from_latest_idr_and_decode(self):
        encoded = subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=160x96:rate=10', '-t', '2.2', '-c:v', 'libx264',
            '-preset', 'ultrafast', '-tune', 'zerolatency', '-g', '5', '-keyint_min', '5',
            '-sc_threshold', '0', '-bf', '0', '-movflags',
            '+empty_moov+frag_keyframe+default_base_moof', '-frag_duration', '200000',
            '-f', 'mp4', 'pipe:1'], capture_output=True, timeout=20, check=True).stdout
        initial, fragments, pending = bytearray(), [], None
        flags = []
        for kind, raw in split_boxes(encoded):
            if kind in (b'ftyp', b'moov'):
                initial.extend(raw)
            elif kind == b'moof':
                pending = raw
                flags.append(fragment_random_access(raw))
                if flags[-1]:
                    fragments = []
            elif kind == b'mdat':
                fragments.append(pending + raw)
                pending = None
        self.assertIn(True, flags)
        self.assertIn(False, flags)
        self.assertGreaterEqual(sum(flags), 4)
        expected = bytes(initial) + b''.join(fragments)
        decoded = subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
            input=expected, capture_output=True, timeout=20)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
        self.assertEqual(decoded.stderr, b'')
        self.assertGreaterEqual(len(decoded.stdout), 160 * 96 * 3)
        self.assertEqual(len(decoded.stdout) % (160 * 96 * 3), 0)
