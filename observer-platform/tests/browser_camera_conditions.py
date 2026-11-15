"""Chromium timing regression for camera conditional GET, isolated fixtures.

Run only against tests.serve_browser and a separate temporary Chromium profile.
Delayed timing responses exist only in this test tab. One synthetic marker JPEG
is uploaded solely to the ephemeral loopback backend for native 304 verification.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request

import websockets

from tests.browser_cloud import FIXTURE_SCRIPT, local_test_url
from tests.test_protocol import envelope, jpeg


FIXTURE = FIXTURE_SCRIPT + r'''
  window.__httpCameraFixture=true;window.__epoch=null;window.__fixedFrame=null;
  window.__scenario={name:'idle',cadenceMs:500,baseAge:.55,phaseMs:0,delayMs:0};
  window.__requests=[];window.__samples=[];window.__forceBad304=false;
  window.__conditionKeys=['usbFront'];
  const frameAt=now=>{
    if(__fixedFrame)return {...__fixedFrame,age:__scenario.resetAge?.04:__fixedFrame.age+(now-__fixedFrame.observed)/1000};
    const elapsed=now-__epoch;return {seq:100+Math.floor(elapsed/__scenario.cadenceMs),
      age:__scenario.baseAge+(elapsed%__scenario.cadenceMs)/1000,observed:now};
  };
  window.__httpSnapshotMetadata=key=>{
    if(!__conditionKeys.includes(key))return {status:'WAITING_UPSTREAM'};
    if(__epoch===null)return {status:'AVAILABLE',ageS:0};
    const frame=frameAt(performance.now());
    return {status:'AVAILABLE',ageS:frame.age,frameIdentity:__scenario.name+'-'+key+':'+frame.seq,frameSeq:frame.seq};
  };
  const nativeFetch=fetch.bind(window),surface=document.createElement('canvas');
  surface.width=320;surface.height=180;const context=surface.getContext('2d');
  context.fillStyle='#305968';context.fillRect(0,0,320,180);context.font='18px sans-serif';
  context.fillStyle='#ffeacd';context.fillText('ISOLATED CONDITIONS',28,95);
  const jpeg=Uint8Array.from(atob(surface.toDataURL('image/jpeg').split(',')[1]),c=>c.charCodeAt(0));
  window.fetch=(path,options)=>{
    const key=String(path).match(/^\/camera\/([A-Za-z0-9]+)\.jpg$/)?.[1];
    if(!__conditionKeys.includes(key))return nativeFetch(path,options);
    const now=performance.now();if(__epoch===null)__epoch=now-__scenario.phaseMs;
    const frame=frameAt(now),scenario=__scenario,index=__requests.length;
    const tag='"fixture-'+scenario.name+'-'+key+'-'+frame.seq+'"';
    const validator=new Headers(options?.headers).get('If-None-Match');
    const delay=scenario.jitterMs?scenario.jitterMs[index%scenario.jitterMs.length]:scenario.delayMs;
    let status=frame.age>=2?204:!scenario.noTag&&validator===tag?304:200;
    if(scenario.dropEvery&&index>0&&index%scenario.dropEvery===0)status=204;
    if(__forceBad304){status=304;window.__forceBad304=false;}
    const headers={'X-Frame-Sequence':scenario.name+'-'+key+':'+frame.seq,
      'X-Frame-Age-S':String(frame.age),'X-Frame-TTL-S':'2'};
    if(status===200)headers['Content-Type']='image/jpeg';
    if(!scenario.noTag&&(status===200||status===304))headers.ETag=tag;
    const response=new Response(status===200?jpeg:null,{status,headers});
    const record={key,started:now,seq:frame.seq,serverAge:frame.age,validator,status,bytes:status===200?jpeg.length:0,delay};
    __requests.push(record);
    return new Promise(resolve=>setTimeout(()=>{record.delivered=performance.now();resolve(response);},delay));
  };
  window.__cameraState=(key='usbFront')=>{
    const image=document.getElementById('media-'+key),overlay=document.getElementById('media-empty-'+key);
    return {now:performance.now(),hidden:image.hidden,retained:overlay.classList.contains('is-stale'),
      live:!image.hidden&&overlay.hidden,src:image.getAttribute('src'),
      recording:document.querySelector('[data-record='+key+']').getAttribute('aria-pressed')==='true',
      disabled:document.querySelector('[data-record='+key+']').disabled};
  };
'''


async def main():
    server = local_test_url('CLOUD_TEST_URL', 'http://127.0.0.1:19088')
    cdp = local_test_url('CLOUD_CDP_URL', 'http://127.0.0.1:19089')
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('tests/artifacts/camera-conditions')
    destination.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(cdp + '/json/new?about:blank', method='PUT'), timeout=5) as response:
        target = json.load(response)
    pending, events, sequence = {}, [], 0
    async with websockets.connect(target['webSocketDebuggerUrl'], proxy=None) as socket:
        async def reader():
            async for raw in socket:
                message = json.loads(raw)
                if 'id' in message:
                    future = pending.pop(message['id'], None)
                    if future:
                        future.set_result(message)
                else:
                    events.append(message)

        task = asyncio.create_task(reader())

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await socket.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
            response = await asyncio.wait_for(future, 15)
            if 'error' in response:
                raise RuntimeError(response)
            return response.get('result', {})

        async def evaluate(script):
            result = await call('Runtime.evaluate', {'expression': script, 'returnByValue': True, 'awaitPromise': True, 'userGesture': True})
            if 'exceptionDetails' in result:
                raise RuntimeError(result['exceptionDetails'])
            return result.get('result', {}).get('value')

        async def until(expression, seconds=14):
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                try:
                    if await evaluate(expression):
                        return
                except RuntimeError:
                    pass
                await asyncio.sleep(.1)
            raise AssertionError(expression)

        async def scenario(settings):
            await evaluate("RKObserverDashboard.openPage('overview');window.__epoch=null;window.__fixedFrame=null;window.__requests=[];window.__samples=[];window.__scenario=" + json.dumps(settings) + ";RKObserverDashboard.openPage('media/cameras');window.__sampleTimer=setInterval(()=>__samples.push(__cameraState()),50)")
            await until('__cameraState().live&&!__cameraState().disabled')

        try:
            await call('Page.enable')
            await call('Runtime.enable')
            await call('Page.addScriptToEvaluateOnNewDocument', {'source': FIXTURE})
            await call('Page.navigate', {'url': server + '/login'})
            await until("!!document.getElementById('login-form')")
            await evaluate('document.getElementById("username").value=' + json.dumps(os.environ['CLOUD_TEST_USERNAME']))
            await evaluate('document.getElementById("password").value=' + json.dumps(os.environ['CLOUD_TEST_PASSWORD']))
            await evaluate("document.getElementById('login-form').requestSubmit()")
            await until('!!window.RKObserverDashboard&&!!window.__fixtureSnapshot')
            marker = envelope('media')
            marker.update(robot_id='ISOLATED_BROWSER_FIXTURE', source_id='TEST_ONLY')
            marker['payload'] = {'key': 'usbLeft', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': .05,
                                 'frameSeq': 7, 'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg'}
            request = urllib.request.Request(server + '/api/v1/ingest', data=json.dumps(marker).encode(),
                                             headers={'Content-Type': 'application/json',
                                                      'Authorization': 'Bearer ISOLATED-UNUSED-DEVICE-TOKEN-' + 'x' * 32})
            with opener.open(request, timeout=5) as reply:
                assert reply.status == 200
            native304 = await evaluate("""(async()=>{
              const first=await fetch('/camera/usbLeft.jpg',{cache:'no-store'}),tag=first.headers.get('ETag');
              const firstBytes=(await first.arrayBuffer()).byteLength,age1=Number(first.headers.get('X-Frame-Age-S'));
              await new Promise(resolve=>setTimeout(resolve,100));
              const second=await fetch('/camera/usbLeft.jpg',{cache:'no-store',headers:{'If-None-Match':tag}});
              return {firstStatus:first.status,firstBytes,secondStatus:second.status,
                secondBytes:(await second.arrayBuffer()).byteLength,sameTag:second.headers.get('ETag')===tag,
                sameIdentity:first.headers.get('X-Frame-Sequence')===second.headers.get('X-Frame-Sequence'),
                growingAge:Number(second.headers.get('X-Frame-Age-S'))>age1,ttl:second.headers.get('X-Frame-TTL-S'),
                cacheControl:second.headers.get('Cache-Control')};})()""")
            assert native304['firstStatus'] == 200 and native304['firstBytes'] > 0
            assert native304['secondStatus'] == 304 and native304['secondBytes'] == 0
            assert native304['sameTag'] and native304['sameIdentity'] and native304['growingAge']
            assert native304['ttl'] == '2' and native304['cacheControl'] == 'no-store'
            results = []
            for settings in [
                {'name': 'baseline-1fps-no-validator', 'cadenceMs': 1000, 'baseAge': .85, 'phaseMs': 400, 'delayMs': 300, 'noTag': True},
                {'name': 'conditional-2fps-rtt300', 'cadenceMs': 500, 'baseAge': .85, 'phaseMs': 400, 'delayMs': 300},
                {'name': 'conditional-2fps-rtt600', 'cadenceMs': 500, 'baseAge': .85, 'phaseMs': 400, 'delayMs': 600},
                {'name': 'conditional-2fps-jitter', 'cadenceMs': 500, 'baseAge': .7, 'phaseMs': 400, 'delayMs': 0, 'jitterMs': [120, 250, 500, 320]},
                {'name': 'conditional-2fps-204', 'cadenceMs': 500, 'baseAge': .55, 'phaseMs': 100, 'delayMs': 150, 'dropEvery': 7},
            ]:
                await scenario(settings)
                await evaluate("document.querySelector('[data-record=usbFront]').click()")
                await asyncio.sleep(5)
                raw = await evaluate("clearInterval(__sampleTimer);({requests:__requests,samples:__samples,final:__cameraState()})")
                samples = [sample for sample in raw['samples'] if sample['now'] >= raw['requests'][0]['started'] + 1500]
                result = {'scenario': settings, 'sampleCount': len(samples),
                          'liveFraction': sum(sample['live'] for sample in samples) / len(samples),
                          'retainedFraction': sum(sample['retained'] for sample in samples) / len(samples),
                          'hiddenFraction': sum(sample['hidden'] for sample in samples) / len(samples),
                          'http200': sum(request['status'] == 200 for request in raw['requests']),
                          'http304': sum(request['status'] == 304 for request in raw['requests']),
                          'http204': sum(request['status'] == 204 for request in raw['requests']),
                          'downloadBytes': sum(request['bytes'] for request in raw['requests']),
                          'recordingContinues': raw['final']['recording'],
                          'frameIdentityAdvance': len({request['seq'] for request in raw['requests']}),
                          'raw': raw}
                results.append(result)
                print(json.dumps({key: value for key, value in result.items() if key != 'raw'}), flush=True)
            assert results[1]['liveFraction'] > results[0]['liveFraction'] + .35, results[:2]
            assert results[1]['recordingContinues'], results[1]
            assert results[3]['liveFraction'] > .8, results[3]
            assert results[4]['http304'] > 0 and results[4]['http204'] > 0 and not results[4]['recordingContinues']

            await evaluate("window.__conditionKeys=['usbFront','usbLeft','usbHub2','usbHub3']")
            await scenario({'name': 'four-desktop-rtt300', 'cadenceMs': 500, 'baseAge': .85, 'phaseMs': 400, 'delayMs': 300})
            await until('__conditionKeys.every(k=>__cameraState(k).live&&!__cameraState(k).disabled)')
            await evaluate("__conditionKeys.forEach(k=>document.querySelector('[data-record='+k+']').click());window.__fourSamples=[];window.__fourTimer=setInterval(()=>__fourSamples.push({now:performance.now(),states:__conditionKeys.map(k=>({key:k,...__cameraState(k)}))}),50)")
            await asyncio.sleep(6)
            four = await evaluate("clearInterval(__fourTimer);clearInterval(__sampleTimer);({samples:__fourSamples,requests:__requests,final:__conditionKeys.map(k=>({key:k,...__cameraState(k)}))})")
            four_summary = [{'key': key, 'liveFraction': sum(next(state for state in sample['states'] if state['key'] == key)['live'] for sample in four['samples']) / len(four['samples']),
                             'http200': sum(request['key'] == key and request['status'] == 200 for request in four['requests']),
                             'http304': sum(request['key'] == key and request['status'] == 304 for request in four['requests']),
                             'downloadBytes': sum(request['bytes'] for request in four['requests'] if request['key'] == key),
                             'recordingContinues': next(state for state in four['final'] if state['key'] == key)['recording']}
                            for key in ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3')]
            assert all(item['liveFraction'] > .95 and item['recordingContinues'] for item in four_summary), four_summary
            await evaluate("window.__conditionKeys=['usbFront']")

            # An empty unexpected 304 without a held validator must recover by
            # sending a subsequent unconditional GET, never a 304-only loop.
            await scenario({'name': 'bad304', 'cadenceMs': 500, 'baseAge': .55, 'phaseMs': 0, 'delayMs': 40})
            await evaluate("RKObserverDashboard.openPage('overview');window.__requests=[];window.__forceBad304=true;RKObserverDashboard.openPage('media/cameras')")
            await until('__requests.length>=2&&__cameraState().live')
            assert await evaluate('__requests[0].status===304&&!__requests[0].validator&&__requests[1].status===200&&!__requests[1].validator')

            # Fresh-looking repeated 304s may tighten, but must never extend,
            # an identity's original deadline or restart a stopped recorder.
            await scenario({'name': 'frozen304', 'cadenceMs': 500, 'baseAge': .04, 'phaseMs': 0, 'delayMs': 30, 'resetAge': True})
            await evaluate("window.__fixedFrame=frameAt(performance.now());document.querySelector('[data-record=usbFront]').click()")
            await until('__cameraState().retained&&!__cameraState().recording')
            await asyncio.sleep(.8)
            assert await evaluate('__cameraState().retained&&__cameraState().disabled&&__requests.some(r=>r.status===304)')
            await until('__cameraState().hidden&&!__cameraState().src')

            layouts = []
            await scenario({'name': 'layouts', 'cadenceMs': 500, 'baseAge': .1, 'phaseMs': 0, 'delayMs': 30})
            for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
                await call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': width < 500})
                assert await evaluate('document.documentElement.scrollWidth<=innerWidth')
                layouts.append({'width': width, 'height': height, 'noHorizontalOverflow': True})
            assert await evaluate('__wsCount===1&&__maxActiveWsCount===1')
            errors = [event for event in events if event.get('method') == 'Runtime.exceptionThrown']
            assert not errors, errors
            report = {'fixtureScope': 'ISOLATED_BROWSER_TAB_ONLY', 'productionChanged': False,
                      'ttlStillTwoSeconds': True, 'unexpected304UnconditionalRecovery': True,
                      'nativeBrowserBackend304': native304,
                      'fourDesktopRtt300': four_summary,
                      'repeated304NeverRenewsTTLOrRecording': True, 'singleWebSocket': True,
                      'consoleErrors': 0, 'layouts': layouts, 'results': results}
            (destination / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({**report, 'results': [{key: value for key, value in item.items() if key != 'raw'} for item in results]}, ensure_ascii=False), flush=True)
        finally:
            await call('Page.close')
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
