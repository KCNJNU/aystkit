"""Two independent Firefox viewers, synthetic sources, real cloud backend.

No production credentials, robot pixels, device traffic or cloud deployment.
"""
import argparse
import asyncio
import base64
import collections
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

from cloud.backend.app import NATIVE_VIDEO, STORE, create_app
from cloud.backend.security import Config, hash_password
from tests.browser_native_video import mp4_parts
from tests.test_protocol import envelope, jpeg

ROOT = Path(__file__).resolve().parents[1]
KEYS = ('usbFront', 'usbLeft', 'usbHub3', 'usbHub2')
CAMERAS = (('camera2', 'front'), ('camera1', 'rear'), ('camera3', 'left'), ('camera4', 'right'))
PROFILES = {'standard': (640, 360, 10, 350000), 'balanced': (480, 270, 10, 200000),
            'compact': (384, 216, 8, 120000), 'economy': (320, 180, 6, 80000)}
USER, PASSWORD = 'syntheticviewer', 'isolated-adaptive-browser-only'
INSTRUMENT = '''<script>
window.__qaErrors=[];window.__qaFetches=[];window.__qaPlayers=[];window.__qaPolicies=[];window.__qaFrames=[];
addEventListener('error',e=>__qaErrors.push(String(e.message)));addEventListener('unhandledrejection',e=>__qaErrors.push(String(e.reason)));
const qaFetch=fetch.bind(window);window.fetch=async(path,options)=>{const row={path:String(path),at:performance.now()};__qaFetches.push(row);try{const r=await qaFetch(path,options);row.status=r.status;if(path==='/native-camera/status'&&r.ok){const v=await r.clone().json();if(v.adaptive)__qaPolicies.push({at:performance.now(),...v.adaptive});}return r;}catch(e){row.error=e.name;throw e;}};
const QaPlayer=NativeCameraPlayer;window.NativeCameraPlayer=class extends QaPlayer{constructor(v,o){super(v,{...o,onFrame:info=>{__qaFrames.push({key:v.id,at:performance.now(),width:v.videoWidth,session:info.streamSession});o?.onFrame?.(info);}});__qaPlayers.push(this);}};
window.__qaRows=()=>['usbFront','usbLeft','usbHub3','usbHub2'].map(key=>{const v=document.getElementById('media-'+key),p=__qaPlayers.filter(p=>p.video===v&&p.controller&&!p.controller.signal.aborted).at(-1);return{key,tag:v?.tagName,width:v?.videoWidth,height:v?.videoHeight,time:v?.currentTime,decoded:v?.getVideoPlaybackQuality?.().totalVideoFrames,paused:v?.paused,hidden:v?.hidden,phase:p?.phase||null,streamSession:p?.streamSession||null,error:v?.error?.code??null,requests:__qaFetches.filter(r=>r.path==='/native-camera/'+key+'/video.mp4').length};});
</script>'''


