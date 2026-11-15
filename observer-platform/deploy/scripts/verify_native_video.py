#!/usr/bin/env python3
"""Authenticated real native video transport check, run only inside ECS.

No pixels or credentials are printed/saved. ECS-loopback acceptance does not
measure a remote viewer's WAN playback or decoder performance.
"""
import argparse
import asyncio
import json
import socket
import ssl
import sys

import aiohttp

from verify_live import COOKIE, CREDENTIALS, ORIGIN, VerificationError, credentials, read_json, report, require

KEYS = ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3')


class LoopbackResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        require(host == 'aystkit.cn', 'native_resolver_hostname')
        return [{'hostname': host, 'host': '127.0.0.1', 'port': port,
                 'family': socket.AF_INET, 'proto': socket.IPPROTO_TCP,
                 'flags': socket.AI_NUMERICHOST}]

    async def close(self):
        pass


async def video(session, key):
    async with session.get(ORIGIN + '/native-camera/' + key + '/video.mp4', allow_redirects=False) as response:
        require(response.status == 200 and response.content_type == 'video/mp4', 'native_video_response')
        require(response.headers.get('Cache-Control') == 'no-store'
                and response.headers.get('X-Content-Type-Options') == 'nosniff', 'native_stream_headers')
        boxes, fragments, pixels, total = [], 0, 0, 0
        while pixels < 3:
            header = await response.content.readexactly(8)
            size, kind = int.from_bytes(header[:4], 'big'), header[4:]
            header_size = 8
            if size == 1:
                size = int.from_bytes(await response.content.readexactly(8), 'big')
                header_size = 16
            require(header_size <= size <= 16 * 1024 * 1024, 'native_mp4_box_size')
            require(kind in (b'ftyp', b'moov', b'moof', b'mdat', b'free', b'skip', b'wide', b'styp', b'sidx'),
                    'native_mp4_box_type')
            require(boxes or kind == b'ftyp', 'native_mp4_initial_type')
            remain = size - header_size
            while remain:
                chunk = min(remain, 65536)
                await response.content.readexactly(chunk)
                remain -= chunk
            total += size
            require(total <= 32 * 1024 * 1024 and len(boxes) < 64, 'native_mp4_budget')
            boxes.append(kind.decode('ascii'))
            if kind == b'moof':
                fragments += 1
            if kind == b'mdat':
                pixels += 1
        require('moov' in boxes and fragments >= 3, 'native_mp4_fragments')
        report('native_transport:' + key, status='PASS', fragments=fragments, bytes=total)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--web-max-side', type=int, choices=(640,))
    parser.add_argument('--web-fps', type=int, choices=(10,))
    parser.add_argument('--require-usage', action='store_true')
    args = parser.parse_args()
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(), resolver=LoopbackResolver())
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=30, sock_read=12),
            headers={'Origin': ORIGIN}, cookie_jar=aiohttp.CookieJar()) as session:
        for path in ('/native-camera/status', *(f'/native-camera/{key}/video.mp4' for key in KEYS)):
            async with session.get(ORIGIN + path, allow_redirects=False) as response:
                require(response.status == 401, 'native_anonymous_denied')
        login = credentials(CREDENTIALS)
        issued = None
        try:
            async with session.post(ORIGIN + '/api/login', json=login, allow_redirects=False) as response:
                require(response.status == 200, 'native_login')
                issued = response.cookies[COOKIE].value
        finally:
            login.clear()
        try:
            async with session.get(ORIGIN + '/native-camera/status') as response:
                require(response.status == 200, 'native_status_response')
                first = await read_json(response)
            require(first.get('enabled') is True and first.get('maxPlaying') == 4, 'native_four_enabled')
            require(set(first.get('cameras', {})) == set(KEYS), 'native_keys')
            await asyncio.wait_for(asyncio.gather(*(video(session, key) for key in KEYS)), 25)
            await asyncio.sleep(1)
            async with session.get(ORIGIN + '/native-camera/status') as response:
                second = await read_json(response)
            for key in KEYS:
                before, after = first['cameras'][key], second['cameras'][key]
                require(after['frames'] > before['frames'] and after['encoded_frames'] > before['encoded_frames'],
                        'native_source_progress')
                require(after['error'] is None and after['video_error'] is None, 'native_source_error')
                expected = [2592, 1944] if key in ('usbFront', 'usbLeft') else [1920, 1080]
                if args.web_max_side:
                    expected = [640, 480] if key in ('usbFront', 'usbLeft') else [640, 360]
                require([after['width'], after['height']] == expected, 'native_source_dimensions')
                if args.web_fps:
                    require(after['requested_fps'] == args.web_fps, 'native_web_fps')
                if args.require_usage:
                    usage = second.get('cameraUsage', {}).get(key, {})
                    require(usage.get('connected') is True and usage.get('owner') == 'WEB'
                            and isinstance(usage.get('checkedAgeMs'), (int, float))
                            and usage['checkedAgeMs'] < 6000, 'native_connected_shared_owner')
                report('native_source:' + key, status='PASS', width=after['width'], height=after['height'],
                       captureFps=round(after['actual_fps'], 2), encodedFps=round(after['encoded_fps'], 2),
                       sourceFramesAdvanced=True)
            async with session.ws_connect('wss://aystkit.cn/ws', origin=ORIGIN,
                    autoping=False, heartbeat=None, max_msg_size=2 * 1024 * 1024) as ws:
                message = await asyncio.wait_for(ws.receive(), 10)
                require(message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data)['meta']['readOnly'],
                        'native_telemetry_read_only')
        finally:
            async with session.post(ORIGIN + '/api/logout') as response:
                require(response.status == 200, 'native_logout')
            session.cookie_jar.clear()
            async with session.get(ORIGIN + '/native-camera/status', cookies={COOKIE: issued}) as response:
                require(response.status == 401, 'native_revoked_session')
        report('native_verification', status='PASS', scope='ECS_LOOPBACK_TLS_NOT_VIEWER_WAN',
               pixelsSaved=False, remoteBrowserAcceptance=False)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except VerificationError as error:
        report('native_verification', status='FAIL', reason=str(error))
        sys.exit(1)
    except Exception as error:
        # Never print response bodies, credentials or arbitrary exception text.
        report('native_verification', status='FAIL', exception=type(error).__name__)
        sys.exit(1)
