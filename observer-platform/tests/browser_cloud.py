"""Native Chromium cloud regression against an explicitly isolated backend/browser.

Start dedicated Chromium with --remote-debugging-port=19089 and a temporary
profile. Set CLOUD_TEST_URL, CLOUD_CDP_URL, CLOUD_TEST_USERNAME and
CLOUD_TEST_PASSWORD. Fixtures alter only incoming messages in this test tab;
they are never uploaded, persisted, published to ROS or used in production.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
from urllib.parse import urlsplit

import websockets

FIXTURE_SCRIPT = r'''
  window.__wsCount=0; window.__activeWsCount=0; window.__maxActiveWsCount=0; window.__fixtureOffline=false;
  window.__removedCameraRequests=[];
  const NativeCameraFetch=window.fetch.bind(window);
  window.fetch=(path,options)=>{
    if(/^\/camera\/(fisheye|usbRear|usbPort21)\.jpg$/.test(String(path)))__removedCameraRequests.push(String(path));
    return NativeCameraFetch(path,options);
  };
  window.__isVisible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
  window.__revokedUrls=[]; const revoke=URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL=url=>{__revokedUrls.push(url);revoke(url);};
  const NativeWebSocket=WebSocket;
  window.WebSocket=class extends NativeWebSocket {
    constructor(...args){super(...args);window.__wsCount++;window.__activeWsCount++;
      window.__maxActiveWsCount=Math.max(__maxActiveWsCount,__activeWsCount);
      super.addEventListener('close',()=>window.__activeWsCount--);}
    addEventListener(name,callback,options){
      if(name!=='message')return super.addEventListener(name,callback,options);
      return super.addEventListener(name,event=>{
        let s;try{s=JSON.parse(event.data);}catch(_){return callback(event);}
        if(!s.meta)return callback(event);
        const status=window.__fixtureOffline?'STALE':'AVAILABLE',t=s.meta.timestamp;
        s.cloud={robotId:'ISOLATED_BROWSER_FIXTURE',sourceId:'TEST_ONLY',online:!window.__fixtureOffline,
          status,lastSeen:t-1,lastObservation:t-1,ageS:1};
        s.rk3588={status,uptimeS:0,cpu:{status,value:0},ram:{usage:{status,value:0},usedBytes:0,totalBytes:0},
          primaryThermal:{status:'UNAVAILABLE',temperatureC:null},thermalZones:[],network:[],
          npu:{status:'UNAVAILABLE',modelName:null,inferenceFps:null,inferenceLatencyMs:null}};
        s.robot={status,source:'ISOLATED_BROWSER_FIXTURE /chassis_host/telemetry',
          telemetry:{status,ageS:1,hz:0,publisherCount:1,pages:{},parseErrorCount:0},
          data:{chassisAlive:false,debug:{fields:{busVoltage:{value:0,state:status,type:'ACTUAL',ageMs:1000,
            unit:'V',source:'ISOLATED_BROWSER_FIXTURE'}}}}};
        s.devices={visionDocking:{},sensors:{},cameras:{
          ...Object.fromEntries(['usbFront','usbLeft','usbHub2','usbHub3'].map((key,i)=>[key,{
            label:['前视相机','后视相机','右视相机','左视相机'][i],
            status:window.__httpCameraFixture?(window.__httpCameraState||'AVAILABLE'):'WAITING_UPSTREAM',
            source:'TEST_ONLY_HTTP_UPLOAD',topic:'/http_camera/camera'+[2,1,4,3][i],
            note:'Jetson HTTP 预览（隔离测试）',width:320,height:180,previewHz:1,ageS:0,
            ...(window.__httpSnapshotMetadata?window.__httpSnapshotMetadata(key):{})
          }])),
          fisheye:{label:'鱼眼相机',status:'WAITING_UPSTREAM',source:'TEST_ONLY',hz:0},
          color:{label:'深度相机彩色',status:'WAITING_UPSTREAM',source:'TEST_ONLY',previewHz:0,hz:10},
          depth:{label:'深度相机深度',status:'WAITING_UPSTREAM',source:'TEST_ONLY',previewHz:null,hz:null}}};
        s.ros2={status:window.__httpCameraFixture?'WAITING_UPSTREAM':status,
          domainId:'0',rmwImplementation:'TEST_ONLY',topics:{},deployment:'CLOUD_TEST'};
        s.pages={};s.errors={};
        window.__fixtureSnapshot=s;
        document.title='ISOLATED CLOUD BROWSER TEST';
        callback({data:JSON.stringify(s)});
      },options);
    }
  };
'''

SUBPAGES = {
    "system": ("performance", "environment"),
    "robot": ("motion", "actuators", "sensors", "docking"),
    "media": ("cameras", "scans", "clouds", "maps"),
}
ROUTES = ("overview", "system/performance", "system/environment", "robot/motion", "robot/actuators",
          "robot/sensors", "robot/docking", "stm32", "ros", "npu", "media/cameras", "media/scans",
          "media/clouds", "media/maps")
MEDIA_KEYS = {"cameras": ("usbFront", "usbLeft", "usbHub2", "usbHub3", "color", "depth"), "scans": ("bluesea", "scan"),
              "clouds": ("lidar", "registered", "depthPoints"), "maps": ("map", "localCostmap", "globalCostmap")}


def local_test_url(name, default):
    value = os.environ.get(name, default).rstrip("/")
    parsed = urlsplit(value)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.port in (8888, 8765, 9234):
        raise RuntimeError(f"{name} must identify an isolated loopback test instance, not production")
    return value


async def main():
    server = local_test_url("CLOUD_TEST_URL", "http://127.0.0.1:19088")
    cdp = local_test_url("CLOUD_CDP_URL", "http://127.0.0.1:19089")
    username = os.environ["CLOUD_TEST_USERNAME"]
    password = os.environ["CLOUD_TEST_PASSWORD"]
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "artifacts" / "browser"
    destination.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(cdp + "/json/new?about:blank", method="PUT"), timeout=5) as response:
        target = json.load(response)
    events, pending, sequence, accept_dialog = [], {}, 0, True
    async with websockets.connect(target["webSocketDebuggerUrl"], proxy=None, max_size=30_000_000) as ws:
        async def receive():
            nonlocal sequence
            try:
                async for raw in ws:
                    value = json.loads(raw)
                    if "id" in value:
                        future = pending.pop(value["id"], None)
                        if future and not future.done():
                            future.set_result(value)
                    else:
                        events.append(value)
                        if value.get("method") == "Page.javascriptDialogOpening":
                            # Only this isolated test tab is controlled here.
                            sequence += 1
                            await ws.send(json.dumps({"id": sequence, "method": "Page.handleJavaScriptDialog",
                                                       "params": {"accept": accept_dialog}}))
            except websockets.ConnectionClosed:
                return

        reader = asyncio.create_task(receive())

        async def call(method, params=None):
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            value = await asyncio.wait_for(future, 15)
            if "error" in value:
                raise RuntimeError(f"CDP {method} failed: {value['error']}")
            return value.get("result", {})

        async def evaluate(expression):
            value = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                      "awaitPromise": True, "userGesture": True})
            if "exceptionDetails" in value:
                details = value["exceptionDetails"]
                raise RuntimeError(details.get("exception", {}).get("description", details.get("text", "Browser evaluation failed")))
            return value.get("result", {}).get("value")

        async def until(expression):
            for _ in range(80):
                try:
                    if await evaluate(expression):
                        return
                except RuntimeError:
                    pass  # Navigation replaces the execution context.
                await asyncio.sleep(.2)
            raise AssertionError("Browser state did not become ready: " + expression)

        for domain in ("Page", "Runtime", "Log", "Network"):
            await call(domain + ".enable")
        downloads = destination / "downloads"
        downloads.mkdir(exist_ok=True)
        await call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads.resolve()),
                                                   "eventsEnabled": True})
        # Close only prior tabs belonging to this dedicated loopback test server.
        # A failed earlier run must not share its session with the new test tab.
        for prior in (await call("Target.getTargets"))["targetInfos"]:
            if prior["type"] == "page" and prior["targetId"] != target["id"] and prior["url"].startswith(server + "/"):
                await call("Target.closeTarget", {"targetId": prior["targetId"]})
        await call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844,
                                                          "deviceScaleFactor": 1, "mobile": True})
        await call("Page.addScriptToEvaluateOnNewDocument", {"source": FIXTURE_SCRIPT})
        await call("Page.navigate", {"url": server + "/login"})
        await until("!!document.getElementById('login-form')")
        assert await evaluate("document.documentElement.scrollWidth<=390")
        capture = await call("Page.captureScreenshot", {"format": "png"})
        (destination / "390-login.png").write_bytes(base64.b64decode(capture["data"]))
        await evaluate("document.getElementById('username').value=" + json.dumps(username))
        await evaluate("document.getElementById('password').value=" + json.dumps(password))
        await evaluate("document.getElementById('login-form').requestSubmit()")
        await until("!!window.RKObserverDashboard && RKObserverDashboard.getHistoryCount('cpu')>=2")
        # A real document load at a leaf hash must restore the requested child.
        await call("Page.navigate", {"url": server + "/#robot/sensors"})
        await until("!!window.RKObserverDashboard && RKObserverDashboard.getRoute()==='robot/sensors'")
        await evaluate("window.__beforeDeepReload=true")
        await call("Page.reload", {"ignoreCache": True})
        await until("!window.__beforeDeepReload && !!window.RKObserverDashboard && RKObserverDashboard.getRoute()==='robot/sensors' && RKObserverDashboard.getHistoryCount('cpu')>=2")
        await evaluate("window.__firstSocket=RKObserverDashboard.getConnection()")
        assert await evaluate("window.__wsCount") == 1
        assert await evaluate("ObserverFormat.formatValue(0)") == "0"
        assert await evaluate("ObserverFormat.formatValue(null)") == "—"
        assert await evaluate("ObserverFormat.formatValue(NaN)") == "—"
        assert await evaluate("ObserverFormat.formatValue(Infinity)") == "—"
        await evaluate("RKObserverDashboard.openPage('media/cameras')")
        await until("document.getElementById('media-meta-color').textContent.includes('实收 0 帧/秒')")
        assert await evaluate("['fisheye','usbRear','usbPort21'].every(k=>!document.getElementById('media-'+k)&&!document.querySelector('[data-record='+k+']'))")
        assert await evaluate("document.getElementById('media-meta-color').textContent.includes('实收 0 帧/秒')")
        assert await evaluate("document.getElementById('media-meta-depth').textContent.includes('实收 — 帧/秒')")
        assert await evaluate("document.getElementById('cloud-source-note').hidden&&!__isVisible(document.getElementById('cloud-source-note'))")
        initial_history = await evaluate("RKObserverDashboard.getHistoryCount('cpu')")
        results, copy_results, navigation_results = [], [], []
        # Invalid input cannot select a hidden arbitrary DOM element or strand navigation.
        await evaluate("location.hash='#not-a-real-page/unknown'")
        await until("RKObserverDashboard.getRoute()==='overview'")
        await evaluate("RKObserverDashboard.openPage('robot/not-a-child')")
        assert await evaluate("RKObserverDashboard.getRoute()") in ("overview", "robot/motion", "robot/sensors")
        await evaluate("RKObserverDashboard.openPage('robot/actuators');RKObserverDashboard.openPage('media/scans')")
        assert await evaluate("location.hash") == "#media/scans"
        await evaluate("history.back()")
        await until("RKObserverDashboard.getRoute()==='robot/actuators'")
        await evaluate("history.forward()")
        await until("RKObserverDashboard.getRoute()==='media/scans'")
        await evaluate("RKObserverDashboard.openPage('overview');RKObserverDashboard.openPage('media')")
        assert await evaluate("RKObserverDashboard.getRoute()") == "media/scans"
        assert await evaluate("__wsCount===1&&RKObserverDashboard.getConnection()===__firstSocket")
        for width, height, route in ((1920, 1080, "robot/motion"), (3440, 1440, "media/scans"), (390, 844, "robot/sensors")):
            await call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                              "deviceScaleFactor": 1, "mobile": width < 500})
            await evaluate(f"RKObserverDashboard.openPage({json.dumps(route)});window.scrollTo(0,0)")
            await asyncio.sleep(.3)
            await evaluate("document.getAnimations().filter(a=>a.effect?.target?.closest?.('.page')).forEach(a=>a.finish())")
            capture = await call("Page.captureScreenshot", {"format": "png"})
            (destination / f"preview-{width}-{route.replace('/', '-')}.png").write_bytes(base64.b64decode(capture["data"]))
        for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
            await call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                              "deviceScaleFactor": 1, "mobile": width < 500})
            await evaluate("document.querySelector('[data-view-mode=compact]').click()")
            for route in ROUTES:
                page, _, child = route.partition("/")
                await evaluate(f"RKObserverDashboard.openPage({json.dumps(route)});window.scrollTo(0,0)")
                await asyncio.sleep(.3)
                # Low-frame-rate headless sessions can still be mid-transition.
                # Finish presentation-only animations before capturing evidence.
                await evaluate("document.getAnimations().filter(a=>a.effect?.target?.closest?.('.page')).forEach(a=>a.finish())")
                result = await evaluate(r'''({page:document.querySelector('.page.active').dataset.pageName,
                  route:RKObserverDashboard.getRoute(),
                  width:document.documentElement.scrollWidth,viewport:innerWidth,
                  titleSize:parseFloat(getComputedStyle(document.querySelector('.page.active h1')).fontSize),
                  websocketCount:__wsCount,sameSocket:RKObserverDashboard.getConnection()===__firstSocket,
                  history:RKObserverDashboard.getHistoryCount('cpu')})''')
                assert result["width"] <= width, result
                assert result["route"] == route, result
                assert result["titleSize"] >= 24, result
                assert result["websocketCount"] == 1 and result["sameSocket"], result
                assert await evaluate("document.querySelectorAll('.page.active').length") == 1
                if child:
                    assert await evaluate("[...document.querySelectorAll('.page.active .subpage')].filter(__isVisible).map(e=>e.dataset.subpage)") == [child]
                    assert await evaluate("[...document.querySelectorAll('.page.active .subpage')].filter(e=>!__isVisible(e)).every(e=>e.hidden)")
                    assert await evaluate("[...document.querySelectorAll('.subnav-button[aria-current=page]')].map(e=>e.dataset.route)") == [route]
                    # Mobile picker is accessible and does not sit behind the fixed header.
                    if width < 500:
                        assert await evaluate(f"__isVisible(document.querySelector('[data-subpage-picker={page}]'))")
                        assert await evaluate(f"(()=>{{const e=document.querySelector('[data-subpage-picker={page}]'),r=e.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===e;}})()")
                assert await evaluate("[...document.querySelectorAll('.page.active .eyebrow,.page.active .page-description,.page.active .panel-head p')].every(e=>!__isVisible(e))")
                if page == "media":
                    assert await evaluate("document.querySelectorAll('[data-record]').length") == 14
                    assert await evaluate("[...document.querySelectorAll('[data-record]')].filter(__isVisible).map(e=>e.dataset.record)") == list(MEDIA_KEYS[child])
                    assert await evaluate("[...document.querySelectorAll('[data-record]')].every(b=>b.disabled)")
                    assert await evaluate("[...document.querySelectorAll('.media-overlay')].every(e=>!e.hidden)")
                    assert await evaluate("__isVisible(document.querySelector('.recording-notice'))&&document.querySelector('.recording-notice').innerText.includes('未下载的视频')")
                    assert await evaluate("!document.querySelector('.recording-help').open")
                    if child == "scans":
                        assert await evaluate("[...document.querySelectorAll('.media-legend')].some(e=>__isVisible(e)&&e.innerText.includes('不做 TF 融合'))")
                        await until("document.getElementById('media-meta-scan').classList.contains('detail-only')")
                        assert await evaluate("!__isVisible(document.getElementById('media-meta-scan'))")
                    elif child == "cameras":
                        assert await evaluate("__isVisible(document.getElementById('media-meta-color'))&&document.getElementById('media-meta-color').innerText.includes('来源 TEST_ONLY')")
                capture = await call("Page.captureScreenshot", {"format": "png"})
                (destination / f"{width}-{route.replace('/', '-')}.png").write_bytes(base64.b64decode(capture["data"]))
                results.append(result)
            for parent, children in SUBPAGES.items():
                route = f"{parent}/{children[-1]}"
                await evaluate(f"RKObserverDashboard.openPage({json.dumps(route)});window.scrollTo(0,0)")
                if width < 500:
                    await evaluate(f"document.querySelector('.nav[data-page={parent}]').click()")
                    assert await evaluate(f"document.activeElement===document.querySelector('[data-subpage-picker={parent}]')")
                    assert await evaluate(f"document.querySelector('.nav[data-page={parent}]').getAttribute('aria-expanded')") == "false"
                    assert await evaluate(f"document.getElementById('subnav-{parent}').hidden")
                    await evaluate(f"(()=>{{const p=document.querySelector('[data-subpage-picker={parent}]');p.value={json.dumps(children[0])};p.dispatchEvent(new Event('change',{{bubbles:true}}));}})()")
                    # Select values may be full routes, or leaf IDs; use the actual option if necessary.
                    if await evaluate("RKObserverDashboard.getRoute()") != f"{parent}/{children[0]}":
                        await evaluate(f"(()=>{{const p=document.querySelector('[data-subpage-picker={parent}]');p.selectedIndex=0;p.dispatchEvent(new Event('change',{{bubbles:true}}));}})()")
                    assert await evaluate("RKObserverDashboard.getRoute()") == f"{parent}/{children[0]}"
                    navigation_results.append({"width": width, "parent": parent, "mobilePicker": True})
                else:
                    await evaluate(f"document.querySelector('.nav[data-page={parent}]').focus()")
                    for expected in ("false", "true"):
                        await call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                                                               "text": "\r", "unmodifiedText": "\r"})
                        await call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
                        assert await evaluate(f"document.querySelector('.nav[data-page={parent}]').getAttribute('aria-expanded')") == expected
                        assert await evaluate(f"document.getElementById('subnav-{parent}').hidden") == (expected == "false")
                        assert await evaluate("RKObserverDashboard.getRoute()") == route
                    navigation_results.append({"width": width, "parent": parent, "keyboardCollapseExpand": True})
            for mode in ("compact", "detailed"):
                await evaluate(f"document.querySelector('[data-view-mode={mode}]').click()")
                assert await evaluate("RKObserverDashboard.getConnection()===__firstSocket")
                assert await evaluate("RKObserverDashboard.getHistoryCount('cpu')") >= initial_history
                for route in ("overview", "media/cameras", "media/scans", "media/clouds", "media/maps"):
                    page = route.split("/")[0]
                    await evaluate(f"RKObserverDashboard.openPage({json.dumps(route)});window.scrollTo(0,0)")
                    await asyncio.sleep(.2)
                    await evaluate("document.getAnimations().filter(a=>a.effect?.target?.closest?.('.page')).forEach(a=>a.finish())")
                    details = await evaluate("[...document.querySelectorAll('.cloud-detail')].every(__isVisible)")
                    assert details == (mode == "detailed"), (width, page, mode)
                    assert await evaluate("document.documentElement.scrollWidth<=innerWidth")
                    if page == "media":
                        assert await evaluate("[...document.querySelectorAll('.page.active .subpage:not([hidden]) .media-meta .detail-only')].every(e=>__isVisible(e)===(document.body.dataset.view==='detailed'))")
                    capture = await call("Page.captureScreenshot", {"format": "png"})
                    (destination / f"{width}-{route.replace('/', '-')}-{mode}.png").write_bytes(base64.b64decode(capture["data"]))
                    copy_results.append({"width": width, "route": route, "mode": mode,
                                         "engineeringFieldsVisible": details, "noHorizontalOverflow": True})
        await evaluate("document.querySelector('[data-view-mode=compact]').click()")
        await evaluate("window.__fixtureOffline=true;RKObserverDashboard.openPage('overview')")
        await until("document.getElementById('cloud-robot-status').textContent.includes('离线')")
        assert await evaluate("document.querySelector('#overview-kpis .mini-badge').textContent") == "数据过期"
        assert await evaluate("document.querySelector('#overview-kpis .kpi-value').textContent") == "0"
        assert await evaluate("document.getElementById('cloud-source-note').textContent.includes('最后一次真实观测')")
        assert await evaluate("__isVisible(document.getElementById('cloud-source-note'))")
        assert await evaluate("__wsCount") == 1
        await evaluate("window.__fixtureOffline=false")
        await until("document.getElementById('cloud-robot-status').textContent==='机器人在线'")
        assert await evaluate("!__isVisible(document.getElementById('cloud-source-note'))")
        # Exercise link/observation warnings without opening extra sockets.
        for expression in ("CloudObserver.update(__fixtureSnapshot,false)",
                           "CloudObserver.update({...__fixtureSnapshot,cloud:{...__fixtureSnapshot.cloud,status:'STALE'}},true)"):
            assert await evaluate(f"(()=>{{{expression};return __isVisible(document.getElementById('cloud-source-note'))&&!!document.getElementById('cloud-source-note').innerText;}})()")
        await evaluate("CloudObserver.update(__fixtureSnapshot,true)")
        assert await evaluate("RKObserverDashboard.getHistoryCount('cpu')") >= initial_history
        # Tab-only HTTP JPEG, cloud and 2-D scan fixtures exercise independent recorders.
        # Nothing is uploaded to the backend or robot.
        await evaluate(r'''(() => {
          window.__previewFixture=true; window.__scanError=false; window.__mediaRequests=0; window.__requestedMedia=[]; const original=window.fetch.bind(window);
          window.__httpCameraFixture=true; window.__httpCameraState='AVAILABLE';
          window.__httpCameraEmpty=false; window.__httpCameraRequests=[];
          window.__httpSession='isolated-jpeg'; window.__httpFrames={};
          window.__httpFreezeIdentity=false;window.__httpJitter=false;window.__httpSkewSnapshots=false;window.__httpSkewedSnapshots=0;
          window.__httpTransportFailure=null;window.__httpOmitFreshness=false;
          window.__cameraInvalidations=[];
          const invalidate=ObserverRecording.invalidate;
          ObserverRecording.invalidate=(key,reason)=>{
            if(/^usb/.test(key))__cameraInvalidations.push({key,reason,time:performance.now(),
              frame:__httpFrames[key],snapshot:__fixtureSnapshot.devices.cameras[key]});
            return invalidate(key,reason);
          };
          window.__httpSnapshotMetadata=key=>{
            const frame=__httpFrames[key];if(!frame)return {};
            const skew=__httpSkewSnapshots&&frame.sequence%4===2;
            if(skew)__httpSkewedSnapshots++;
            return {frameIdentity:__httpSession+':'+(frame.sequence-(skew?2:0)),
              sequence:frame.sequence-(skew?2:0),frameSeq:frame.sequence-(skew?2:0),
              ageS:skew?2.1:Math.max(0,(performance.now()-frame.observed)/1000),
              ...(skew?{status:'STALE'}:{})};
          };
          const jpegCanvas=document.createElement('canvas');jpegCanvas.width=320;jpegCanvas.height=180;
          const jpegContext=jpegCanvas.getContext('2d');
          const gradient=jpegContext.createLinearGradient(0,0,320,180);
          gradient.addColorStop(0,'#24677e');gradient.addColorStop(1,'#edb05b');
          jpegContext.fillStyle=gradient;jpegContext.fillRect(0,0,320,180);
          jpegContext.fillStyle='white';jpegContext.font='20px sans-serif';
          jpegContext.fillText('ISOLATED JPEG TEST',32,90);
          const jpegRaw=atob(jpegCanvas.toDataURL('image/jpeg',0.8).split(',')[1]);
          const jpegBytes=Uint8Array.from(jpegRaw,c=>c.charCodeAt(0));
          window.fetch=(path,options)=>{
            if(/^\/(camera|visual)/.test(String(path))){__mediaRequests++;__requestedMedia.push(String(path));}
            if(/^\/camera\/usb(Front|Left|Hub2|Hub3)\.jpg$/.test(String(path))){
              __httpCameraRequests.push({path:String(path),time:performance.now()});
              const key=String(path).split('/').pop().split('.')[0];
              if(__httpTransportFailure==='503')return Promise.resolve(new Response(null,{status:503}));
              if(__httpTransportFailure==='abort')return Promise.reject(new DOMException('ISOLATED_TEST','AbortError'));
              if(__httpCameraState!=='AVAILABLE'||__httpCameraEmpty)return Promise.resolve(new Response(null,{status:204}));
              const old=__httpFrames[key];
              const frame=old&&__httpFreezeIdentity?old:{sequence:(old?.sequence||100)+1,observed:performance.now()};
              __httpFrames[key]=frame;
              const headers={'Content-Type':'image/jpeg','X-Frame-Sequence':__httpSession+':'+frame.sequence,
                ...(__httpOmitFreshness?{}:{'X-Frame-Age-S':'0.04','X-Frame-TTL-S':'2'})};
              if(String(path)==='/camera/usbFront.jpg'&&window.__delayHttpJpeg){
                window.__delayHttpJpeg=false;
                return new Promise(resolve=>{window.__lateHttpJpeg=()=>resolve(new Response(jpegBytes,
                  {headers}));});
              }
              return new Promise(resolve=>setTimeout(()=>resolve(new Response(jpegBytes,{headers})),
                __httpJitter?40+(frame.sequence*37)%140:0));
            }
            // Deliberately ignore cancellation once to verify the generation guard.
            if(String(path)==='/visuals.json'&&window.__delayVisual){
              window.__delayVisual=false;
              return new Promise(resolve=>{window.__lateVisualReply=()=>resolve(new Response(JSON.stringify({
                lidar:{status:'AVAILABLE',points:[[999,999,999]],totalPoints:1,source:'LATE_REPLY_MUST_NOT_RENDER'},
                scan:{status:'ERROR',reason:'LATE_REPLY_MUST_NOT_RENDER'}
              }),{headers:{'Content-Type':'application/json'}}));});
            }
            return String(path)==='/visuals.json'
              ? Promise.resolve(new Response(JSON.stringify(Object.fromEntries(['lidar','bluesea','scan'].map(key=>[key,
              key==='bluesea'&&__scanError ? {status:'ERROR',reason:'ISOLATED_BROWSER_FIXTURE invalid scan'} : __previewFixture
              ? {status:'AVAILABLE',points:[[0,0,0],[1,0,0],[0,1,0]],totalPoints:3,
                 frameId:'ISOLATED_BROWSER_FIXTURE',source:'TEST_ONLY',topic:'/test/'+key,
                 hz:key==='scan'?null:0,ageS:0}
              : {status:'STALE',reason:'ISOLATED_BROWSER_FIXTURE source stopped'}]))),
                {headers:{'Content-Type':'application/json'}}))
              : original(path,options);
          };
          RKObserverDashboard.openPage('media/scans');
        })()''')
        await evaluate("RKObserverDashboard.openPage('media/cameras')")
        await until("__fixtureSnapshot.ros2.status==='WAITING_UPSTREAM'&&document.getElementById('media-meta-usbFront').textContent.includes('TEST_ONLY_HTTP_UPLOAD')")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>{const e=document.getElementById('media-'+k);return e.complete&&e.naturalWidth===320&&e.naturalHeight===180&&!e.hidden&&document.getElementById('media-empty-'+k).hidden&&!document.querySelector('[data-record='+k+']').disabled;})")
        assert await evaluate("['color','depth'].every(k=>document.querySelector('[data-record='+k+']').disabled)")
        assert await evaluate("['fisheye','usbRear','usbPort21'].every(k=>!document.getElementById('media-'+k)&&!document.querySelector('[data-record='+k+']'))")
        assert await evaluate("__fixtureSnapshot.ros2.status==='WAITING_UPSTREAM'")
        for key in ("usbFront", "usbLeft", "usbHub2", "usbHub3"):
            assert await evaluate(f"document.getElementById('media-meta-{key}').innerText.includes('来源 Jetson 边缘计算机 · 网页预览')")
            assert await evaluate(f"!/(采集端|话题|TEST_ONLY_HTTP_UPLOAD|http_camera)/.test(document.getElementById('media-meta-{key}').innerText)")
        await evaluate("document.querySelector('[data-view-mode=detailed]').click()")
        assert await evaluate("document.getElementById('media-meta-usbFront').innerText.includes('采集端 TEST_ONLY_HTTP_UPLOAD')&&document.getElementById('media-meta-usbFront').innerText.includes('接口 /http_camera/camera2')&&!document.getElementById('media-meta-usbFront').innerText.includes('话题')")
        await evaluate("document.querySelector('[data-view-mode=compact]').click();window.__httpCameraRequests=[]")
        await asyncio.sleep(2.3)
        camera_requests = await evaluate("__httpCameraRequests")
        for key in ("usbFront", "usbLeft", "usbHub2", "usbHub3"):
            times = [entry["time"] for entry in camera_requests if entry["path"] == f"/camera/{key}.jpg"]
            assert 1 <= len(times) <= 3, (key, times)
            assert all(right - left >= 995 for left, right in zip(times, times[1:])), (key, times)
        for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
            await call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                              "deviceScaleFactor": 1, "mobile": width < 500})
            await evaluate("window.scrollTo(0,0)")
            await asyncio.sleep(.3)
            assert await evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert await evaluate("[...document.querySelectorAll('#camera-gallery .media-card')].length") == 6
            capture = await call("Page.captureScreenshot", {"format": "png"})
            (destination / f"{width}-http-camera-isolated-jpeg.png").write_bytes(base64.b64decode(capture["data"]))
        for state in ("STALE", "ERROR"):
            await evaluate(f"window.__httpCameraState={json.dumps(state)}")
            await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>document.getElementById('media-'+k).hidden&&!document.getElementById('media-'+k).hasAttribute('src')&&!document.getElementById('media-empty-'+k).hidden&&document.querySelector('[data-record='+k+']').disabled)")
            await evaluate("window.__httpCameraState='AVAILABLE'")
            await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        await evaluate("window.__httpCameraEmpty=true")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>document.getElementById('media-'+k).hidden&&!document.getElementById('media-empty-'+k).hidden&&document.querySelector('[data-record='+k+']').disabled)")
        await evaluate("window.__httpCameraEmpty=false")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        await evaluate("window.__delayHttpJpeg=true")
        await until("typeof __lateHttpJpeg==='function'")
        await evaluate("window.__httpCameraState='STALE'")
        await until("__fixtureSnapshot.devices.cameras.usbFront.status==='STALE'&&document.getElementById('media-state-usbFront').textContent.includes('过期')&&document.getElementById('media-usbFront').hidden")
        await evaluate("__lateHttpJpeg()")
        await asyncio.sleep(.3)
        assert await evaluate("document.getElementById('media-usbFront').hidden&&!document.getElementById('media-empty-usbFront').hidden&&document.querySelector('[data-record=usbFront]').disabled")
        await evaluate("window.__httpCameraState='AVAILABLE'")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        # A late WS status for an older frame must not kill a verified newer JPEG.
        await evaluate(r'''(() => {
          const button=document.querySelector('[data-record=usbFront]');button.click();
          const stale=structuredClone(__fixtureSnapshot);stale.meta.timestamp+=.000001;
          stale.devices.cameras.usbFront={...stale.devices.cameras.usbFront,status:'STALE',
            frameIdentity:__httpSession+':'+(__httpFrames.usbFront.sequence-2)};
          ObserverMedia.update(stale);
          // Replaying that exact snapshot time is not a new invalidation.
          stale.devices.cameras.usbFront.frameIdentity=__httpSession+':'+__httpFrames.usbFront.sequence;
          ObserverMedia.update(stale);
        })()''')
        assert await evaluate("!document.getElementById('media-usbFront').hidden&&document.getElementById('media-empty-usbFront').hidden&&document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='true'")
        await asyncio.sleep(.25)
        await evaluate("document.querySelector('[data-record=usbFront]').click()")
        await until("!document.querySelector('[data-download=usbFront]').hidden")
        # A transport retry may preserve only the remaining truthful lease.
        for failure in ("503", "abort"):
            await until("performance.now()-__httpFrames.usbFront.observed<200&&!document.querySelector('[data-record=usbFront]').disabled")
            await evaluate(f"window.__httpTransportFailure={json.dumps(failure)};window.__retryBaseline=__httpCameraRequests.length;document.querySelector('[data-record=usbFront]').click()")
            await until("__httpCameraRequests.slice(__retryBaseline).some(r=>r.path==='/camera/usbFront.jpg')")
            assert await evaluate("!document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='true'")
            await until("document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='false'&&!document.querySelector('[data-download=usbFront]').hidden")
            await evaluate("window.__httpTransportFailure=null")
            await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        # Repeated identities with even a bogus reset Age cannot renew a lease.
        await evaluate("window.__httpFreezeIdentity=true;document.querySelector('[data-record=usbFront]').click()")
        await until("document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='false'")
        await asyncio.sleep(1.3)
        assert await evaluate("document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').disabled")
        await evaluate("window.__httpFreezeIdentity=false")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        await evaluate("window.__httpOmitFreshness=true")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>document.getElementById('media-'+k).hidden&&document.querySelector('[data-record='+k+']').disabled)")
        await evaluate("window.__httpOmitFreshness=false")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        # Reconnect UI redraws must not replay an earlier stale camera snapshot.
        await evaluate("window.__socketBeforeReconnect=RKObserverDashboard.getConnection();__socketBeforeReconnect.close()")
        await until("RKObserverDashboard.getConnection()&&RKObserverDashboard.getConnection()!==__socketBeforeReconnect&&RKObserverDashboard.getConnection().readyState===WebSocket.OPEN")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        assert await evaluate("__wsCount") == 2
        assert await evaluate("__maxActiveWsCount") == 1
        await evaluate("window.__firstSocket=RKObserverDashboard.getConnection()")
        # An accepted new session retires the old one: a late old WS observation
        # cannot clear the new JPEG, even with a later snapshot timestamp.
        await evaluate("window.__previousHttpSession=__httpSession;window.__httpSession='isolated-session-B'")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)&&__fixtureSnapshot.devices.cameras.usbFront.frameIdentity.startsWith('isolated-session-B:')")
        await evaluate(r'''(() => {
          document.querySelector('[data-record=usbFront]').click();
          const old=structuredClone(__fixtureSnapshot);old.meta.timestamp+=.000001;
          old.devices.cameras.usbFront={...old.devices.cameras.usbFront,status:'STALE',
            frameIdentity:__previousHttpSession+':999999'};
          ObserverMedia.update(old);
        })()''')
        assert await evaluate("!document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').getAttribute('aria-pressed')==='true'")
        await asyncio.sleep(.25)
        await evaluate("document.querySelector('[data-record=usbFront]').click()")
        await until("!document.querySelector('[data-download=usbFront]').hidden")
        # Session switches must invalidate a held old GET even if the old image
        # was already cleared. The held response deliberately ignores abort.
        await evaluate("window.__httpCameraEmpty=true")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>document.getElementById('media-'+k).hidden)")
        await evaluate("window.__lateHttpJpeg=null;window.__delayHttpJpeg=true;window.__httpCameraEmpty=false")
        await until("typeof __lateHttpJpeg==='function'")
        await evaluate(r'''(() => {
          window.__httpSession='isolated-session-C';
          const changed=structuredClone(__fixtureSnapshot);changed.meta.timestamp+=.000001;
          changed.devices.cameras.usbFront={...changed.devices.cameras.usbFront,status:'AVAILABLE',
            frameIdentity:__httpSession+':1'};
          ObserverMedia.update(changed);__lateHttpJpeg();
        })()''')
        await asyncio.sleep(.15)
        assert await evaluate("document.getElementById('media-usbFront').hidden&&document.querySelector('[data-record=usbFront]').disabled")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        # Simultaneous 4-camera native recording with bounded network jitter and
        # genuinely older WS observations. Never exceed the real 2-second TTL.
        await evaluate("window.__httpJitter=true;window.__httpSkewSnapshots=true;window.__httpSkewedSnapshots=0;window.__cameraInvalidations=[];window.__httpCameraRequests=[];['usbFront','usbLeft','usbHub2','usbHub3'].forEach(k=>document.querySelector('[data-record='+k+']').click())")
        stress_started = time.monotonic()
        stress_checks = 0
        while time.monotonic() - stress_started < 125:
            await asyncio.sleep(.25)
            sample = await evaluate("({states:['usbFront','usbLeft','usbHub2','usbHub3'].map(k=>({key:k,visible:!document.getElementById('media-'+k).hidden&&document.getElementById('media-empty-'+k).hidden,recording:document.querySelector('[data-record='+k+']').getAttribute('aria-pressed')})),ws:__wsCount})")
            if sample["ws"] != 2 or not all(item["visible"] and item["recording"] == "true" for item in sample["states"]):
                sample["diagnostics"] = await evaluate("({invalidations:__cameraInvalidations.slice(-12),requests:__httpCameraRequests.slice(-24),status:['usbFront','usbLeft','usbHub2','usbHub3'].map(k=>document.getElementById('record-status-'+k).innerText)})")
                raise AssertionError(sample)
            stress_checks += 1
        stress_seconds = time.monotonic() - stress_started
        skewed_snapshots = await evaluate("__httpSkewedSnapshots")
        assert skewed_snapshots > 20
        stress_requests = await evaluate("__httpCameraRequests")
        stress_max_gaps = {}
        for key in ("usbFront", "usbLeft", "usbHub2", "usbHub3"):
            times = [entry["time"] for entry in stress_requests if entry["path"] == f"/camera/{key}.jpg"]
            gaps = [right-left for left,right in zip(times,times[1:])]
            # The fixture timestamps fetch(), after the request-start sample;
            # variable JS work can shift individual observed gaps by a few ms.
            # The pure helper test checks the exact 1000 ms scheduling boundary.
            assert 100 <= len(times) <= int(stress_seconds) + 2, (key, len(times))
            assert all(gap >= 900 for gap in gaps), (key, gaps)
            stress_max_gaps[key] = round(max(gaps), 3)
            assert stress_max_gaps[key] < 1500, (key, stress_max_gaps)
        await evaluate("['usbFront','usbLeft','usbHub2','usbHub3'].forEach(k=>document.querySelector('[data-record='+k+']').click());window.__httpJitter=false;window.__httpSkewSnapshots=false")
        await until("['usbFront','usbLeft','usbHub2','usbHub3'].every(k=>!document.querySelector('[data-download='+k+']').hidden)")
        for key in ("usbFront", "usbLeft", "usbHub2", "usbHub3"):
            await evaluate(f"document.querySelector('[data-download={key}]').click()")
        await asyncio.sleep(1)
        await evaluate("RKObserverDashboard.openPage('media/scans')")
        await until("['bluesea','scan'].every(k=>!document.querySelector('[data-record='+k+']').disabled)")
        assert await evaluate("document.querySelector('[data-record=lidar]').disabled")
        assert await evaluate("!document.querySelector('[data-cloud-view=bluesea]')&&!document.querySelector('[data-cloud-view=scan]')")
        assert await evaluate("document.getElementById('media-meta-bluesea').textContent.includes('实收 0 Hz')")
        assert await evaluate("document.getElementById('media-meta-scan').textContent.includes('实收 —')")
        assert await evaluate("document.getElementById('media-meta-scan').textContent.includes('来源 TEST_ONLY · 话题 /test/scan')")
        assert await evaluate("document.getElementById('media-meta-scan').textContent.includes('有效采样 3 点 / 原始 3 条距离')")
        assert await evaluate("document.getElementById('media-meta-scan').innerText.includes('来源 TEST_ONLY')")
        assert await evaluate("document.getElementById('media-meta-bluesea').innerText.includes('有效采样 3 点')&&document.getElementById('media-meta-bluesea').innerText.includes('实收 0 Hz')")
        assert await evaluate("!/(话题|坐标系|原始|ISOLATED_BROWSER_FIXTURE)/.test(document.getElementById('media-meta-scan').innerText)")
        await evaluate("document.querySelector('[data-view-mode=detailed]').click()")
        assert await evaluate("document.getElementById('media-meta-scan').innerText.includes('话题 /test/scan')&&document.getElementById('media-meta-scan').innerText.includes('坐标系 ISOLATED_BROWSER_FIXTURE')&&document.getElementById('media-meta-scan').innerText.includes('原始 3 条距离')")
        await evaluate("document.querySelector('[data-view-mode=compact]').click()")
        await evaluate("window.__scanError=true")
        await until("document.querySelector('[data-record=bluesea]').disabled&&document.getElementById('media-state-bluesea').textContent.includes('异常')")
        assert await evaluate("!document.querySelector('[data-record=scan]').disabled")
        assert await evaluate("document.getElementById('media-meta-bluesea').innerText.includes('ISOLATED_BROWSER_FIXTURE invalid scan')")
        assert await evaluate("__isVisible(document.getElementById('media-empty-bluesea'))&&document.getElementById('media-empty-bluesea').innerText.includes('invalid scan')")
        await evaluate("window.__scanError=false")
        await until("!document.querySelector('[data-record=bluesea]').disabled")
        for width, height in ((1920, 1080), (2560, 1440), (3440, 1440), (390, 844)):
            await call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                              "deviceScaleFactor": 1, "mobile": width < 500})
            for mode in ("compact", "detailed"):
                await evaluate(f"document.querySelector('[data-view-mode={mode}]').click()")
                await evaluate("document.getElementById('scan-gallery').scrollIntoView({block:'center'})")
                await asyncio.sleep(1.1)
                assert await evaluate("document.documentElement.scrollWidth<=innerWidth")
                assert await evaluate("[...document.querySelectorAll('#scan-gallery .media-meta .detail-only')].every(e=>__isVisible(e)===(document.body.dataset.view==='detailed'))")
                capture = await call("Page.captureScreenshot", {"format": "png"})
                (destination / f"{width}-scan-isolated-fixture-{mode}.png").write_bytes(base64.b64decode(capture["data"]))
        await evaluate("document.querySelector('[data-view-mode=compact]').click()")
        for key in ("usbFront", "usbLeft", "usbHub2", "usbHub3", "lidar", "bluesea", "scan"):
            category = "cameras" if key.startswith("usb") else "clouds" if key == "lidar" else "scans"
            await evaluate(f"window.__previewFixture=true;window.__httpCameraState='AVAILABLE';RKObserverDashboard.openPage('media/{category}')")
            # The overlay changes immediately, while inactive recorder buttons refresh
            # on a 100 ms timer. Do not click a stale enabled button during recovery.
            await until(f"document.getElementById('media-empty-{key}').hidden&&!document.querySelector('[data-record={key}]').disabled")
            await evaluate(f"document.querySelector('[data-record={key}]').click()")
            assert await evaluate(f"document.querySelector('[data-record={key}]').getAttribute('aria-pressed')") == "true"
            await asyncio.sleep(1.2)
            await evaluate(f"document.querySelector('[data-record={key}]').click()")
            await until(f"!document.querySelector('[data-download={key}]').hidden")
            assert await evaluate("__isVisible(document.querySelector('.recording-notice'))&&document.querySelector('.recording-notice').innerText.includes('未下载的视频')")
            assert await evaluate(f"document.querySelector('[data-record={key}]').title.includes('放弃上一段视频')")
            # Use the real download link; never relax production CSP for tests.
            existing_downloads = set(downloads.iterdir())
            await evaluate(f"document.querySelector('[data-download={key}]').click()")
            for _ in range(60):
                clips = [path for path in set(downloads.iterdir()) - existing_downloads
                         if path.suffix in (".webm", ".mp4") and path.stat().st_size > 32]
                if clips:
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError(f"Native browser recording was not downloaded: {key}")
            prior_clip = await evaluate(f"document.querySelector('[data-download={key}]').href")
            await evaluate(f"document.querySelector('[data-record={key}]').click()")
            assert await evaluate(f"document.querySelector('[data-download={key}]').hidden")
            assert await evaluate("__revokedUrls.includes(" + json.dumps(prior_clip) + ")")
            await asyncio.sleep(1.2)
            await evaluate("window.__httpCameraState='STALE'" if key.startswith("usb") else "window.__previewFixture=false")
            await until(f"!document.querySelector('[data-download={key}]').hidden&&document.querySelector('[data-record={key}]').disabled")
            assert await evaluate(f"document.getElementById('record-status-{key}').textContent.includes('自动停止')")
        # Only the visible category may request media. Switching still retains saved blobs.
        await evaluate("window.__previewFixture=true;RKObserverDashboard.openPage('media/cameras')")
        await asyncio.sleep(.3)
        await evaluate("window.__requestedMedia=[]")
        await asyncio.sleep(1.3)
        requests = await evaluate("__requestedMedia")
        assert requests and all(path.startswith("/camera/") for path in requests), requests
        await evaluate("RKObserverDashboard.openPage('media/scans')")
        await until("!document.querySelector('[data-record=scan]').disabled")
        await evaluate("window.__requestedMedia=[]")
        await asyncio.sleep(1.3)
        requests = await evaluate("__requestedMedia")
        assert requests and set(requests) == {"/visuals.json"}, requests
        await evaluate("window.__delayVisual=true;RKObserverDashboard.openPage('media/clouds')")
        await until("typeof __lateVisualReply==='function'")
        await evaluate("RKObserverDashboard.openPage('media/scans');__lateVisualReply()")
        await until("!document.querySelector('[data-record=scan]').disabled")
        assert await evaluate("!document.getElementById('media-meta-scan').textContent.includes('LATE_REPLY_MUST_NOT_RENDER')")
        assert await evaluate("document.getElementById('media-empty-lidar').hidden===false&&document.querySelector('[data-record=lidar]').disabled")
        # Use native MediaRecorder, not a fake recorder: folding only navigation must not stop it.
        await call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080,
                                                          "deviceScaleFactor": 1, "mobile": False})
        await evaluate("document.querySelector('[data-record=scan]').click()")
        await asyncio.sleep(1.2)
        await evaluate("document.querySelector('.nav[data-page=media]').click()")
        assert await evaluate("document.getElementById('subnav-media').hidden")
        assert await evaluate("RKObserverDashboard.getRoute()") == "media/scans"
        await asyncio.sleep(.3)
        assert await evaluate("document.querySelector('[data-record=scan]').getAttribute('aria-pressed')") == "true"
        assert await evaluate("document.querySelector('[data-download=scan]').hidden")
        await evaluate("document.querySelector('.nav[data-page=media]').click()")
        assert not await evaluate("document.getElementById('subnav-media').hidden")
        await evaluate("RKObserverDashboard.openPage('media/clouds')")
        await until("!document.querySelector('[data-download=scan]').hidden&&document.querySelector('[data-record=scan]').getAttribute('aria-pressed')==='false'")
        cross_category_clip = await evaluate("document.querySelector('[data-download=scan]').href")
        assert cross_category_clip.startswith("blob:")
        assert await evaluate("document.getElementById('record-status-scan').textContent.includes('自动停止')")
        await evaluate("RKObserverDashboard.openPage('media/scans')")
        await until("!document.querySelector('[data-record=scan]').disabled")
        assert await evaluate("__isVisible(document.querySelector('[data-download=scan]'))")
        assert await evaluate("document.querySelector('[data-download=scan]').href") == cross_category_clip
        assert await evaluate("__revokedUrls.includes(" + json.dumps(cross_category_clip) + ")") is False
        await evaluate("RKObserverDashboard.openPage('overview')")
        await asyncio.sleep(.3)
        await evaluate("window.__requestedMedia=[]")
        await asyncio.sleep(1.3)
        assert await evaluate("__requestedMedia") == []
        # Return to a live media subpage so expired-session polling checks are meaningful.
        await evaluate("RKObserverDashboard.openPage('media/scans')")
        await until("!document.querySelector('[data-record=scan]').disabled")
        assert await evaluate("__wsCount") == 2
        assert await evaluate("__maxActiveWsCount") == 1
        console_errors = [event for event in events if event.get("method") == "Runtime.exceptionThrown"
                          or (event.get("method") == "Log.entryAdded" and event["params"]["entry"]["level"] == "error")
                          or (event.get("method") == "Runtime.consoleAPICalled" and event["params"]["type"] == "error")]
        http_errors = [{"status": event["params"]["response"]["status"], "url": event["params"]["response"]["url"]} for event in events
                       if event.get("method") == "Network.responseReceived" and event["params"]["response"]["status"] >= 400]
        (destination / "console-results.json").write_text(json.dumps({"consoleErrors": console_errors,
            "httpErrors": http_errors}, ensure_ascii=False, indent=2))
        assert not console_errors, "Browser console contains errors"
        assert not http_errors, http_errors
        assert await evaluate("__removedCameraRequests") == []
        # A user may cancel navigation to save their local clip after logout.
        # In that state no protected polling or WebSocket reconnect may continue.
        accept_dialog = False
        await evaluate("document.getElementById('logout-button').click()")
        await until("!!window.CloudObserver&&CloudObserver.isExpired()")
        await asyncio.sleep(.3)
        request_count = await evaluate("window.__mediaRequests")
        await asyncio.sleep(1.3)
        assert await evaluate("window.__mediaRequests") == request_count
        assert await evaluate("window.__wsCount") == 2
        assert await evaluate("!document.querySelector('[data-download=lidar]').hidden")
        accept_dialog = True
        await call("Page.navigate", {"url": server + "/login"})
        await until("location.pathname==='/login'&&!!document.getElementById('login-form')")
        assert await evaluate("window.__wsCount") == 0
        report = {"fixtureScope": "ISOLATED_BROWSER_TAB_ONLY", "realRobotDataVerified": False,
                  "pages": results, "consoleErrors": len(console_errors), "httpErrors": http_errors,
                  "singleWebSocket": True, "nullAndZero": True, "historyRetained": True,
                  "offlineStaleRecovery": True, "emptyMediaRecordingDisabled": True,
                  "nativeRecordingStopDownloadRestartDiscardAndStale": True,
                  "independentLaserScanRecordingAndSourceMetadata": True,
                  "httpCamerasDecodedJpegAndNativeRecording": True,
                  "httpCameraStaleErrorAndEmptyHidden": True, "httpCameraPollingOneHz": True,
                  "lateHttpJpegCannotResurrectStaleFrame": True,
                  "oldSnapshotCannotClearNewJpeg": True, "replayedSnapshotIgnored": True,
                  "transientFailureRetainsOnlyTruthfulLease": True,
                  "sameFrameCannotRenewLease": True, "missingFreshnessFailsClosed": True,
                  "explicitReconnectRetainsMediaSafety": True,
                  "retiredSessionCannotClearNewFrame": True,
                  "newSessionInvalidatesHiddenPendingGet": True,
                  "fourCameraJitterAndOldSnapshotRecording": {"seconds": stress_seconds,
                      "checks": stress_checks, "olderSnapshots": skewed_snapshots,
                      "maxRequestGapMs": stress_max_gaps},
                  "httpCameraSourceNotRosAndDepthCamerasPreserved": True,
                  "historicalCameraCardsAbsent": True, "historicalCameraRequests": 0,
                  "subpageNavigation": navigation_results, "deepLinkReloadAndBackForward": True,
                  "unknownHashFallback": True, "visibleSubpageOnly": True,
                  "visibleCategoryOnlyPolling": True, "collapseKeepsRecording": True,
                  "lateHiddenCategoryResponseDiscarded": True,
                  "crossCategoryStopsRecordingAndRetainsDownload": True,
                  "compactDetailedCopy": copy_results, "normalNoteHiddenWarningsVisible": True,
                  "mediaSourceStatsAndErrorsVisible": True, "unsavedRecordingWarningVisible": True,
                  "expiredSessionStopsPollingAndReconnect": True, "loginLogout": True}
        (destination / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
