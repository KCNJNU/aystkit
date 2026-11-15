"""Independent shared-stream Firefox acceptance, using synthetic loopback data.

Runs the real backend and maintained frontend with two approved ephemeral
accounts, four 640x360/10 FPS sources, and two independent Firefox profiles.
No production credential, robot image, remote host, or device is used.
"""
from __future__ import annotations

import argparse
import asyncio
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
import urllib.request
import uuid
from unittest.mock import patch

from aiohttp import web
import websockets

from cloud.backend.app import ACCOUNTS, NATIVE_VIDEO, STORE, create_app
from cloud.backend.security import Config, hash_password
from tests.browser_native_video import mp4_parts
from tests.test_protocol import envelope

ROOT = Path(__file__).resolve().parents[1]
KEYS = ('usbFront', 'usbLeft', 'usbHub3', 'usbHub2')
CAMERAS = (('camera2', 'front'), ('camera1', 'rear'), ('camera3', 'left'), ('camera4', 'right'))
USERNAME, SECOND_USER = 'synthetic.admin', 'synthetic.viewer'
PASSWORD = 'isolated-shared-video-browser-only'
INSTRUMENT = r'''
window.__qaErrors=[];window.__qaFetches=[];window.__qaPlayers=[];window.__qaFrameEvents=[];
window.__qaSnapshots=[];window.__qaPageShows=[];
addEventListener('pageshow',e=>__qaPageShows.push({persisted:e.persisted,at:performance.now()}));
const QaSocket=WebSocket;window.WebSocket=class extends QaSocket{
 constructor(...args){super(...args);this.addEventListener('message',e=>{try{const s=JSON.parse(e.data);
 if(s.meta){__qaSnapshots.push({at:performance.now(),timestamp:s.meta.timestamp});if(__qaSnapshots.length>64)__qaSnapshots.shift();}}catch(_){}})}
};
addEventListener('error',e=>__qaErrors.push(String(e.message)));
addEventListener('unhandledrejection',e=>__qaErrors.push(String(e.reason)));
const qaFetch=fetch.bind(window);window.fetch=async(path,options)=>{
 const row={path:String(path),at:performance.now(),aborted:false};__qaFetches.push(row);
 options?.signal?.addEventListener('abort',()=>{row.aborted=true});
 try{const r=await qaFetch(path,options);row.status=r.status;return r;}
 catch(e){row.error=e.name;throw e;}
};
const QaPlayer=NativeCameraPlayer;window.NativeCameraPlayer=class extends QaPlayer{
 constructor(v,o){super(v,{...o,onFrame:info=>{__qaFrameEvents.push({key:v.id,at:performance.now(),session:info.streamSession});
 o?.onFrame?.(info)}});__qaPlayers.push(this)}
};
window.__qaRows=()=>['usbFront','usbLeft','usbHub3','usbHub2'].map(key=>{
 const v=document.getElementById('media-'+key),p=__qaPlayers.filter(p=>p.video===v&&p.controller&&!p.controller.signal.aborted).at(-1);
 return {key,width:v?.videoWidth,height:v?.videoHeight,time:v?.currentTime,
 decoded:v?.getVideoPlaybackQuality?.().totalVideoFrames,paused:v?.paused,hidden:v?.hidden,
 phase:p?.phase||null,streamSession:p?.streamSession||null,error:v?.error?.code??null,
 requests:__qaFetches.filter(r=>r.path==='/native-camera/'+key+'/video.mp4').length}
});
window.__qaDiagnostics=()=>({path:location.pathname,hash:location.hash,hidden:document.hidden,
 lease:window.ObserverVideoLease?.state(),rows:__qaRows(),errors:__qaErrors,
 fetches:__qaFetches.filter(r=>/video\.mp4$/.test(r.path)).map(({path,status,error,aborted})=>({path,status,error,aborted}))});
'''


