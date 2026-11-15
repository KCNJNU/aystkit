"""Four-stream synthetic Firefox MSE jitter/recovery acceptance.

Uses only generated pixels, loopback fixtures, and a dedicated temporary browser
profile. Never reads production credentials or connects to robot/cloud video.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

from aiohttp import web

from tests.browser_native_video import mp4_parts

ROOT = Path(__file__).resolve().parents[1]
KEYS = ('usbFront', 'usbLeft', 'usbHub3', 'usbHub2')
HTML = '''<!doctype html><meta charset="utf-8"><title>ISOLATED SYNTHETIC VIDEO JITTER</title>
<style>body{background:#07141c;color:white;display:grid;grid-template-columns:repeat(2,640px)}video{width:640px;height:360px}</style><body>
<script>window.__errors=[];addEventListener('error',e=>__errors.push(String(e.message)));addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
window.__requests={};window.__states=[];const nativeFetch=fetch.bind(window);window.fetch=(path,opts)=>{__requests[path]=(__requests[path]||0)+1;return nativeFetch(path,opts);};</script>
<script src="/native-player.js"></script><script>
window.__players=[];window.__samples=[];window.__naturalWaiting={};
for(const key of ['usbFront','usbLeft','usbHub3','usbHub2']){
 const v=document.createElement('video');v.id=key;document.body.appendChild(v);
 v.addEventListener('waiting',()=>__naturalWaiting[key]=(__naturalWaiting[key]||0)+1);
 const p=new NativeCameraPlayer(v,{isCurrent:()=>true,onState:(status,row)=>__states.push({key,at:performance.now(),status,...row})});
 __players.push(p);p.start('/native-camera/'+key+'/video.mp4');
}
window.__rows=()=>__players.map(p=>{const v=p.video,q=v.getVideoPlaybackQuality();return{
 key:v.id,at:performance.now(),decoded:q.totalVideoFrames,dropped:q.droppedVideoFrames,
 fps:p.decodedFps(),time:v.currentTime,paused:v.paused,hidden:v.hidden,width:v.videoWidth,height:v.videoHeight,
 phase:p.phase,targetBuffer:p.targetBuffer,ahead:v.buffered.length?v.buffered.end(v.buffered.length-1)-v.currentTime:null,
 waitingEvents:__naturalWaiting[v.id]||0,requests:__requests['/native-camera/'+v.id+'/video.mp4']||0,error:v.error?.code??null};});
window.__sampleTimer=setInterval(()=>__samples.push(...__rows()),200);
window.__stop=()=>{clearInterval(__sampleTimer);__players.forEach(p=>p.stop());};
</script>'''


async def serve(port):
    state = {'mode': 'steady', 'pauseUntil': 0.0}
    with tempfile.TemporaryDirectory(prefix='synthetic-video-jitter-') as directory:
        path = Path(directory) / 'generated.mp4'
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'testsrc2=size=640x360:rate=10', '-t', '150', '-an', '-c:v', 'libx264',
            '-threads', '1', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g', '2', '-keyint_min', '2',
            '-sc_threshold', '0', '-b:v', '350k', '-maxrate', '350k', '-bufsize', '350k',
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-f', 'mp4', '-y', str(path),
            stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError('Synthetic generation failed: ' + error.decode()[-500:])
        initial, fragments = mp4_parts(path.read_bytes())

        async def index(request):
            return web.Response(text=HTML, content_type='text/html')

        async def script(request):
            return web.FileResponse(ROOT / 'cloud/frontend/native-player.js')

        async def control(request):
            value = await request.json()
            assert value.get('mode', 'steady') in ('steady', 'bursty')
            state['mode'] = value.get('mode', 'steady')
            state['pauseUntil'] = time.monotonic() + min(max(float(value.get('pauseSeconds', 0)), 0), 8)
            return web.json_response({'mode': state['mode'], 'syntheticOnly': True})

        async def video(request):
            if request.match_info['key'] not in KEYS:
                raise web.HTTPNotFound()
            response = web.StreamResponse(headers={'Content-Type': 'video/mp4', 'Cache-Control': 'no-store'})
            try:
                await response.prepare(request)
                await response.write(initial)
                cursor = 0
                while cursor < len(fragments):
                    while time.monotonic() < state['pauseUntil']:
                        await asyncio.sleep(.025)
                    if cursor < 3:
                        count, delay = 1, 0
                    elif state['mode'] == 'bursty':
                        count = 4 if (cursor // 4) % 2 else 5
                        delay = count * .2
                    else:
                        count, delay = 1, .2
                    await asyncio.sleep(delay)
                    for fragment in fragments[cursor:cursor + count]:
                        await response.write(fragment)
                    cursor += count
                await response.write_eof()
            except (ConnectionError, RuntimeError, asyncio.CancelledError):
                pass
            return response

        app = web.Application()
        app.add_routes([web.get('/', index), web.get('/native-player.js', script),
                        web.post('/fixture-control', control), web.get('/native-camera/{key}/video.mp4', video)])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method='GET', data=None, timeout=20):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def stats(values):
    values = sorted(value for value in values if value is not None)
    return None if not values else {'min': round(values[0], 3), 'median': round(statistics.median(values), 3),
                                    'max': round(values[-1], 3)}


def verify(destination, port, driver_port):
    destination.mkdir(parents=True, exist_ok=True)
    profile_root = Path.home() / 'snap/firefox/common'
    profile_root.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix='observer-native-jitter-', dir=profile_root))
    base, driver = f'http://127.0.0.1:{port}', f'http://127.0.0.1:{driver_port}'
    processes, logs, session = [], [], None
    phases = []
    try:
        for command, name in (([sys.executable, '-B', '-m', 'tests.browser_native_jitter_firefox', '--serve', '--port', str(port)], 'fixture'),
                              (['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', str(driver_port), '--profile-root', str(profile)], 'driver')):
            log = (destination / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                request(driver + '/status')
                with OPENER.open(base, timeout=1) as response:
                    assert response.status == 200
                break
            except (OSError, ValueError):
                time.sleep(.2)
        else:
            raise AssertionError('Isolated fixture/driver did not start')
        created = request(driver + '/session', 'POST', {'capabilities': {'alwaysMatch': {'browserName': 'firefox',
            'moz:firefoxOptions': {'binary': '/snap/firefox/current/usr/lib/firefox/firefox', 'args': ['-headless'],
                                   'prefs': {'media.autoplay.default': 0}}}}}, 55)['value']
        session = created['sessionId']

        def call(endpoint, data=None, method='POST'):
            return request(driver + '/session/' + session + endpoint, method, data)['value']

        def evaluate(code):
            return call('/execute/sync', {'script': code, 'args': []})

        def rows():
            return evaluate('return __rows()')

        def wait_playing(seconds=20):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                current = rows()
                if all(row['phase'] == 'playing' and not row['paused'] and row['decoded'] > 2 for row in current):
                    return current
                time.sleep(.1)
            raise AssertionError({'notPlaying': rows(), 'errors': evaluate('return __errors')})

        def phase(name, seconds):
            start = rows()
            began = evaluate('return performance.now()')
            time.sleep(seconds)
            end = rows()
            samples = evaluate('return __samples.filter(row=>row.at>=' + str(began) + ')')
            report = {'name': name, 'durationSeconds': seconds, 'cameras': {}}
            for first, last in zip(start, end):
                current = [row for row in samples if row['key'] == last['key']]
                report['cameras'][last['key']] = {'decodedDelta': last['decoded'] - first['decoded'],
                    'timeDelta': round(last['time'] - first['time'], 3), 'requestsDelta': last['requests'] - first['requests'],
                    'naturalWaitingEventsDelta': last['waitingEvents'] - first['waitingEvents'],
                    'playingFraction': round(sum(row['phase'] == 'playing' for row in current) / max(len(current), 1), 4),
                    'pausedSamples': sum(row['paused'] for row in current), 'hiddenSamples': sum(row['hidden'] for row in current),
                    'zeroWidthSamples': sum(not row['width'] for row in current), 'targetBuffer': stats([row['targetBuffer'] for row in current]),
                    'bufferAheadS': stats([row['ahead'] for row in current]), 'decodedFps': stats([row['fps'] for row in current]),
                    'last': last}
            phases.append(report)
            (destination / 'progress.json').write_text(json.dumps(phases, indent=2))
            return report

        call('/url', {'url': base})
        wait_playing()
        steady = phase('steady_10fps', 6)
        request(base + '/fixture-control', 'POST', {'mode': 'bursty'})
        burst = phase('four_stream_0.8_to_1s_bursts', 24)
        for row in burst['cameras'].values():
            assert row['decodedDelta'] >= 180 and row['requestsDelta'] == 0, burst
            assert row['hiddenSamples'] == 0 and row['zeroWidthSamples'] == 0, burst
            assert row['targetBuffer']['max'] <= 1.000001, burst
            # A naturally absorbed burst need not increase the target. When
            # a sampled low-water event occurs, adaptation must be visible.
            assert row['targetBuffer']['max'] > .4 or row['bufferAheadS']['min'] >= .05, burst
            row['adaptiveGrowthObserved'] = row['targetBuffer']['max'] > .4
            row['adaptationNote'] = ('low-water target increased' if row['adaptiveGrowthObserved']
                                     else 'bursts absorbed without reaching the .05s low-water threshold')
        requests_before = {row['key']: row['requests'] for row in rows()}
        request(base + '/fixture-control', 'POST', {'mode': 'steady', 'pauseSeconds': 2.5})
        short_outage = phase('natural_waiting_2.5s_outage', 3)
        for row in short_outage['cameras'].values():
            assert row['naturalWaitingEventsDelta'] > 0, short_outage
            assert row['targetBuffer']['max'] > .4, short_outage
        wait_playing(10)
        resumed_short = phase('after_2.5s_outage', 2)
        for row in resumed_short['cameras'].values():
            assert row['decodedDelta'] >= 12 and row['last']['requests'] == requests_before[row['last']['key']], resumed_short
        request(base + '/fixture-control', 'POST', {'mode': 'steady', 'pauseSeconds': 7.8})
        outage = phase('short_supply_outage', 8)
        for row in outage['cameras'].values():
            assert row['targetBuffer']['max'] > .4, outage
            assert row['naturalWaitingEventsDelta'] > 0, outage
        wait_playing(12)
        recovery = phase('after_supply_restored', 6)
        for row in recovery['cameras'].values():
            assert row['decodedDelta'] >= 45 and not row['last']['paused'] and not row['last']['error'], recovery
            assert row['last']['requests'] == requests_before[row['last']['key']], recovery
        # Exercise the exact stall boundary using a real decoded MSE buffer.
        evaluate('const p=__players[0];p.video.pause();p.started=true;')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if evaluate('const p=__players[0],v=p.video;return v.buffered.length&&v.buffered.end(v.buffered.length-1)-v.currentTime>=p.targetBuffer;'):
                break
            time.sleep(.05)
        before = rows()[0]
        evaluate('const p=__players[0];p.video.pause();p.lastFrameAt=performance.now()-8001;p.lastAppendAt=performance.now();p.recoveryFrameAt=null;p.tick(p.epoch);')
        time.sleep(2)
        boundary = rows()[0]
        assert boundary['decoded'] > before['decoded'] and not boundary['paused'], boundary
        assert boundary['requests'] == before['requests'], boundary
        errors = evaluate('return __errors')
        states = evaluate('return __states')
        assert not errors, errors
        report = {'status': 'PASS', 'scope': 'FOUR_SYNTHETIC_LOOPBACK_FIREFOX_MSE_NO_PRODUCTION_TRAFFIC',
                  'sourceFps': 10, 'phases': phases, 'stallBoundary': {'before': before, 'after': boundary,
                    'realMseBufferResumedBeforeReconnect': True}, 'stateChanges': states, 'errors': errors,
                  'productionCredentialsUsed': False, 'pixelsSaved': False}
        evaluate('__stop()')
        (destination / 'results.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        if session:
            try:
                request(driver + '/session/' + session, 'DELETE')
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
        shutil.rmtree(profile)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?', default='/tmp/native-jitter-firefox-results')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=19166)
    parser.add_argument('--driver-port', type=int, default=19168)
    args = parser.parse_args()
    if not 19000 <= args.port <= 19999 or not 19000 <= args.driver_port <= 19999:
        raise RuntimeError('Use dedicated isolated loopback ports')
    if args.serve:
        asyncio.run(serve(args.port))
    else:
        verify(Path(args.destination), args.port, args.driver_port)
