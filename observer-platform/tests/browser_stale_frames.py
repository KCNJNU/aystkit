"""Real Chromium regression for bounded last pixels, isolated tab fixtures only.

Requires tests.serve_browser on 19088 and a dedicated Chromium CDP on 19089.
No robot credentials, device access, uploads, or production state is used.
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


FIXTURE = FIXTURE_SCRIPT + r'''
  window.__httpCameraFixture=true;
  window.__frameMode='live';window.__frameSession='A';window.__frameCounter=100;
  window.__imageRequests=[];window.__revokedUrls=[];
  window.__httpSnapshotMetadata=key=>key==='usbFront'?{
    status:__frameMode==='error'?'ERROR':'AVAILABLE',
    frameIdentity:__frameSession+':'+__frameCounter,frameSeq:__frameCounter
  }:{status:'WAITING_UPSTREAM'};
  const nativeFetch=window.fetch.bind(window);
  const surface=document.createElement('canvas');surface.width=320;surface.height=180;
  const context=surface.getContext('2d');context.fillStyle='#315974';context.fillRect(0,0,320,180);
  context.fillStyle='#eff9ff';context.font='19px sans-serif';context.fillText('ISOLATED STALE TEST',35,95);
  const jpeg=Uint8Array.from(atob(surface.toDataURL('image/jpeg').split(',')[1]),c=>c.charCodeAt(0));
  window.fetch=(path,options)=>{
    if(String(path)!=='/camera/usbFront.jpg')return nativeFetch(path,options);
    __imageRequests.push({time:performance.now(),mode:__frameMode});
    if(__frameMode==='204')return Promise.resolve(new Response(null,{status:204}));
    if(__frameMode==='error')return Promise.resolve(new Response(null,{status:400}));
    if(__frameMode==='live')__frameCounter++;
    const response=new Response(jpeg,{headers:{'Content-Type':'image/jpeg',
      'X-Frame-Sequence':__frameSession+':'+__frameCounter,'X-Frame-Age-S':'.04','X-Frame-TTL-S':'2'}});
    // Delay a request from the old session while a newer WS session arrives.
    if(__frameMode==='delayed'){
      window.__pendingOldIdentity=__frameSession+':'+__frameCounter;
      return new Promise(resolve=>setTimeout(()=>resolve(response),900));
    }
    return Promise.resolve(response);
  };
  window.__staleState=()=>{
    const image=document.getElementById('media-usbFront'),overlay=document.getElementById('media-empty-usbFront');
    return {hidden:image.hidden,src:image.getAttribute('src'),alt:image.alt,
      overlayHidden:overlay.hidden,retained:overlay.classList.contains('is-stale'),text:overlay.textContent,
      badge:document.getElementById('media-state-usbFront').innerText,
      disabled:document.querySelector('[data-record=usbFront]').disabled,
      recording:document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='true',
      saved:!document.querySelector('[data-download=usbFront]').hidden};
  };
'''


async def main():
    server = local_test_url('CLOUD_TEST_URL', 'http://127.0.0.1:19088')
    cdp = local_test_url('CLOUD_CDP_URL', 'http://127.0.0.1:19089')
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('tests/artifacts/stale-frames')
    destination.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(cdp + '/json/new?about:blank', method='PUT'), timeout=5) as reply:
        target = json.load(reply)
    pending, events, command_id = {}, [], 0
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
            result = await call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                     'awaitPromise': True, 'userGesture': True})
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
                    pass  # Successful login replaces the execution context.
                await asyncio.sleep(.1)
            raise AssertionError('Browser state not reached: ' + expression)

        async def fresh():
            await until("!__staleState().hidden&&__staleState().overlayHidden&&!__staleState().disabled")

        async def capture(name):
            shot = await call('Page.captureScreenshot', {'format': 'png'})
            (destination / name).write_bytes(base64.b64decode(shot['data']))

        try:
            for domain in ('Page', 'Runtime', 'Log', 'Network'):
                await call(domain + '.enable')
            await call('Page.addScriptToEvaluateOnNewDocument', {'source': FIXTURE})
            await call('Page.navigate', {'url': server + '/login'})
            await until("!!document.getElementById('login-form')")
            await evaluate('document.getElementById("username").value=' + json.dumps(os.environ['CLOUD_TEST_USERNAME']))
            await evaluate('document.getElementById("password").value=' + json.dumps(os.environ['CLOUD_TEST_PASSWORD']))
            await evaluate("document.getElementById('login-form').requestSubmit()")
            await until('!!window.RKObserverDashboard&&!!window.__fixtureSnapshot')
            await evaluate("RKObserverDashboard.openPage('media/cameras');window.__originalSocket=RKObserverDashboard.getConnection()")
            await fresh()
            assert await evaluate("['fisheye','usbRear','usbPort21'].every(k=>!document.getElementById('media-'+k)&&!document.querySelector('[data-record='+k+']'))")

            # Expired duplicated identity: stop recording at the true deadline,
            # hold only visibly marked last pixels, then revoke them by ten sec.
            await evaluate("document.querySelector('[data-record=usbFront]').click();window.__frameMode='freeze'")
            await until('__staleState().retained&&!__staleState().recording&&__staleState().saved')
            original = await evaluate('__staleState()')
            assert original['disabled'] and original['badge'] == '数据过期'
            assert original['text'] == '已过期 · 最后一帧（非实时）' and '非实时' in original['alt']
            layouts = []
            for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
                await call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height,
                                                                  'deviceScaleFactor': 1, 'mobile': width < 500})
                await evaluate("document.getElementById('media-usbFront').scrollIntoView({block:'center'})")
                measurement = await evaluate("(()=>{const e=document.getElementById('media-empty-usbFront');return {fits:e.scrollWidth<=e.clientWidth+1,font:parseFloat(getComputedStyle(e).fontSize),horizontalOverflow:document.documentElement.scrollWidth>innerWidth};})()")
                assert measurement['fits'] and measurement['font'] >= 14 and not measurement['horizontalOverflow']
                layouts.append({'width': width, 'height': height, **measurement})
                await capture(f'{width}-retained.png')
            await asyncio.sleep(1)
            assert (await evaluate('__staleState()'))['src'] == original['src']
            await until('__staleState().hidden&&!__staleState().src')
            assert await evaluate('__revokedUrls.includes(' + json.dumps(original['src']) + ')')
            await evaluate("window.__frameMode='live'")
            await fresh()

            # 204 may precede an AVAILABLE WS status. Metadata alone must not
            # unmark or permit recording of retained pixels.
            await evaluate("document.querySelector('[data-record=usbFront]').click();window.__frameMode='204'")
            await until('__staleState().retained&&!__staleState().recording')
            await asyncio.sleep(1.1)
            assert await evaluate('__staleState().disabled&&__staleState().badge==="数据过期"')
            await evaluate("window.__frameMode='live'")
            await fresh()

            # Explicit source error immediately removes the displayed frame.
            await evaluate("window.__frameMode='error'")
            await until('__staleState().hidden&&!__staleState().src&&!__staleState().retained')
            await evaluate("window.__frameMode='live'")
            await fresh()

            # A session switch clears old retained pixels before a new GET.
            await evaluate("window.__frameMode='204'")
            await until('__staleState().retained')
            await evaluate("window.__frameSession='B';window.__frameCounter=10")
            await until('__staleState().hidden&&!__staleState().src')
            await evaluate("window.__frameMode='live'")
            await fresh()

            # An in-flight old-session GET must not undo a newer WS session.
            await evaluate("window.__frameMode='delayed';window.__pendingOldIdentity=null")
            await until('!!window.__pendingOldIdentity')
            await evaluate("window.__frameSession='C';window.__frameCounter=50;window.__frameMode='204'")
            await until('__staleState().hidden&&!__staleState().src')
            await asyncio.sleep(1.1)
            assert await evaluate('__staleState().hidden&&!__staleState().src')
            await evaluate("window.__frameMode='live'")
            await fresh()
            await evaluate("window.__frameMode='204'")
            await until('__staleState().retained')
            await evaluate("RKObserverDashboard.openPage('media/scans')")
            assert await evaluate('__staleState().hidden&&!__staleState().src&&!__staleState().retained')
            await evaluate('window.__imageRequests=[]')
            await asyncio.sleep(1.2)
            assert await evaluate('__imageRequests') == []
            await evaluate("window.__frameMode='live';RKObserverDashboard.openPage('media/cameras')")
            await fresh()
            assert await evaluate('__wsCount===1&&__maxActiveWsCount===1&&RKObserverDashboard.getConnection()===__originalSocket')
            errors = [event for event in events if event.get('method') == 'Runtime.exceptionThrown'
                      or (event.get('method') == 'Log.entryAdded' and event['params']['entry']['level'] == 'error')
                      or (event.get('method') == 'Runtime.consoleAPICalled' and event['params']['type'] == 'error')]
            assert not errors, errors
            report = {'fixtureScope': 'ISOLATED_BROWSER_TAB_ONLY', 'realRobotDataVerified': False,
                      'duplicateIdentityDoesNotRenewTTL': True, 'lastPixelsRevokedWithinTenSeconds': True,
                      'retainedPixelsExplicitlyNonLive': True, 'expirationStopsRecording': True,
                      'availableMetadataDoesNotUnmark204': True, 'newFrameRestoresLive': True,
                      'explicitErrorClearsFrame': True, 'sourceSessionSwitchClearsFrame': True,
                      'lateOldSessionGetIgnored': True, 'hiddenCategoryClearsAndStopsPolling': True,
                      'singleWebSocket': True, 'consoleErrors': 0, 'layouts': layouts}
            (destination / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            await call('Page.close')
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