async def serve(port):
    with tempfile.TemporaryDirectory(prefix='shared-video-fixture-') as temporary:
        directory = Path(temporary)
        path = directory / 'synthetic.mp4'
        process = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=10', '-t', '300', '-an',
            '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g', '10', '-keyint_min', '10',
            '-sc_threshold', '0', '-b:v', '350000', '-maxrate', '350000', '-bufsize', '350000',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '200000',
            '-f', 'mp4', '-y', str(path), stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError('Synthetic encoder failed: ' + error.decode()[-500:])
        initial, fragments = mp4_parts(path.read_bytes())
        active, maximum, requests = collections.Counter(), collections.Counter(), collections.Counter()
        byte_counts = collections.Counter()
        started = time.monotonic()
        generation = 0
        outage = False
        sessions = {identity: uuid.uuid4().hex for identity, _ in CAMERAS}

        async def status(request):
            elapsed = time.monotonic() - started
            return web.json_response({'cameras': [dict(id=identity, direction=direction,
                codec='h264', width=640, height=360, requested_fps=10, actual_fps=25,
                encoded_fps=10, h264_mbps=.35, frame_age_ms=10,
                frames=int(elapsed * 25), encoded_frames=int(elapsed * 10),
                error=None, video_error=None) for identity, direction in CAMERAS],
                'cameraUsage': {key: {'owner': 'WEB', 'connected': True, 'checkedAgeMs': 0} for key in KEYS}})

        async def video(request):
            identity = request.match_info['identity']
            if identity not in sessions:
                raise web.HTTPNotFound()
            if outage:
                raise web.HTTPServiceUnavailable()
            epoch = generation
            requests[identity] += 1
            active[identity] += 1
            maximum[identity] = max(maximum[identity], active[identity])
            response = web.StreamResponse(headers={'Content-Type': 'video/mp4', 'X-Video-Session': sessions[identity]})
            try:
                await response.prepare(request)
                await response.write(initial)
                byte_counts[identity] += len(initial)
                clock = time.monotonic()
                for index, fragment in enumerate(fragments):
                    if generation != epoch or outage:
                        break
                    wait = clock + index * .2 - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await response.write(fragment)
                    byte_counts[identity] += len(fragment)
                await response.write_eof()
            except (ConnectionError, RuntimeError, asyncio.CancelledError):
                pass
            finally:
                active[identity] -= 1
            return response

        source = web.Application()
        source.add_routes([web.get('/status', status), web.get('/camera/{identity}/video.mp4', video)])
        source_runner = web.AppRunner(source, access_log=None)
        await source_runner.setup()
        socket = str(directory / 'source.sock')
        await web.UnixSite(source_runner, socket).start()
        config = Config(robot_id='ISOLATED_SHARED_VIDEO', device_credentials=[{
            'token': 'ISOLATED_UNUSED_DEVICE_' + 'x' * 40, 'robot_id': 'ISOLATED_SHARED_VIDEO',
            'source_id': 'TEST_ONLY', 'role': 'primary'}], viewer_username=USERNAME,
            viewer_password_hash=hash_password(PASSWORD), session_secret='ISOLATED_SESSION_' + 'x' * 48,
            public_origin=f'http://127.0.0.1:{port}', data_dir=str(directory / 'db'), allow_insecure_local=True)
        with patch.dict(os.environ, {'NATIVE_VIDEO_SOCKET': socket, 'NATIVE_VIDEO_MAX_PLAYING': '4',
                                    'NATIVE_VIDEO_SPLIT_SOCKETS': 'false'}):
            app = create_app(config)
        account = app[ACCOUNTS].register(SECOND_USER, hash_password(PASSWORD))
        app[ACCOUNTS].approve(account['id'], USERNAME)

        @web.middleware
        async def fixture_only(request, handler):
            nonlocal outage, generation, sessions
            if request.path == '/qa/instrument.js':
                return web.Response(text=INSTRUMENT, content_type='application/javascript')
            if request.path == '/qa/metrics':
                return web.json_response({'sourceRequests': dict(requests), 'activeSources': dict(active),
                    'maxActiveSources': dict(maximum), 'sourceBytes': dict(byte_counts),
                    'sessions': dict(sessions), 'outage': outage})
            if request.path == '/qa/outage' and request.method == 'POST':
                outage = bool((await request.json())['outage'])
                generation += 1
                if not outage:
                    sessions = {identity: uuid.uuid4().hex for identity, _ in CAMERAS}
                return web.json_response({'outage': outage, 'generation': generation})
            response = await handler(request)
            if request.path == '/' and response.status == 200:
                html = (ROOT / 'cloud/frontend/index.html').read_text()
                html = html.replace('<script src="/media.js"></script>',
                    '<script src="/qa/instrument.js"></script><script src="/media.js"></script>')
                return web.Response(text=html, content_type='text/html')
            return response

        app.middlewares.append(fixture_only)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        session, sequence = str(uuid.uuid4()), 0

        async def heartbeat():
            nonlocal sequence
            while True:
                sequence += 1
                value = envelope('heartbeat', sequence=sequence, session=session)
                value.update(robot_id='ISOLATED_SHARED_VIDEO', source_id='TEST_ONLY')
                app[STORE].ingest(value)
                await asyncio.sleep(.5)

        task = asyncio.create_task(heartbeat())
        try:
            await asyncio.Event().wait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()
            await source_runner.cleanup()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method='GET', data=None, timeout=20):
    req = urllib.request.Request(url, None if data is None else json.dumps(data).encode(),
                                 {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


class Browser:
    def __init__(self, driver):
        self.driver = driver
        result = request(driver + '/session', 'POST', {'capabilities': {'alwaysMatch': {
            'browserName': 'firefox', 'webSocketUrl': True, 'moz:firefoxOptions': {
                'binary': '/snap/firefox/current/usr/lib/firefox/firefox', 'args': ['-headless'],
                'prefs': {'media.autoplay.default': 0, 'webgl.force-enabled': True,
                    'webgl.disabled': False, 'gfx.webrender.software': True,
                    'webgl.enable-software-renderer': True}}}}}, timeout=60)['value']
        self.session = result['sessionId']
        self.websocket = result['capabilities']['webSocketUrl']

    def call(self, endpoint, data=None, method='POST'):
        return request(self.driver + '/session/' + self.session + endpoint, method, data)['value']

    def evaluate(self, code):
        return self.call('/execute/sync', {'script': code, 'args': []})

    def wait(self, expression, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate('return (' + expression + ')')
            if value:
                return value
            time.sleep(.2)
        raise AssertionError({'expression': expression, 'browser': self.evaluate('return window.__qaDiagnostics?.()')})

    async def bidi(self, method, params):
        async with websockets.connect(self.websocket, proxy=None) as socket:
            await socket.send(json.dumps({'id': 1, 'method': method, 'params': params}))
            while True:
                reply = json.loads(await socket.recv())
                if reply.get('id') == 1:
                    if reply.get('type') == 'error':
                        raise AssertionError(reply)
                    return reply['result']

    def activate(self, context):
        return asyncio.run(self.bidi('browsingContext.activate', {'context': context}))

    def background_value(self, context, expression):
        response = asyncio.run(self.bidi('script.evaluate', {
            'expression': 'JSON.stringify(' + expression + ')',
            'target': {'context': context}, 'awaitPromise': False}))
        assert response['type'] == 'success', response
        return json.loads(response['result']['value'])

    def playing(self):
        self.wait('__qaRows().every(r=>r.width===640&&r.height===360&&r.phase==="playing"&&!r.paused&&!r.hidden&&r.decoded>5&&!r.error)')

    def login(self, base, username):
        self.call('/url', {'url': base + '/login'})
        self.wait('!!document.getElementById("login-form")')
        self.evaluate('document.getElementById("username").value=' + json.dumps(username)
            + ';document.getElementById("password").value=' + json.dumps(PASSWORD)
            + ';document.getElementById("login-form").requestSubmit();')
        self.wait('!!window.RKObserverDashboard&&!!window.__qaRows')
        self.evaluate("RKObserverDashboard.openPage('media/cameras')")


def verify(destination, port, driver_ports):
    destination.mkdir(parents=True, exist_ok=True)
    base = f'http://127.0.0.1:{port}'
    processes, logs, profiles, browsers, phases = [], [], [], [], []
    result = {'status': 'FAIL', 'scope': 'SYNTHETIC_LOOPBACK_REAL_BACKEND_TWO_FIREFOX_PROFILES',
              'productionRequests': 0, 'productionCredentialsUsed': False, 'pixelsSaved': False}
    sources = ('cloud/backend/app.py', 'cloud/backend/native_video.py', 'cloud/frontend/app.js',
               'cloud/frontend/native-player.js', 'cloud/frontend/media.js', 'cloud/frontend/video-lease.js',
               'cloud/frontend/index.html')
    result['sourceDigests'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}

    def record(name, **values):
        phases.append({'name': name, **values})
        (destination / 'progress.json').write_text(json.dumps(phases, indent=2))

    def rows(browser):
        return browser.evaluate('return __qaRows()')

    def growth(browser, seconds=3, minimum=20):
        before = rows(browser)
        time.sleep(seconds)
        after = rows(browser)
        assert all(y['decoded'] - x['decoded'] >= minimum and y['width'] == 640 and not y['hidden']
                   for x, y in zip(before, after)), (before, after)
        return after

    def metrics():
        return request(base + '/qa/metrics')

    def wait_sources(expected, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = metrics()
            if sum(value['activeSources'].values()) == expected:
                return value
            time.sleep(.2)
        raise AssertionError({'expectedActive': expected, 'metrics': value})

    try:
        env = {**os.environ, 'LIBGL_ALWAYS_SOFTWARE': '1', 'MOZ_WEBRENDER': '1'}
        authority = Path('/run/user/1000/gdm/Xauthority')
        if not env.get('DISPLAY') and Path('/tmp/.X11-unix/X1').exists() and authority.exists():
            env.update(DISPLAY=':1', XAUTHORITY=str(authority))
        commands = [([sys.executable, '-B', '-m', 'tests.browser_shared_video_firefox', '--serve', '--port', str(port)], 'fixture')]
        profile_root = Path.home() / 'snap/firefox/common'
        for index, driver_port in enumerate(driver_ports):
            profile = Path(tempfile.mkdtemp(prefix='observer-shared-qa-', dir=profile_root))
            profiles.append(profile)
            commands.append((['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', str(driver_port),
                              '--websocket-port', str(driver_port + 100), '--profile-root', str(profile)], 'driver' + str(index)))
        for command, name in commands:
            log = (destination / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env))
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            try:
                request(base + '/healthz')
                for driver_port in driver_ports:
                    request(f'http://127.0.0.1:{driver_port}/status')
                break
            except (OSError, ValueError):
                time.sleep(.2)
        else:
            raise AssertionError('Isolated fixture/geckodriver startup failed')
        first = Browser(f'http://127.0.0.1:{driver_ports[0]}')
        browsers.append(first)
        first.login(base, USERNAME)
        first.playing()
        record('single_viewer_fixed_quality', rows=growth(first), metrics=metrics())
        second = Browser(f'http://127.0.0.1:{driver_ports[1]}')
        browsers.append(second)
        second.login(base, SECOND_USER)
        second.playing()
        first.playing()
        initial = [rows(first), rows(second)]
        for index in range(6):
            time.sleep(5)
            current = [rows(first), rows(second)]
            for before, after in zip(initial, current):
                assert all(y['decoded'] - x['decoded'] >= 30 and y['width'] == 640 and not y['hidden']
                    for x, y in zip(before, after)), (index, before, after)
                assert all(x['requests'] == y['requests'] for x, y in zip(before, after)), (before, after)
            initial = current
        shared = metrics()
        fanout = first.call('/execute/async', {'script': 'const done=arguments[arguments.length-1];fetch("/native-camera/status").then(r=>r.json()).then(done)', 'args': []})['fanout']
        assert fanout['upstreams'] == 4 and fanout['subscribers'] == 8, fanout
        assert all(v['upstreams'] == 1 and v['subscribers'] == 2 for v in fanout['cameras'].values()), fanout
        assert all(shared['sourceRequests'].get(identity) == 1 for identity, _ in CAMERAS), shared
        assert all(shared['activeSources'].get(identity) == 1 for identity, _ in CAMERAS), shared
        assert all(x['streamSession'] == y['streamSession'] for x, y in zip(*initial)), initial
        record('two_independent_accounts_decode_30_seconds_fixed_quality_one_upstream', rows=initial, metrics=shared, fanout=fanout)

        second.evaluate("RKObserverDashboard.openPage('overview')")
        second.wait('window.__qaFetches.filter(r=>/video\\.mp4$/.test(r.path)).every(r=>r.aborted)')
        growth(first)
        second.evaluate("RKObserverDashboard.openPage('media/cameras')")
        second.playing()
        assert all(metrics()['sourceRequests'].get(identity) == 1 for identity, _ in CAMERAS)
        record('late_join_cached_gop_no_source_restart', rows=growth(second), metrics=metrics())

        # Actual Firefox background tab visibility, not a fabricated browser event.
        second_handle = second.call('/window', method='GET')
        blank = second.call('/window/new', {'type': 'tab'})['handle']
        second.call('/window', {'handle': blank})
        second.call('/url', {'url': 'about:blank'})
        second.activate(blank)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            visibility = second.background_value(second_handle, '({hidden:document.hidden,lease:ObserverVideoLease.state(),connection:RKObserverDashboard.getConnection()?.readyState??null,fetches:__qaFetches.filter(r=>/video\\.mp4$/.test(r.path))})')
            if visibility['hidden'] and not visibility['lease']['active'] and visibility['connection'] is None and all(row['aborted'] for row in visibility['fetches']):
                break
            time.sleep(.2)
        else:
            raise AssertionError({'actualBackground': visibility})
        resumed_at = second.background_value(second_handle, 'performance.now()')
        second.activate(second_handle)
        second.call('/window', {'handle': second_handle})
        second.playing()
        second.wait('RKObserverDashboard.getConnection()?.readyState===WebSocket.OPEN')
        second.wait('__qaSnapshots.some(s=>s.at>' + str(resumed_at) + '&&Date.now()/1000-s.timestamp<5)')
        record('real_background_tab_stops_video_and_telemetry_return_resumes', background=visibility, rows=growth(second), metrics=metrics())

        # Two visible windows within one Firefox profile share cookies and locks.
        original = first.call('/window', method='GET')
        duplicate = first.call('/window/new', {'type': 'window'})['handle']
        first.call('/window', {'handle': duplicate})
        first.call('/url', {'url': base + '/#media/cameras'})
        first.wait('!!window.ObserverVideoLease&&!!window.__qaRows')
        first.wait('!!document.getElementById("video-lease-takeover")')
        duplicate_lease = first.evaluate('return ObserverVideoLease.state()')
        first.evaluate('document.getElementById("video-lease-takeover").click()')
        first.playing()
        growth(second)
        first.call('/window', {'handle': original})
        first.wait('!ObserverVideoLease.state().active')
        first.wait('window.__qaFetches.filter(r=>/video\\.mp4$/.test(r.path)).every(r=>r.aborted)')
        first.evaluate('document.getElementById("video-lease-takeover").click()')
        first.playing()
        first.call('/window', {'handle': duplicate})
        first.wait('!ObserverVideoLease.state().active')
        # Schedule both visible contexts against the same wall clock. Their
        # broadcast messages may cross; a unique owner must still emerge.
        takeover_at = int(time.time() * 1000) + 750
        for context in (original, duplicate):
            first.background_value(context, '(setTimeout(()=>ObserverVideoLease.takeover(),Math.max(0,' + str(takeover_at) + '-Date.now())),true)')
        time.sleep(1.2)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            concurrent = [first.background_value(context, 'ObserverVideoLease.state()') for context in (original, duplicate)]
            if sum(int(state['active']) for state in concurrent) == 1:
                break
            time.sleep(.2)
        else:
            raise AssertionError({'concurrentTakeoverStates': concurrent})
        winner = original if concurrent[0]['active'] else duplicate
        first.call('/window', {'handle': winner})
        first.playing()
        winning_rows = growth(first)
        stable = [first.background_value(context, 'ObserverVideoLease.state()') for context in (original, duplicate)]
        assert sum(int(state['active']) for state in stable) == 1, stable
        assert stable[0]['active'] == concurrent[0]['active'], (concurrent, stable)
        for context in (original, duplicate):
            statuses = first.background_value(context, '__qaFetches.filter(r=>r.path.endsWith("/video.mp4")).map(r=>r.status??null)')
            assert not any(status in (401, 403, 429) for status in statuses), (context, statuses)
        record('simultaneous_same_browser_takeover_settles_one_owner', states=stable,
               winningRows=winning_rows, metrics=metrics())
        first.call('/window', {'handle': original})
        if not first.evaluate('return ObserverVideoLease.state().active'):
            first.evaluate('document.getElementById("video-lease-takeover").click()')
        first.playing()
        first.call('/window', {'handle': duplicate})
        first.wait('!ObserverVideoLease.state().active')
        first.call('/window', method='DELETE')
        first.call('/window', {'handle': original})
        first.playing()
        assert all(metrics()['sourceRequests'].get(identity) == 1 for identity, _ in CAMERAS)
        record('same_browser_duplicate_window_takeover_without_extra_source', duplicateInitialLease=duplicate_lease,
               rows=growth(first), metrics=metrics())

        # A real history navigation exercises pagehide and pageshow. Firefox
        # may reload rather than use BFCache for a page with active MediaSource;
        # record that outcome explicitly instead of pretending a cache hit.
        first.call('/url', {'url': base + '/users'})
        first.wait('location.pathname==="/users"')
        growth(second)
        first.call('/back', {})
        first.wait('!!window.__qaRows&&location.hash==="#media/cameras"')
        first.playing()
        first.wait('RKObserverDashboard.getConnection()?.readyState===WebSocket.OPEN')
        assert all(metrics()['sourceRequests'].get(identity) == 1 for identity, _ in CAMERAS)
        record('history_navigation_stops_then_restores_camera_page', rows=growth(first),
               pageShows=first.evaluate('return __qaPageShows'), metrics=metrics())

        previous_sessions = [row['streamSession'] for row in rows(first)]
        request(base + '/qa/outage', 'POST', {'outage': True})
        time.sleep(3)
        request(base + '/qa/outage', 'POST', {'outage': False})
        first.playing()
        second.playing()
        first.wait('__qaRows().every((r,i)=>r.streamSession!==' + json.dumps(previous_sessions) + '[i])', 35)
        second.wait('__qaRows().every((r,i)=>r.streamSession!==' + json.dumps(previous_sessions) + '[i])', 35)
        first.playing()
        second.playing()
        record('source_disconnect_and_recovery_changes_session', first=growth(first), second=growth(second), metrics=metrics())

        # Logging out the second account immediately revokes only its requests.
        logout = second.call('/execute/async', {'script': 'const done=arguments[arguments.length-1];fetch("/api/logout",{method:"POST"}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}))', 'args': []})
        assert logout['status'] == 200, logout
        second.wait('location.pathname==="/login"', 15)
        first.playing()
        record('logout_other_account_preserves_first', rows=growth(first), metrics=metrics())
        transport = first.evaluate('return __qaFetches.filter(r=>/video\\.mp4$/.test(r.path)).map(({path,status,error})=>({path,status,error}))')
        assert not any(row.get('status') in (401, 403, 429) for row in transport), transport
        errors = first.evaluate('return __qaErrors')
        assert not errors, errors
        first.evaluate("RKObserverDashboard.openPage('overview')")
        final = wait_sources(0)
        record('last_viewer_exit_closes_all_upstreams', metrics=final)
        result.update(status='PASS', phases=phases, transport=transport, scriptErrors=errors,
                      fixedQuality={'width': 640, 'height': 360, 'fps': 10, 'bitrate': 350000},
                      continuousDualViewerSeconds=30)
    except Exception as error:
        diagnostics = []
        for browser in browsers:
            try:
                diagnostics.append(browser.evaluate('return window.__qaDiagnostics?.()||{path:location.pathname}'))
            except Exception as capture_error:
                diagnostics.append({'error': str(capture_error)})
        result.update(failure=str(error), phases=phases, browsers=diagnostics)
        try:
            result['metricsAtFailure'] = metrics()
        except Exception:
            pass
        raise
    finally:
        result['sourceUnchangedDuringRun'] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected for name, expected in result['sourceDigests'].items())
        (destination / 'results.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key not in ('phases', 'browsers')}), flush=True)
        for browser in reversed(browsers):
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
        for profile in profiles:
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', default=str(ROOT.parent / 'artifacts/shared-video-firefox'))
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=19318)
    parser.add_argument('--driver-ports', type=int, nargs=2, default=(19319, 19320))
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.port))
    else:
        verify(Path(args.destination), args.port, args.driver_ports)
