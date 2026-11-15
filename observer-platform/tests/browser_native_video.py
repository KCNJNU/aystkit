"""Independent native-H264 Chromium QA; never uses production credentials.

The default makes a small synthetic fragmented MP4 and an ephemeral loopback
cloud with a Unix-socket upstream. ``--source-mode tunnel`` reads only the
existing 127.0.0.1:18082 preview through that same isolated Unix adapter. Such a
run verifies LAN video playback, not public-domain throughput or user WAN FPS.
Use a dedicated existing Chromium CDP port, serially with other browser tests.
"""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.request
import uuid
from unittest.mock import patch

import aiohttp
from aiohttp import web
import websockets

from cloud.backend.app import STORE, create_app
from cloud.backend.security import Config, hash_password
from tests.browser_cloud import local_test_url
from tests.test_protocol import envelope


KEYS = ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3')
CAMERAS = (('camera2', 'front'), ('camera1', 'rear'), ('camera4', 'right'), ('camera3', 'left'))
USERNAME = 'testviewer'
PASSWORD = 'isolated-browser-test-only'
INSTRUMENTATION = r'''
  window.__nativeWsCount=0;window.__nativeWsActive=0;window.__nativeWsMax=0;
  const NativeWs=WebSocket;
  window.WebSocket=class extends NativeWs{
    constructor(...args){super(...args);__nativeWsCount++;__nativeWsActive++;
      __nativeWsMax=Math.max(__nativeWsMax,__nativeWsActive);
      super.addEventListener('close',()=>__nativeWsActive--);}
  };
  window.__nativeFetches=[];window.__nativeRevoked=[];
  const nativeFetch=fetch.bind(window),revoke=URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL=url=>{__nativeRevoked.push(url);revoke(url);};
  window.fetch=(path,options)=>{
    __nativeFetches.push({path:String(path),started:performance.now(),signal:options?.signal});
    return nativeFetch(path,options);
  };
  window.__nativeRows=()=>['usbFront','usbLeft','usbHub2','usbHub3'].map(key=>{
    const element=document.getElementById('media-'+key),quality=element?.getVideoPlaybackQuality?.();
    return {key,tag:element?.tagName,hidden:element?.hidden,paused:element?.paused,
      width:element?.videoWidth,height:element?.videoHeight,time:element?.currentTime,
      decoded:quality?.totalVideoFrames??element?.webkitDecodedFrameCount??null,
      dropped:quality?.droppedVideoFrames??null,error:element?.error?.code??null,
      drawable:element?.tagName==='VIDEO'&&NativeCameraPlayer.isDrawable(element),
      actualPlaybackFps:element?.tagName==='VIDEO'?NativeCameraPlayer.decodedFps(element):null,
      targetBuffer:element?.tagName==='VIDEO'?NativeCameraPlayer.bufferTarget?.(element)??null:null,
      ahead:element?.buffered?.length?element.buffered.end(element.buffered.length-1)-element.currentTime:null,
      metadata:document.getElementById('media-meta-'+key)?.innerText,
      state:document.getElementById('media-state-'+key)?.innerText,
      overlay:document.getElementById('media-empty-'+key)?.innerText,
      overlayHidden:document.getElementById('media-empty-'+key)?.hidden,
      downloadVisible:!document.querySelector('[data-download='+key+']')?.hidden,
      source:element?.getAttribute('src'),
      recording:document.querySelector('[data-record='+key+']')?.getAttribute('aria-pressed')==='true',
      recordDisabled:document.querySelector('[data-record='+key+']')?.disabled};
  });
'''


def mp4_parts(data):
    """Split one generated fixture into complete top-level MP4 fragments."""
    initial, fragments, current, offset = bytearray(), [], bytearray(), 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('Incomplete synthetic MP4 box')
        size = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        if size < 8 or offset + size > len(data):
            raise ValueError('Invalid synthetic MP4 box')
        box = data[offset:offset + size]
        if kind == b'moof':
            if current:
                fragments.append(bytes(current))
            current = bytearray(box)
        elif current:
            if kind != b'mfra':
                current.extend(box)
        else:
            initial.extend(box)
        offset += size
    if current:
        fragments.append(bytes(current))
    if b'ftyp' not in initial or b'moov' not in initial or len(fragments) < 20:
        raise ValueError('Missing synthetic fMP4 initialization/fragments')
    return bytes(initial), fragments


