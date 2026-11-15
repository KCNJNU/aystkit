"""Focused browser QA for current Hub cards and removed historical entries.

Use tests.serve_browser on 19088 and a dedicated Chromium CDP port 19089.
Fixtures live only in the test tab. They never upload or touch robot devices.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request

import websockets

from tests.browser_cloud import local_test_url


FIXTURE = r'''
  window.__hubKeys=['usbHub2','usbHub3'];window.__hubFrames={};window.__hubFreeze=false;
  window.__hubRequests=[];window.__removedCameraRequests=[];window.__wsCount=0;window.__activeWsCount=0;window.__maxActiveWsCount=0;
  const NativeWebSocket=WebSocket;
  window.WebSocket=class extends NativeWebSocket{
    constructor(...args){super(...args);__wsCount++;__activeWsCount++;
      __maxActiveWsCount=Math.max(__maxActiveWsCount,__activeWsCount);
      super.addEventListener('close',()=>__activeWsCount--);}
    addEventListener(name,callback,options){
      if(name!=='message')return super.addEventListener(name,callback,options);
      return super.addEventListener(name,event=>{
        let snapshot;try{snapshot=JSON.parse(event.data);}catch(_){return callback(event);}
        if(!snapshot.meta)return callback(event);
        snapshot.devices=snapshot.devices||{};
        snapshot.devices.cameras=snapshot.devices.cameras||{};
        __hubKeys.forEach((key,index)=>{
          const frame=__hubFrames[key];
          snapshot.devices.cameras[key]={label:['右视相机','左视相机'][index],
            status:'AVAILABLE',source:'ISOLATED_HUB_TEST_ONLY',topic:'/http_camera/test_hub_'+index,
            note:'隔离浏览器测试，不是设备数据',width:320,height:180,previewHz:1,
            ageS:frame?(performance.now()-frame.started)/1000:0,
            ...(frame?{frameIdentity:'hub-fixture:'+frame.seq,frameSeq:frame.seq}:{})};
        });
        window.__hubSnapshot=snapshot;
        callback({data:JSON.stringify(snapshot)});
      },options);
    }
  };
  const originalFetch=window.fetch.bind(window);
  const canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
  const context=canvas.getContext('2d');context.fillStyle='#174654';context.fillRect(0,0,320,180);
  context.fillStyle='#f4c679';context.fillRect(24,28,272,124);
  context.fillStyle='#10252e';context.font='20px sans-serif';context.fillText('ISOLATED HUB TEST',45,100);
  const raw=atob(canvas.toDataURL('image/jpeg',.85).split(',')[1]);
  const bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
  window.fetch=(path,options)=>{
    if(/^\/camera\//.test(String(path)))__hubRequests.push({path:String(path),time:performance.now()});
    if(/^\/camera\/(fisheye|usbRear|usbPort21)\.jpg$/.test(String(path)))__removedCameraRequests.push(String(path));
    const match=String(path).match(/^\/camera\/(usbHub[23])\.jpg$/);
    if(!match)return originalFetch(path,options);
    const key=match[1],old=__hubFrames[key];
    const frame=old&&__hubFreeze?old:{seq:(old?.seq||100)+1,started:performance.now()};
    __hubFrames[key]=frame;
    return Promise.resolve(new Response(bytes,{headers:{'Content-Type':'image/jpeg',
      'X-Frame-Sequence':'hub-fixture:'+frame.seq,'X-Frame-Age-S':'0.04','X-Frame-TTL-S':'2'}}));
  };
'''


async def main():
    server = local_test_url('CLOUD_TEST_URL', 'http://127.0.0.1:19088')
    cdp = local_test_url('CLOUD_CDP_URL', 'http://127.0.0.1:19089')
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('tests/artifacts/hub-cameras')
    destination.mkdir(parents=True, exist_ok=True)
    downloads = destination / 'downloads'
    downloads.mkdir(exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(cdp + '/json/new?about:blank', method='PUT'), timeout=5) as reply:
        target = json.load(reply)
    events, pending, command_id = [], {}, 0
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

        reader = asyncio.create_task(receive())

        async def call(method, params=None):
            nonlocal command_id
            command_id += 1
            future = asyncio.get_running_loop().create_future()
            pending[command_id] = future
            await socket.send(json.dumps({'id': command_id, 'method': method, 'params': params or {}}))
            value = await asyncio.wait_for(future, 15)
            if 'error' in value:
                raise RuntimeError(value['error'])
            return value.get('result', {})

        async def evaluate(expression):
            value = await call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                    'awaitPromise': True, 'userGesture': True})
            if 'exceptionDetails' in value:
                raise RuntimeError(value['exceptionDetails'])
            return value.get('result', {}).get('value')

        async def until(expression):
            for _ in range(80):
                try:
                    if await evaluate(expression):
                        return
                except RuntimeError:
                    pass  # Login navigation replaces the execution context.
                await asyncio.sleep(.15)
            raise AssertionError('Browser state did not become ready: ' + expression)

        try:
            for domain in ('Page', 'Runtime', 'Log', 'Network'):
                await call(domain + '.enable')
            await call('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(downloads.resolve()),
                                                       'eventsEnabled': True})
            await call('Page.addScriptToEvaluateOnNewDocument', {'source': FIXTURE})
            await call('Page.navigate', {'url': server + '/login'})
            await until("!!document.getElementById('login-form')")
            await evaluate('document.getElementById("username").value=' + json.dumps(os.environ['CLOUD_TEST_USERNAME']))
            await evaluate('document.getElementById("password").value=' + json.dumps(os.environ['CLOUD_TEST_PASSWORD']))
            await evaluate("document.getElementById('login-form').requestSubmit()")
            await until("!!window.RKObserverDashboard&&!!window.__hubSnapshot")
            await evaluate("RKObserverDashboard.openPage('media/cameras');window.__originalSocket=RKObserverDashboard.getConnection()")
            ready = "__hubKeys.every(k=>{const e=document.getElementById('media-'+k);return !e.hidden&&e.complete&&e.naturalWidth===320&&e.naturalHeight===180&&document.getElementById('media-empty-'+k).hidden&&!document.querySelector('[data-record='+k+']').disabled;})"
            await until(ready)
            assert await evaluate("document.querySelectorAll('#camera-gallery .media-card').length") == 6
            assert await evaluate("document.querySelectorAll('[data-record]').length") == 14
            assert await evaluate("['fisheye','usbRear','usbPort21'].every(k=>!document.getElementById('media-'+k)&&!document.querySelector('[data-record='+k+']'))")
            assert await evaluate("__removedCameraRequests") == []
            assert await evaluate("__hubSnapshot.ros2.status!=='AVAILABLE'")
            for key, label in (('usbHub2', '右视相机'), ('usbHub3', '左视相机')):
                assert await evaluate(f"document.getElementById('media-{key}').closest('.media-card').querySelector('h2').textContent") == label
                assert await evaluate(f"document.getElementById('media-meta-{key}').innerText.includes('来源 Jetson 边缘计算机 · 网页预览')")

            layouts = []
            for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
                await call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height,
                                                                  'deviceScaleFactor': 1, 'mobile': width < 500})
                await evaluate("document.getElementById('media-usbHub2').closest('.media-card').scrollIntoView({block:'center'})")
                await asyncio.sleep(.25)
                assert await evaluate('document.documentElement.scrollWidth<=innerWidth')
                measurements = await evaluate("__hubKeys.map(k=>{const e=document.getElementById('media-'+k).closest('.media-card'),h=e.querySelector('h2');return {key:k,width:e.clientWidth,headingFits:h.scrollWidth<=h.clientWidth+1,titleSize:parseFloat(getComputedStyle(h).fontSize)};})")
                assert all(row['headingFits'] and row['titleSize'] >= 14 for row in measurements), measurements
                capture = await call('Page.captureScreenshot', {'format': 'png'})
                (destination / f'{width}-hub-cameras.png').write_bytes(base64.b64decode(capture['data']))
                layouts.append({'width': width, 'height': height, 'cards': measurements})

            await evaluate('window.__hubRequests=[]')
            await asyncio.sleep(3.2)
            requests = await evaluate('__hubRequests')
            for key in ('usbHub2', 'usbHub3'):
                times = [row['time'] for row in requests if row['path'] == f'/camera/{key}.jpg']
                assert 2 <= len(times) <= 4
                assert all(900 <= right-left < 1500 for left, right in zip(times, times[1:])), (key, times)

            await evaluate("__hubKeys.forEach(k=>document.querySelector('[data-record='+k+']').click())")
            assert await evaluate("__hubKeys.every(k=>document.querySelector('[data-record='+k+']').getAttribute('aria-pressed')==='true')")
            await asyncio.sleep(1.6)
            await evaluate("__hubKeys.forEach(k=>document.querySelector('[data-record='+k+']').click())")
            await until("__hubKeys.every(k=>!document.querySelector('[data-download='+k+']').hidden)")
            saved_files = []
            for key in ('usbHub2', 'usbHub3'):
                previous = set(downloads.iterdir())
                await evaluate(f"document.querySelector('[data-download={key}]').click()")
                for _ in range(50):
                    clips = [path for path in set(downloads.iterdir())-previous
                             if path.suffix in ('.webm', '.mp4') and path.stat().st_size > 32]
                    if clips:
                        saved_files.append({'key': key, 'file': clips[0].name, 'bytes': clips[0].stat().st_size})
                        break
                    await asyncio.sleep(.15)
                else:
                    raise AssertionError('Native recording download failed: ' + key)

            # Keep sending the same identity with a falsely reset Age: the
            # original two-second deadline still stops recording immediately.
            # Only marked, non-recordable last pixels remain for ten seconds.
            await until("__hubKeys.every(k=>performance.now()-__hubFrames[k].started<850)")
            await evaluate("window.__hubFreeze=true;__hubKeys.forEach(k=>document.querySelector('[data-record='+k+']').click())")
            await until("__hubKeys.every(k=>!document.getElementById('media-'+k).hidden&&document.getElementById('media-empty-'+k).classList.contains('is-stale')&&!document.querySelector('[data-download='+k+']').hidden&&document.querySelector('[data-record='+k+']').getAttribute('aria-pressed')==='false')")
            await asyncio.sleep(1.1)
            assert await evaluate("__hubKeys.every(k=>!document.getElementById('media-'+k).hidden&&document.getElementById('media-empty-'+k).textContent.includes('非实时')&&document.querySelector('[data-record='+k+']').disabled&&document.getElementById('record-status-'+k).textContent.includes('自动停止'))")
            await until("__hubKeys.every(k=>document.getElementById('media-'+k).hidden&&!document.getElementById('media-'+k).hasAttribute('src'))")
            await evaluate('window.__hubFreeze=false')
            await until(ready)
            await evaluate("document.querySelector('[data-record=usbHub2]').click()")
            await asyncio.sleep(.35)
            await evaluate("RKObserverDashboard.openPage('media/scans')")
            await until("document.querySelector('[data-record=usbHub2]').getAttribute('aria-pressed')==='false'&&!document.querySelector('[data-download=usbHub2]').hidden")
            await asyncio.sleep(.3)
            await evaluate('window.__hubRequests=[]')
            await asyncio.sleep(1.2)
            assert await evaluate('__hubRequests') == []
            for route in ('overview', 'ros', 'media/cameras'):
                await evaluate(f'RKObserverDashboard.openPage({json.dumps(route)})')
            await until(ready)
            assert await evaluate('__wsCount===1&&__maxActiveWsCount===1&&RKObserverDashboard.getConnection()===__originalSocket')
            assert await evaluate("['fisheye','usbRear','usbPort21'].every(k=>!document.getElementById('media-'+k)&&!document.querySelector('[data-record='+k+']'))")
            assert await evaluate("__removedCameraRequests") == []
            console_errors = [event for event in events if event.get('method') == 'Runtime.exceptionThrown'
                              or (event.get('method') == 'Log.entryAdded' and event['params']['entry']['level'] == 'error')
                              or (event.get('method') == 'Runtime.consoleAPICalled' and event['params']['type'] == 'error')]
            http_errors = [event['params']['response']['status'] for event in events
                           if event.get('method') == 'Network.responseReceived' and event['params']['response']['status'] >= 400]
            assert not console_errors, console_errors
            assert not http_errors, http_errors
            report = {'fixtureScope': 'ISOLATED_BROWSER_TAB_ONLY', 'realRobotDataVerified': False,
                      'layouts': layouts, 'cameraCards': 6, 'allMediaCards': 14,
                      'jpegDecoded': True, 'nativeRecordingDownloads': saved_files,
                      'originalDeadlineExpiresAndStopsRecording': True, 'markedLastFrameIsBounded': True,
                      'hiddenCategoryStopsPolling': True,
                      'pageSwitchSingleWebSocket': True, 'historicalCameraCardsAbsent': True,
                      'historicalCameraRequests': 0,
                      'httpPreviewDoesNotClaimRosHealthy': True,
                      'consoleErrors': len(console_errors), 'httpErrors': http_errors}
            (destination / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