async def serve(port):
    with tempfile.TemporaryDirectory(prefix='adaptive-viewers-synthetic-') as temporary:
        directory = Path(temporary)
        generated = {}
        for name, (width, height, fps, bitrate) in PROFILES.items():
            path = directory / (name + '.mp4')
            process = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i', f'testsrc2=size={width}x{height}:rate={fps}', '-t', '150', '-an',
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g', str(max(2, fps // 2)), '-keyint_min', str(max(2, fps // 2)),
                '-sc_threshold', '0', '-b:v', str(bitrate), '-maxrate', str(bitrate), '-bufsize', str(bitrate),
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '200000',
                '-f', 'mp4', '-y', str(path), stderr=asyncio.subprocess.PIPE)
            _, error = await process.communicate()
            if process.returncode:
                raise RuntimeError('Synthetic encoder failed: ' + error.decode()[-500:])
            generated[name] = mp4_parts(path.read_bytes())
        active = collections.Counter()
        maximum = collections.Counter()
        requests = collections.Counter()
        active_profile = {identity: 'standard' for identity, _ in CAMERAS}
        sessions = {identity: uuid.uuid4().hex for identity, _ in CAMERAS}
        started = time.monotonic()

        async def status(request):
            rows = []
            for identity, direction in CAMERAS:
                width, height, fps, bitrate = PROFILES[active_profile[identity]]
                rows.append({'id': identity, 'direction': direction, 'width': width, 'height': height,
                    'codec': 'avc1.42c016', 'frames': int((time.monotonic() - started) * 25),
                    'encoded_frames': int((time.monotonic() - started) * fps), 'requested_fps': fps,
                    'actual_fps': 25, 'encoded_fps': fps, 'frame_age_ms': 10, 'h264_mbps': bitrate / 1e6,
                    'error': None, 'video_error': None})
            return web.json_response({'cameras': rows, 'cameraUsage': {key: {'owner': 'WEB', 'connected': True, 'checkedAgeMs': 0} for key in KEYS}})

        async def video(request):
            identity, profile = request.match_info['identity'], request.match_info['profile']
            if identity not in active_profile or profile not in PROFILES:
                raise web.HTTPNotFound()
            active_profile[identity] = profile
            session = uuid.uuid4().hex
            sessions[identity] = session
            key = identity + ':' + profile
            requests[key] += 1
            active[key] += 1
            maximum[key] = max(maximum[key], active[key])
            response = web.StreamResponse(headers={'Content-Type': 'video/mp4', 'X-Video-Session': session})
            try:
                await response.prepare(request)
                initial, fragments = generated[profile]
                await response.write(initial)
                # Mixed key/non-key fragments, matching the real .2s muxer.
                for index, fragment in enumerate(fragments):
                    if sessions[identity] != session:
                        break
                    if index:
                        await asyncio.sleep(.2)
                    await response.write(fragment)
                await response.write_eof()
            except (ConnectionError, RuntimeError, asyncio.CancelledError):
                pass
            finally:
                active[key] -= 1
            return response

        upstream = web.Application()
        upstream.add_routes([web.get('/status', status),
            web.get('/camera/{identity}/video-{profile}.mp4', video)])
        source_runner = web.AppRunner(upstream, access_log=None)
        await source_runner.setup()
        socket = str(directory / 'source.sock')
        await web.UnixSite(source_runner, socket).start()
        config = Config(robot_id='ISOLATED_ADAPTIVE_VIEWERS', device_credentials=[{
            'token': 'ISOLATED_UNUSED_PRIMARY_TOKEN_' + 'x' * 32, 'robot_id': 'ISOLATED_ADAPTIVE_VIEWERS',
            'source_id': 'TEST_ONLY', 'role': 'primary'}], viewer_username=USER,
            viewer_password_hash=hash_password(PASSWORD), session_secret='ISOLATED_BROWSER_SESSION_' + 'x' * 32,
            public_origin=f'http://127.0.0.1:{port}', data_dir=str(directory / 'db'), allow_insecure_local=True)
        with patch.dict(os.environ, {'NATIVE_VIDEO_SOCKET': socket, 'NATIVE_VIDEO_MAX_PLAYING': '4',
                                    'NATIVE_VIDEO_SPLIT_SOCKETS': 'false', 'NATIVE_VIDEO_ADAPTIVE': 'true'}):
            app = create_app(config)

        @web.middleware
        async def fixture_only(request, handler):
            if request.path == '/qa/instrument.js':
                return web.Response(text=INSTRUMENT.removeprefix('<script>').removesuffix('</script>'), content_type='application/javascript')
            if request.path == '/qa/metrics':
                return web.json_response({'sourceRequests': dict(requests), 'activeSources': dict(active),
                    'maxActiveSources': dict(maximum), 'profiles': dict(active_profile), 'sessions': dict(sessions),
                    'hubCount': len(app[NATIVE_VIDEO].hubs)})
            if request.path == '/qa/detections':
                return web.json_response({'syntheticOnly': True})
            response = await handler(request)
            if request.path == '/' and response.status == 200:
                html = (ROOT / 'cloud/frontend/index.html').read_text()
                html = html.replace('<script src="/media.js"></script>', '<script src="/qa/instrument.js"></script><script src="/media.js"></script>')
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
                obj = envelope('heartbeat', sequence=sequence, session=session)
                obj.update(robot_id='ISOLATED_ADAPTIVE_VIEWERS', source_id='TEST_ONLY')
                app[STORE].ingest(obj)
                for key in ('color', 'depth'):
                    sequence += 1
                    frame = envelope('media', sequence=sequence, session=session)
                    frame.update(robot_id='ISOLATED_ADAPTIVE_VIEWERS', source_id='TEST_ONLY', topic='/observer_camera/' + key + '/compressed')
                    frame['payload'] = {'key': key, 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                        'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg', 'frameSeq': sequence}
                    app[STORE].ingest(frame)
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
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def verify(destination, port, driver_ports):
    destination.mkdir(parents=True, exist_ok=True)
    profiles, processes, logs, browsers = [], [], [], []
    base = f'http://127.0.0.1:{port}'
    phases = []
    try:
        commands = [([sys.executable, '-B', '-m', 'tests.browser_adaptive_viewers_firefox', '--serve', '--port', str(port)], 'fixture')]
        for index, driver_port in enumerate(driver_ports):
            root = Path.home() / 'snap/firefox/common'
            root.mkdir(parents=True, exist_ok=True)
            profile = Path(tempfile.mkdtemp(prefix='observer-adaptive-' + str(index) + '-', dir=root))
            profiles.append(profile)
            commands.append((['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', str(driver_port), '--profile-root', str(profile)], 'driver' + str(index)))
        for command, name in commands:
            log = (destination / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                request(base + '/healthz')
                for driver_port in driver_ports:
                    request(f'http://127.0.0.1:{driver_port}/status')
                break
            except (OSError, ValueError):
                time.sleep(.2)
        else:
            raise AssertionError('Isolated fixture did not start')

        def create_browser(driver_port):
            driver = f'http://127.0.0.1:{driver_port}'
            created = request(driver + '/session', 'POST', {'capabilities': {'alwaysMatch': {'browserName': 'firefox',
                'moz:firefoxOptions': {'binary': '/snap/firefox/current/usr/lib/firefox/firefox', 'args': ['-headless'],
                    'prefs': {'media.autoplay.default': 0}}}}}, 55)['value']
            session = created['sessionId']

            def call(endpoint, data=None, method='POST'):
                return request(driver + '/session/' + session + endpoint, method, data)['value']

            def evaluate(code):
                return call('/execute/sync', {'script': code, 'args': []})

            browser = {'driver': driver, 'session': session, 'call': call, 'evaluate': evaluate}
            browsers.append(browser)
            return browser

        def wait(browser, script, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    value = browser['evaluate']('return (' + script + ')')
                    if value:
                        return value
                except (OSError, ValueError):
                    pass
                time.sleep(.15)
            raise AssertionError({'expression': script, 'rows': browser['evaluate']('return window.__qaRows?.()'),
                'errors': browser['evaluate']('return window.__qaErrors'), 'metrics': request(base + '/qa/metrics')})

        def login(browser):
            browser['call']('/url', {'url': base + '/login'})
            wait(browser, "!!document.getElementById('login-form')")
            browser['evaluate']('document.getElementById("username").value=' + json.dumps(USER) + ';document.getElementById("password").value=' + json.dumps(PASSWORD) + ';document.getElementById("login-form").requestSubmit();')
            wait(browser, '!!window.RKObserverDashboard&&!!window.__qaRows')
            browser['evaluate']("RKObserverDashboard.openPage('media/cameras')")

        def playing(browser, width):
            return wait(browser, '__qaRows().every(r=>r.width===' + str(width) + '&&r.phase==="playing"&&!r.paused&&r.decoded>5&&!r.error)', 35)

        def record(name, **values):
            value = {'name': name, **values}
            phases.append(value)
            (destination / 'progress.json').write_text(json.dumps(phases, indent=2))
            return value

        first = create_browser(driver_ports[0])
        login(first)
        playing(first, 640)
        a = first['evaluate']('return __qaRows()')
        time.sleep(1.3)
        b = first['evaluate']('return __qaRows()')
        assert all(y['decoded'] > x['decoded'] for x, y in zip(a, b))
        standard = request(base + '/qa/metrics')
        assert all(standard['sourceRequests'].get(identity + ':standard') == 1 for identity, _ in CAMERAS), standard
        record('single_viewer_standard', rows=b, metrics=standard, badge=first['evaluate']('return document.getElementById("camera-adaptive-status").textContent'))
        second = create_browser(driver_ports[1])
        login(second)
        playing(first, 480)
        playing(second, 480)
        a = [browser['evaluate']('return __qaRows()') for browser in (first, second)]
        time.sleep(3)
        b = [browser['evaluate']('return __qaRows()') for browser in (first, second)]
        balanced = request(base + '/qa/metrics')
        for initial, final in zip(a, b):
            assert all(y['decoded'] - x['decoded'] >= 20 and not y['hidden'] for x, y in zip(initial, final)), (initial, final)
            assert all(y['requests'] == x['requests'] for x, y in zip(initial, final)), (initial, final)
        assert all(balanced['sourceRequests'].get(identity + ':balanced') == 1 for identity, _ in CAMERAS), balanced
        assert all(value <= 1 for value in balanced['maxActiveSources'].values()), balanced
        # Both authenticated windows decode the same shared encoder session.
        assert all(x['streamSession'] == y['streamSession'] for x, y in zip(b[0], b[1])), b
        record('two_independent_viewers_balanced', rows=b, metrics=balanced,
            badges=[browser['evaluate']('return document.getElementById("camera-adaptive-status").textContent') for browser in (first, second)])
        # A real late join without a policy transition is exercised by hiding
        # and returning one viewer during its existing 10s active lease.
        second['evaluate']("RKObserverDashboard.openPage('overview')")
        time.sleep(1.3)
        second['evaluate']("RKObserverDashboard.openPage('media/cameras')")
        playing(second, 480)
        late = request(base + '/qa/metrics')
        assert all(late['sourceRequests'].get(identity + ':balanced') == 1 for identity, _ in CAMERAS), late
        record('late_join_reuses_one_source', rows=second['evaluate']('return __qaRows()'), metrics=late)
        second['evaluate']("RKObserverDashboard.openPage('overview')")
        closed_at = time.monotonic()
        a = first['evaluate']('return __qaRows()')
        time.sleep(5)
        b = first['evaluate']('return __qaRows()')
        assert all(y['decoded'] - x['decoded'] >= 35 and y['width'] == 480 for x, y in zip(a, b)), (a, b)
        time.sleep(12)
        unchanged = first['evaluate']('return __qaRows()')
        assert all(row['width'] == 480 for row in unchanged), unchanged
        record('remaining_viewer_continues_during_lease_and_hysteresis', elapsedSeconds=round(time.monotonic() - closed_at, 3), rows=unchanged, metrics=request(base + '/qa/metrics'))
        playing(first, 640)
        upgraded_after = time.monotonic() - closed_at
        assert upgraded_after >= 27, upgraded_after
        upgraded = first['evaluate']('return __qaRows()')
        record('hysteresis_restores_standard', elapsedSeconds=round(upgraded_after, 3), rows=upgraded,
            badge=first['evaluate']('return document.getElementById("camera-adaptive-status").textContent'), metrics=request(base + '/qa/metrics'))
        first['evaluate']("RKObserverDashboard.openPage('overview')")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            final = request(base + '/qa/metrics')
            if not sum(final['activeSources'].values()):
                break
            time.sleep(.2)
        assert sum(final['activeSources'].values()) == 0, final
        transport = []
        for browser in (first, second):
            transport.append(browser['evaluate']('''return {videoResponses:__qaFetches.filter(row=>/video\.mp4$/.test(row.path)).map(row=>({path:row.path,status:row.status,error:row.error})),
              profileFirstFrameDelayMs:__qaPolicies.filter((row,index,list)=>index===0||row.profile!==list[index-1].profile).map(row=>{
                const frame=__qaFrames.find(f=>f.at>=row.at&&f.width===row.maxSide);return{profile:row.profile,viewers:row.viewers,
                  delayMs:frame?frame.at-row.at:null};})};'''))
        errors = [browser['evaluate']('return __qaErrors') for browser in (first, second)]
        assert not any(errors), errors
        result = {'status': 'PASS', 'scope': 'TWO_INDEPENDENT_FIREFOX_SESSIONS_REAL_BACKEND_SYNTHETIC_LOOPBACK_ONLY',
            'fourCamerasPerViewer': True, 'lateJoinDecoded': True, 'singleUpstreamPerCameraProfile': True,
            'oneViewerExitPreservesOtherPlayback': True, 'sourceClosedAfterLastViewer': True,
            'hysteresisUpgradeDelaySeconds': round(upgraded_after, 3), 'transport': transport, 'phases': phases, 'final': final, 'scriptErrors': errors,
            'productionRequests': 0, 'productionCredentialsUsed': False, 'pixelsSaved': False}
        (destination / 'results.json').write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != 'phases'}))
    except Exception:
        diagnostics=[]
        for browser in browsers:
            try:
                diagnostics.append(browser['evaluate']('return {location:location.pathname,route:window.RKObserverDashboard?.getRoute?.(),documentHidden:document.hidden,\n                  rows:window.__qaRows?.(),fetches:window.__qaFetches,policies:window.__qaPolicies,errors:window.__qaErrors,\n                  frames:window.__qaFrames?.slice(-12),badge:document.getElementById("camera-adaptive-status")?.textContent};'))
            except Exception as error:
                diagnostics.append({'diagnosticError':type(error).__name__})
        try:metrics=request(base+'/qa/metrics')
        except Exception:metrics={}
        (destination/'failure.json').write_text(json.dumps({'browsers':diagnostics,'metrics':metrics},indent=2))
        raise
    finally:
        for browser in reversed(browsers):
            try:
                request(browser['driver'] + '/session/' + browser['session'], 'DELETE')
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
            shutil.rmtree(profile)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', default='/tmp/adaptive-viewers-firefox')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=19226)
    parser.add_argument('--driver-ports', type=int, nargs=2, default=(19228, 19229))
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.port))
    else:
        verify(Path(args.destination), args.port, args.driver_ports)