class IsolatedVideoCloud:
    def __init__(self, directory, port, mode):
        self.directory, self.port, self.mode = directory, port, mode
        self.origin = f'http://127.0.0.1:{port}'
        self.socket = str(directory / 'preview.sock')
        self.seen, self.active = [], set()
        self.runners, self.heartbeat, self.http = [], None, None
        self.fixture_age, self.fixture_status_unavailable = 50, False
        self.fixture_usage = {key: {'owner': 'WEB', 'connected': True, 'checkedAgeMs': 0} for key in KEYS}
        self.fixture_omit_cameras = set()
        self.fixture_started = time.monotonic()

    async def start(self):
        if self.mode == 'fixture':
            ffmpeg = shutil.which('ffmpeg')
            if not ffmpeg:
                raise RuntimeError('ffmpeg is required to generate an isolated H264 fixture')
            destination = self.directory / 'synthetic.mp4'
            process = await asyncio.create_subprocess_exec(ffmpeg, '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=10', '-t', '180', '-an',
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g', '2', '-keyint_min', '2',
                '-sc_threshold', '0', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-f', 'mp4', '-y', str(destination), stderr=asyncio.subprocess.PIPE)
            _, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError('Synthetic H264 generation failed: ' + stderr.decode()[-1000:])
            self.initial, self.fragments = mp4_parts(destination.read_bytes())
        else:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=10),
                                              trust_env=False, auto_decompress=False)
        upstream = web.Application()
        upstream.add_routes([web.get('/status', self.status), web.get('/camera/{identity}/video.mp4', self.video)])
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        self.runners.append(runner)
        await web.UnixSite(runner, self.socket).start()
        config = Config(robot_id='ISOLATED_NATIVE_BROWSER', device_credentials=[{
            'token': 'ISOLATED_NATIVE_UNUSED_DEVICE_' + 'x' * 32, 'robot_id': 'ISOLATED_NATIVE_BROWSER',
            'source_id': 'TEST_ONLY', 'role': 'primary'}], viewer_username=USERNAME,
            viewer_password_hash=hash_password(PASSWORD), session_secret='ISOLATED_NATIVE_SESSION_' + 'x' * 32,
            public_origin=self.origin, data_dir=str(self.directory / 'db'), allow_insecure_local=True)
        with patch.dict(os.environ, {'NATIVE_VIDEO_SOCKET': self.socket, 'NATIVE_VIDEO_MAX_PLAYING': '4'}):
            self.app = create_app(config)
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        self.runners.append(runner)
        await web.TCPSite(runner, '127.0.0.1', self.port).start()
        session, sequence = str(uuid.uuid4()), 0

        async def heartbeat():
            nonlocal sequence
            while True:
                sequence += 1
                value = envelope('heartbeat', sequence=sequence, session=session)
                value.update(robot_id='ISOLATED_NATIVE_BROWSER', source_id='TEST_ONLY')
                self.app[STORE].ingest(value)
                await asyncio.sleep(.5)

        self.heartbeat = asyncio.create_task(heartbeat())

    async def status(self, request):
        self.seen.append(request.path)
        if self.mode == 'tunnel':
            async with self.http.get('http://127.0.0.1:18082/status', allow_redirects=False) as reply:
                if reply.status != 200:
                    raise web.HTTPServiceUnavailable()
                return web.Response(body=await reply.read(), content_type='application/json')
        if self.fixture_status_unavailable:
            raise web.HTTPServiceUnavailable()
        # Deliberately false acquisition metadata catches a UI that prints
        # metadata FPS instead of counting actual browser-presented frames.
        count = int((time.monotonic() - self.fixture_started) * 10)
        return web.json_response({'cameraUsage': self.fixture_usage, 'cameras': [dict(id=identity, direction=direction, codec='h264',
            width=640, height=360, requested_fps=10, actual_fps=123, encoded_fps=124,
            h264_mbps=.5, frame_age_ms=self.fixture_age, frames=count, encoded_frames=count,
            error=None, video_error=None) for identity, direction in CAMERAS if identity not in self.fixture_omit_cameras]})

    async def video(self, request):
        identity = request.match_info['identity']
        if identity not in {item[0] for item in CAMERAS}:
            raise web.HTTPNotFound()
        self.seen.append(request.path)
        token = object()
        self.active.add(token)
        response = web.StreamResponse(headers={'Content-Type': 'video/mp4'})
        try:
            if self.mode == 'tunnel':
                async with self.http.get('http://127.0.0.1:18082' + request.path, allow_redirects=False) as reply:
                    if reply.status != 200 or reply.content_type != 'video/mp4':
                        raise web.HTTPServiceUnavailable()
                    await response.prepare(request)
                    async for chunk in reply.content.iter_chunked(65536):
                        await response.write(chunk)
            else:
                await response.prepare(request)
                await response.write(self.initial)
                for index, fragment in enumerate(self.fragments):
                    if index >= 2:
                        await asyncio.sleep(.2)
                    await response.write(fragment)
            await response.write_eof()
        except (ConnectionError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            self.active.discard(token)
        return response

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
        for runner in reversed(self.runners):
            await runner.cleanup()
        if self.http:
            await self.http.close()


async def browser_check(server, destination, cdp, mode):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(cdp + '/json/new?about:blank', method='PUT'), timeout=5) as reply:
        target = json.load(reply)
    pending, events, sequence = {}, [], 0
    async with websockets.connect(target['webSocketDebuggerUrl'], proxy=None, max_size=30_000_000) as socket:
        async def receive():
            async for raw in socket:
                value = json.loads(raw)
                if 'id' in value:
                    future = pending.pop(value['id'], None)
                    if future and not future.done():
                        future.set_result(value)
                else:
                    events.append(value)

        receiver = asyncio.create_task(receive())

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await socket.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
            value = await asyncio.wait_for(future, 20)
            if 'error' in value:
                raise RuntimeError(value['error'])
            return value.get('result', {})

        async def evaluate(expression):
            value = await call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                   'awaitPromise': True, 'userGesture': True})
            if 'exceptionDetails' in value:
                raise RuntimeError(value['exceptionDetails'])
            return value.get('result', {}).get('value')

        async def until(expression, seconds=35):
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                try:
                    if await evaluate(expression):
                        return
                except RuntimeError:
                    pass
                await asyncio.sleep(.15)
            raise AssertionError(expression + ': ' + json.dumps(await evaluate('__nativeRows()')))

        async def independent_viewer_probe():
            """Two independently authenticated windows, one real stream each.

            Avoid eight simultaneous high-resolution software decoders: the
            preceding dashboard check already exercises all four real sources.
            """
            with opener.open(cdp + '/json/version', timeout=5) as reply:
                browser_url = json.load(reply)['webSocketDebuggerUrl']
            context_id = None
            async with websockets.connect(browser_url, proxy=None) as browser:
                browser_sequence = 0

                async def browser_call(method, params=None):
                    nonlocal browser_sequence
                    browser_sequence += 1
                    identity = browser_sequence
                    await browser.send(json.dumps({'id': identity, 'method': method, 'params': params or {}}))
                    while True:
                        value = json.loads(await asyncio.wait_for(browser.recv(), 10))
                        if value.get('id') != identity:
                            continue
                        if 'error' in value:
                            raise RuntimeError(value['error'])
                        return value.get('result', {})

                try:
                    context_id = (await browser_call('Target.createBrowserContext'))['browserContextId']
                    secondary_id = (await browser_call('Target.createTarget', {'url': 'about:blank',
                        'browserContextId': context_id, 'newWindow': True}))['targetId']
                    with opener.open(cdp + '/json/list', timeout=5) as reply:
                        secondary = next(item for item in json.load(reply) if item['id'] == secondary_id)
                    async with websockets.connect(secondary['webSocketDebuggerUrl'], proxy=None) as second:
                        second_sequence = 0

                        async def second_call(method, params=None):
                            nonlocal second_sequence
                            second_sequence += 1
                            identity = second_sequence
                            await second.send(json.dumps({'id': identity, 'method': method, 'params': params or {}}))
                            while True:
                                value = json.loads(await asyncio.wait_for(second.recv(), 10))
                                if value.get('id') != identity:
                                    continue
                                if 'error' in value:
                                    raise RuntimeError(value['error'])
                                return value.get('result', {})

                        async def second_evaluate(expression):
                            value = await second_call('Runtime.evaluate', {'expression': expression,
                                'returnByValue': True, 'awaitPromise': True, 'userGesture': True})
                            if 'exceptionDetails' in value:
                                raise RuntimeError(value['exceptionDetails'])
                            return value.get('result', {}).get('value')

                        await second_call('Page.enable')
                        await second_call('Runtime.enable')
                        await second_call('Page.navigate', {'url': server.origin + '/login'})
                        for _ in range(100):
                            if await second_evaluate("!!document.getElementById('login-form')"):
                                break
                            await asyncio.sleep(.1)
                        await second_evaluate('document.getElementById("username").value=' + json.dumps(USERNAME))
                        await second_evaluate('document.getElementById("password").value=' + json.dumps(PASSWORD))
                        await second_evaluate("document.getElementById('login-form').requestSubmit()")
                        for _ in range(100):
                            try:
                                if await second_evaluate('!!window.NativeCameraPlayer&&!!window.RKObserverDashboard'):
                                    break
                            except RuntimeError:
                                pass
                            await asyncio.sleep(.1)
                        probe = """(()=>{
                          RKObserverDashboard.openPage('overview');
                          const video=document.createElement('video');video.id='isolated-native-viewer-probe';
                          video.style.cssText='width:640px;height:360px;position:fixed;left:280px;top:140px;z-index:9999;background:#07141c';
                          document.body.appendChild(video);
                          window.__viewerPlayer=new NativeCameraPlayer(video,{isCurrent:()=>true});
                          __viewerPlayer.start('/native-camera/usbFront/video.mp4');
                          window.__viewerProgress=()=>({width:video.videoWidth,height:video.videoHeight,time:video.currentTime,
                            decoded:video.getVideoPlaybackQuality().totalVideoFrames,fps:NativeCameraPlayer.decodedFps(video),
                            drawable:NativeCameraPlayer.isDrawable(video)});
                        })()"""
                        await evaluate(probe)
                        await second_evaluate(probe)
                        deadline = time.monotonic() + 17
                        while time.monotonic() < deadline:
                            primary, additional = await evaluate('__viewerProgress()'), await second_evaluate('__viewerProgress()')
                            if primary['drawable'] and additional['drawable']:
                                break
                            await asyncio.sleep(.2)
                        else:
                            raise AssertionError('Two viewer streams did not become drawable within 20 seconds')
                        await asyncio.sleep(2)
                        last_primary, last_additional = await evaluate('__viewerProgress()'), await second_evaluate('__viewerProgress()')
                        for first, last in ((primary, last_primary), (additional, last_additional)):
                            assert last['time'] > first['time'] and last['decoded'] > first['decoded']
                        await evaluate('__viewerPlayer.stop();document.getElementById("isolated-native-viewer-probe").remove()')
                        await second_evaluate('__viewerPlayer.stop()')
                        return {'independentBrowserContext': True, 'independentViewerSessions': True,
                            'streamsPerViewer': 1, 'first': last_primary, 'second': last_additional,
                            'bothVideoProgress': True, 'publicWanAcceptance': False}
                finally:
                    if context_id:
                        await browser_call('Target.disposeBrowserContext', {'browserContextId': context_id})

        downloads = destination / 'downloads'
        downloads.mkdir(parents=True, exist_ok=True)
        try:
            for domain in ('Page', 'Runtime', 'Log', 'Network'):
                await call(domain + '.enable')
            await call('Page.addScriptToEvaluateOnNewDocument', {'source': INSTRUMENTATION})
            await call('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(downloads.resolve()), 'eventsEnabled': True})
            await call('Emulation.setDeviceMetricsOverride', {'width': 3440, 'height': 1440, 'deviceScaleFactor': 1, 'mobile': False})
            await call('Page.navigate', {'url': server.origin + '/login'})
            await until("!!document.getElementById('login-form')")
            await evaluate('document.getElementById("username").value=' + json.dumps(USERNAME))
            await evaluate('document.getElementById("password").value=' + json.dumps(PASSWORD))
            await evaluate("document.getElementById('login-form').requestSubmit()")
            await until('!!window.RKObserverDashboard')
            await evaluate("RKObserverDashboard.openPage('media/cameras');window.__originalNativeWs=RKObserverDashboard.getConnection()")
            await until('__nativeRows().every(row=>row.tag==="VIDEO"&&row.drawable&&!row.recordDisabled)')
            assert await evaluate("document.querySelectorAll('#camera-gallery .media-card').length") == 6
            assert await evaluate("document.querySelectorAll('[data-record]').length") == 14
            assert await evaluate("['fisheye','usbRear','usbPort21'].every(key=>!document.getElementById('media-'+key))")
            before = await evaluate('__nativeRows()')
            start = time.monotonic()
            await asyncio.sleep(3)
            after = await evaluate('__nativeRows()')
            duration = time.monotonic() - start
            decoded = []
            for first, last in zip(before, after):
                assert last['width'] > 0 and last['height'] > 0 and last['time'] > first['time'] + 1
                assert last['decoded'] > first['decoded'] and not last['error']
                assert last['actualPlaybackFps'] is not None and 0 < last['actualPlaybackFps'] < 80
                if mode == 'fixture':
                    assert (last['width'], last['height']) == (640, 360)
                    assert 5 < last['actualPlaybackFps'] < 16
                    assert '123' not in last['metadata'] and '124' not in last['metadata']
                assert '来源' not in last['metadata']
                decoded.append({**last, 'decodedFrameDelta': last['decoded'] - first['decoded'],
                                'measuredDecodedFps': (last['decoded'] - first['decoded']) / duration})
            record_keys = KEYS if mode == 'fixture' else KEYS[:1]
            record_filter = 'row=>' + json.dumps(record_keys) + '.includes(row.key)'
            await evaluate(f"__nativeRows().filter({record_filter}).forEach(row=>document.querySelector('[data-record='+row.key+']').click())")
            await until(f'__nativeRows().filter({record_filter}).every(row=>row.recording)')
            await asyncio.sleep(2)
            assert await evaluate(f'__nativeRows().filter({record_filter}).every(row=>row.recording&&row.drawable)')
            await evaluate(f"__nativeRows().filter({record_filter}).forEach(row=>document.querySelector('[data-record='+row.key+']').click())")
            await until(f"__nativeRows().filter({record_filter}).every(row=>!document.querySelector('[data-download='+row.key+']').hidden)")
            clips = []
            for key in record_keys:
                previous = set(downloads.iterdir())
                await evaluate(f"document.querySelector('[data-download={key}]').click()")
                for _ in range(50):
                    created = [path for path in set(downloads.iterdir()) - previous if path.suffix in ('.webm', '.mp4') and path.stat().st_size > 100]
                    if created:
                        clips.append({'key': key, 'file': created[0].name, 'bytes': created[0].stat().st_size})
                        break
                    await asyncio.sleep(.1)
                else:
                    raise AssertionError('Native video recording was not downloaded: ' + key)
            source_safety = None
            if mode == 'fixture':
                safety_cases = []
                for age in (None, 2000, 'INVALID_TEST_ONLY'):
                    await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
                    await evaluate("document.querySelector('[data-record=usbFront]').click()")
                    await until('__nativeRows()[0].recording')
                    server.fixture_age = age
                    await until('__nativeRows().every(row=>row.recordDisabled)&&!__nativeRows()[0].recording', seconds=8)
                    first = await evaluate('__nativeRows()')
                    await asyncio.sleep(1)
                    last = await evaluate('__nativeRows()')
                    assert all(not row['hidden'] and row['width'] > 0 and row['height'] > 0 for row in last)
                    assert all(end['time'] > beginning['time'] for beginning, end in zip(first, last))
                    safety_cases.append({'frameAgeMs': age, 'recordingBlocked': True, 'pixelsRemainDecoded': True})
                    server.fixture_age = 50
                    await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
                await evaluate("document.querySelector('[data-record=usbFront]').click()")
                await until('__nativeRows()[0].recording')
                server.fixture_status_unavailable = True
                await until('__nativeRows().every(row=>row.recordDisabled)&&!__nativeRows()[0].recording', seconds=10)
                first = await evaluate('__nativeRows()')
                await asyncio.sleep(1)
                last = await evaluate('__nativeRows()')
                assert all(not end['hidden'] and end['time'] > row['time'] for row, end in zip(first, last))
                server.fixture_status_unavailable = False
                await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
                source_safety = {'ageCases': safety_cases, 'statusLossBlocksRecording': True,
                                 'statusLossPreservesDecodedPixels': True, 'freshSourceRestoresRecording': True}
            camera_usage = None
            if mode == 'fixture':
                await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
                await evaluate("document.querySelector('[data-record=usbFront]').click()")
                await until('__nativeRows()[0].recording')
                await asyncio.sleep(1.2)
                server.fixture_usage['usbFront'] = {'owner': 'OTHER', 'connected': True, 'checkedAgeMs': 0}
                # Ownership identifies the capturer. A valid shared preview
                # keeps passive playback and recording available.
                await asyncio.sleep(2.5)
                shared_first = await evaluate('__nativeRows()')
                assert shared_first[0]['drawable'] and shared_first[0]['recording']
                assert not shared_first[0]['recordDisabled'] and not shared_first[0]['paused']
                await asyncio.sleep(1)
                shared_last = await evaluate('__nativeRows()')
                assert all(last['time'] > first['time'] for first, last in zip(shared_first, shared_last))
                # With no advertised preview, only the occupied slot stops.
                server.fixture_omit_cameras.add('camera2')
                await until("__nativeRows()[0].state==='未共享'&&"
                            "__nativeRows()[0].overlay==='相机正在使用，尚未共享画面'&&"
                            "__nativeRows()[0].recordDisabled&&!__nativeRows()[0].recording&&"
                            "__nativeRows()[0].paused&&__nativeRows()[0].source===null")
                await until('__nativeRows()[0].downloadVisible')
                occupied = await evaluate('__nativeRows()')
                await asyncio.sleep(1.1)
                later = await evaluate('__nativeRows()')
                assert all(last['time'] > first['time'] and last['drawable']
                           for first, last in zip(occupied[1:], later[1:]))
                assert later[0]['downloadVisible'] and '来源' not in later[0]['metadata']
                server.fixture_status_unavailable = True
                await until("__nativeRows()[0].state==='状态未知'&&"
                            "__nativeRows()[0].overlay==='占用状态未知 · 等待视频'", seconds=10)
                expired = await evaluate('__nativeRows()')
                assert expired[0]['downloadVisible'] and expired[0]['recordDisabled']
                assert all(row['time'] > first['time'] and not row['paused'] and not row['hidden']
                           for first, row in zip(later[1:], expired[1:]))
                server.fixture_usage['usbFront'] = {'owner': 'OTHER', 'connected': True, 'checkedAgeMs': 6000}
                server.fixture_status_unavailable = False
                await until("__nativeRows()[0].state==='状态未知'&&__nativeRows().slice(1).every(row=>row.drawable)")
                server.fixture_usage['usbFront'] = {'owner': 'WEB', 'connected': True, 'checkedAgeMs': 0}
                server.fixture_omit_cameras.clear()
                await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
                recovered = await evaluate('__nativeRows()')
                assert recovered[0]['downloadVisible'] and recovered[0]['decoded'] > 0 and recovered[0]['source']
                camera_usage = {'otherOwnerSharedPreviewKeepsPlaybackAndRecording': True,
                    'otherOwnerWithoutPreviewStopsOnlyOccupiedSlot': True,
                    'otherSlotsKeepPlaying': True, 'savedRecordingPreserved': True,
                    'lostProbeExpiresOccupiedLabel': True, 'metadataLossPreservesOtherDecodedStreams': True,
                    'staleOwnershipNeverClaimsBusy': True, 'releaseRestartsFromDecodedFrame': True}
            layouts = []
            for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
                await call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': width < 500})
                assert await evaluate('document.documentElement.scrollWidth<=innerWidth')
                layouts.append({'width': width, 'height': height, 'noHorizontalOverflow': True})
                screenshot = await call('Page.captureScreenshot', {'format': 'png'})
                (destination / f'{width}-native-video.png').write_bytes(base64.b64decode(screenshot['data']))
            await evaluate("RKObserverDashboard.openPage('media/scans')")
            await until("__nativeRows().every(row=>row.paused&&document.getElementById('media-'+row.key).getAttribute('src')===null)")
            assert await evaluate("__nativeFetches.filter(item=>item.path.startsWith('/native-camera/')&&item.path.endsWith('/video.mp4')).every(item=>item.signal.aborted)")
            request_count = await evaluate("__nativeFetches.filter(item=>item.path.startsWith('/native-camera/')).length")
            await asyncio.sleep(2.3)
            assert await evaluate("__nativeFetches.filter(item=>item.path.startsWith('/native-camera/')).length") == request_count
            assert await evaluate("!__nativeFetches.some(item=>/^\/camera\/(usbFront|usbLeft|usbHub2|usbHub3)\.jpg$/.test(item.path))")
            await evaluate("RKObserverDashboard.openPage('media/cameras')")
            await until('__nativeRows().every(row=>row.drawable&&!row.recordDisabled)')
            assert await evaluate('__nativeWsCount===1&&__nativeWsMax===1&&RKObserverDashboard.getConnection()===__originalNativeWs')
            viewers = None
            if mode == 'tunnel':
                await evaluate("RKObserverDashboard.openPage('media/scans')")
                await asyncio.sleep(1.5)
                viewers = await independent_viewer_probe()
            errors = [event for event in events if event.get('method') == 'Runtime.exceptionThrown'
                      or (event.get('method') == 'Log.entryAdded' and event['params']['entry']['level'] == 'error')
                      or (event.get('method') == 'Runtime.consoleAPICalled' and event['params']['type'] == 'error')]
            assert not errors, errors
            result = {'scope': 'ISOLATED_LOOPBACK_NATIVE_H264', 'sourceMode': mode, 'publicWanAcceptance': False,
                      'syntheticHeartbeatOnly': True, 'productionChanged': False, 'videoCards': 4,
                      'cameraCards': 6, 'recordingControls': 14, 'decoded': decoded, 'recordingDownloads': clips,
                      'recordedCameraCount': len(record_keys),
                      'twoIndependentViewerProbe': viewers,
                      'sourceFreshnessSafety': source_safety,
                      'cameraUsageLifecycle': camera_usage,
                      'singleTelemetryWebSocket': True, 'noNativeJpegPolling': True,
                      'hiddenCategoryAbortsStreamsAndStopsFetch': True, 'returnsWithFreshStreams': True,
                      'sourceLabelAbsent': True, 'consoleErrors': 0, 'layouts': layouts}
            (destination / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        finally:
            await call('Page.close')
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', nargs='?', default='/tmp/native-video-browser-results')
    parser.add_argument('--source-mode', choices=('fixture', 'tunnel'), default='fixture')
    parser.add_argument('--port', type=int, default=19094)
    args = parser.parse_args()
    if not 19000 <= args.port <= 19999 or args.port in (19088, 19089):
        raise RuntimeError('Use a dedicated isolated test backend port 19000..19999, excluding existing 19088/19089')
    cdp = local_test_url('CLOUD_CDP_URL', 'http://127.0.0.1:19089')
    destination = Path(args.destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-browser-isolated-') as temporary:
        server = IsolatedVideoCloud(Path(temporary), args.port, args.source_mode)
        try:
            await server.start()
            await browser_check(server, destination, cdp, args.source_mode)
        finally:
            await server.close()


if __name__ == '__main__':
    asyncio.run(main())
