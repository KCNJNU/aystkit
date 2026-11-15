"""Real Firefox viewport playback regression with synthetic, loopback-only data.

Uses the maintained frontend and backend. No production credentials, network
hosts, robot frames, camera devices or user browser profiles are accessed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

from aiohttp import web

from cloud.backend.app import STORE
from tests import browser_native_video as fixture
from tests.browser_shared_video_firefox import Browser, INSTRUMENT, request
from tests.test_protocol import envelope, jpeg


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT += r'''
window.__viewportRequests=[];
window.__effectiveViewport=()=>{
 const header=document.querySelector('.topbar')?.getBoundingClientRect(),nav=document.querySelector('.sidebar')?.getBoundingClientRect();
 return {left:0,right:innerWidth,top:header&&header.bottom>0?Math.min(innerHeight,header.bottom):0,
  bottom:nav&&nav.width>=innerWidth*.8&&nav.top>0&&nav.bottom>=innerHeight-1?nav.top:innerHeight};
};
window.__rectVisible=r=>{
 const view=__effectiveViewport();return r.width>0&&r.height>0&&
  Math.min(r.right,view.right)>Math.max(r.left,view.left)&&Math.min(r.bottom,view.bottom)>Math.max(r.top,view.top);
};
window.__viewportRows=()=>['usbFront','usbLeft','usbHub2','usbHub3','color','depth'].map(key=>{
 const v=document.getElementById('media-'+key),r=v.closest('.media-viewport').getBoundingClientRect();
 const visible=__rectVisible(r);
 const player=__qaPlayers.filter(p=>p.video===v&&p.controller&&!p.controller.signal.aborted).at(-1);
 return {key,tag:v.tagName,visible,top:r.top,bottom:r.bottom,hidden:v.hidden,
  width:v.videoWidth||v.naturalWidth||0,height:v.videoHeight||v.naturalHeight||0,
  decoded:v.getVideoPlaybackQuality?.().totalVideoFrames??null,
  phase:player?.phase??null,paused:v.paused??null,src:v.getAttribute('src'),
  drawable:v.tagName==='VIDEO'?NativeCameraPlayer.isDrawable(v):!v.hidden&&v.naturalWidth>0,
  requests:__viewportRequests.filter(r=>r.key===key).length};
});
const viewportFetch=fetch.bind(window);
window.fetch=(path,options)=>{
 const match=/^\/native-camera\/([^/]+)\/video\.mp4$/.exec(String(path))||/^\/camera\/([^/]+)\.jpg$/.exec(String(path));
 if(match){
  const key=match[1],source=document.getElementById('media-'+key),r=source.closest('.media-viewport').getBoundingClientRect();
  const row={key,path:String(path),at:performance.now(),aborted:false,
   visible:__rectVisible(r)};
  __viewportRequests.push(row);options?.signal?.addEventListener('abort',()=>{row.aborted=true});
 }
 return viewportFetch(path,options);
};
'''


class ViewportCloud(fixture.IsolatedVideoCloud):
    def __init__(self, directory, port):
        super().__init__(directory, port, 'fixture')
        self.active_keys = collections.Counter()
        self.requests = collections.Counter()

    async def video(self, request):
        identity = request.match_info['identity']
        self.requests[identity] += 1
        self.active_keys[identity] += 1
        try:
            return await super().video(request)
        finally:
            self.active_keys[identity] -= 1

    async def start(self):
        original = fixture.create_app

        @web.middleware
        async def instrument(request, handler):
            if request.path == '/qa/instrument.js':
                return web.Response(text=INSTRUMENT, content_type='application/javascript')
            if request.path == '/qa/metrics':
                return web.json_response({'activeSources': dict(self.active_keys),
                                          'sourceRequests': dict(self.requests)})
            response = await handler(request)
            if request.path == '/' and response.status == 200:
                html = (ROOT / 'cloud/frontend/index.html').read_text().replace(
                    '<script src="/media.js"></script>',
                    '<script src="/qa/instrument.js"></script><script src="/media.js"></script>')
                return web.Response(text=html, content_type='text/html')
            return response

        def create_app(config):
            app = original(config)
            app.middlewares.append(instrument)
            return app

        with patch.object(fixture, 'create_app', create_app):
            await super().start()
        self.heartbeat.cancel()
        await asyncio.gather(self.heartbeat, return_exceptions=True)
        session, sequence = str(uuid.uuid4()), 0
        encoded = base64.b64encode(jpeg()).decode('ascii')

        async def frames():
            nonlocal sequence
            while True:
                for key in (None, 'color', 'depth'):
                    sequence += 1
                    value = envelope('media' if key else 'heartbeat', sequence=sequence, session=session)
                    value.update(robot_id='ISOLATED_NATIVE_BROWSER', source_id='TEST_ONLY')
                    if key:
                        value['payload'] = dict(key=key, kind='image', status='AVAILABLE', ageS=0,
                            frameSeq=sequence, data=encoded, mime_type='image/jpeg', width=1, height=1)
                    self.app[STORE].ingest(value)
                await asyncio.sleep(.25)

        self.heartbeat = asyncio.create_task(frames())


async def serve(port):
    with tempfile.TemporaryDirectory(prefix='camera-viewport-fixture-') as temporary:
        cloud = ViewportCloud(Path(temporary), port)
        await cloud.start()
        try:
            await asyncio.Event().wait()
        finally:
            await cloud.close()


def verify(destination, port, driver_port):
    destination.mkdir(parents=True, exist_ok=True)
    result = {'status': 'FAIL', 'scope': 'SYNTHETIC_LOOPBACK_REAL_FIREFOX_VIEWPORT',
              'productionRequests': 0, 'productionCredentialsUsed': False, 'phases': []}
    sources = ('cloud/frontend/media.js', 'cloud/frontend/native-player.js',
               'cloud/frontend/detection-overlay.js', 'cloud/backend/native_video.py')
    result['sourceDigests'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    processes, logs, browser = [], [], None
    profile = Path(tempfile.mkdtemp(prefix='observer-viewport-qa-', dir=Path.home() / 'snap/firefox/common'))
    base = f'http://127.0.0.1:{port}'

    def record(name, **values):
        result['phases'].append({'name': name, **values})
        (destination / 'progress.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))

    def rows():
        return browser.evaluate('return __viewportRows()')

    def settled():
        browser.wait('__viewportRows().filter(r=>r.visible).every(r=>r.drawable&&r.width>0)')
        browser.wait('__viewportRows().filter(r=>!r.visible&&r.tag==="VIDEO").every(r=>r.paused&&r.src===null)')
        return rows()

    try:
        env = {**os.environ, 'LIBGL_ALWAYS_SOFTWARE': '1', 'MOZ_WEBRENDER': '1',
               'MOZ_DISABLE_CONTENT_SANDBOX': '1'}
        commands = [([sys.executable, '-B', '-m', 'tests.browser_camera_viewport', '--serve',
                      '--port', str(port)], 'fixture'),
                    (['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', str(driver_port),
                      '--websocket-port', str(driver_port + 100), '--profile-root', str(profile)], 'driver')]
        for command, name in commands:
            log = (destination / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            try:
                request(base + '/healthz')
                request(f'http://127.0.0.1:{driver_port}/status')
                break
            except (OSError, ValueError):
                time.sleep(.2)
        else:
            raise AssertionError('Isolated fixture or Firefox driver startup failed')
        browser = Browser(f'http://127.0.0.1:{driver_port}')
        browser.call('/url', {'url': base + '/login'})
        browser.evaluate('document.getElementById("username").value=' + json.dumps(fixture.USERNAME)
            + ';document.getElementById("password").value=' + json.dumps(fixture.PASSWORD)
            + ';document.getElementById("login-form").requestSubmit();')
        browser.wait('!!window.RKObserverDashboard&&!!window.__viewportRows')
        context = browser.call('/window', method='GET')

        for width, height in ((1366, 1000), (390, 844)):
            browser.evaluate("RKObserverDashboard.openPage('overview')")
            asyncio.run(browser.bidi('browsingContext.setViewport', {
                'context': context, 'viewport': {'width': width, 'height': height}, 'devicePixelRatio': 1}))
            browser.evaluate('window.__viewportRequests.length=0;window.__qaFetches.length=0;'
                             'window.__qaErrors.length=0;RKObserverDashboard.openPage("media/cameras");')
            browser.wait('innerWidth===' + str(width) + '&&innerHeight===' + str(height))
            initial = settled()
            time.sleep(1)
            initial_requests = browser.evaluate('return __viewportRequests')
            assert initial_requests and all(row['visible'] for row in initial_requests), initial_requests
            assert all(row['visible'] or row['requests'] == 0 for row in initial), initial
            top_native = [row['key'] for row in initial if row['visible'] and row['tag'] == 'VIDEO']
            assert top_native, initial
            if width == 390:
                assert len(top_native) < 4, initial
            record(f'{width}_initial_only_visible_requested', rows=initial,
                   requests=initial_requests, metrics=request(base + '/qa/metrics'))

            browser.evaluate('window.scrollTo({top:document.documentElement.scrollHeight,behavior:"instant"})')
            browser.wait('__viewportRows().find(r=>r.key==="depth").visible')
            bottom = settled()
            browser.wait('__viewportRows().filter(r=>!r.visible&&r.tag==="VIDEO").every(r=>'
                '__qaFetches.filter(f=>f.path==="/native-camera/"+r.key+"/video.mp4").every(f=>f.aborted))')
            depth_before = browser.evaluate('return __viewportRequests.filter(r=>r.key==="depth").length')
            time.sleep(.8)
            assert browser.evaluate('return __viewportRequests.filter(r=>r.key==="depth").length') > depth_before
            bottom_requests = browser.evaluate('return __viewportRequests')
            assert all(row['visible'] for row in bottom_requests), bottom_requests
            assert any(not row['visible'] for row in bottom if row['key'] in top_native), bottom
            record(f'{width}_bottom_depth_resumes_upper_video_canceled', rows=bottom,
                   requests=bottom_requests, metrics=request(base + '/qa/metrics'))

            previous_requests = {row['key']: row['requests'] for row in bottom}
            browser.evaluate('window.scrollTo({top:0,behavior:"instant"})')
            resumed = settled()
            assert all(row['requests'] > previous_requests[row['key']] for row in resumed
                       if row['visible'] and row['key'] in top_native and
                       next(old for old in bottom if old['key'] == row['key'])['src'] is None), resumed
            first = {row['key']: row['decoded'] for row in resumed if row['tag'] == 'VIDEO' and row['visible']}
            time.sleep(1)
            resumed = rows()
            assert all(row['decoded'] > first[row['key']] for row in resumed if row['key'] in first), resumed
            assert not browser.evaluate('return __qaErrors'), browser.evaluate('return __qaErrors')
            record(f'{width}_scroll_back_decodes_again', rows=resumed)

            browser.evaluate("RKObserverDashboard.openPage('overview')")
            browser.wait('__viewportRows().filter(r=>r.tag==="VIDEO").every(r=>r.paused&&r.src===null)')
            browser.wait('__qaFetches.filter(r=>r.path.endsWith("/video.mp4")).every(r=>r.aborted)')
            count = browser.evaluate('return __viewportRequests.length')
            time.sleep(.8)
            assert browser.evaluate('return __viewportRequests.length') == count
            record(f'{width}_leave_page_stops_all_camera_requests', rows=rows(), requestCount=count,
                   metrics=request(base + '/qa/metrics'))
        result['status'] = 'PASS'
    except Exception as error:
        result['failure'] = str(error)
        if browser:
            try:
                result['browserAtFailure'] = browser.evaluate('return {rows:__viewportRows?.(),errors:__qaErrors,requests:__viewportRequests,width:innerWidth,height:innerHeight}')
            except Exception:
                pass
        raise
    finally:
        result['sourceUnchangedDuringRun'] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                                                for name, digest in result['sourceDigests'].items())
        (destination / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({'status': result['status'], 'artifact': str(destination / 'results.json')}, ensure_ascii=False), flush=True)
        if browser:
            try:
                browser.call('', method='DELETE')
            except OSError:
                pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', default='/tmp/observer-camera-viewport')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=19468)
    parser.add_argument('--driver-port', type=int, default=19469)
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.port))
    else:
        verify(Path(args.destination), args.port, args.driver_port)
