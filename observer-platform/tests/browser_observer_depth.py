"""Isolated real-Firefox acceptance of the observer spatial visual refinement.

All telemetry/point clouds below are clearly labelled synthetic fixtures. No
robot, camera, remote server, production credential or desktop auth is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from aiohttp import web
import browser_visual_polish as fixture
import browser_coverage as qa

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('OBSERVER_DEPTH_ARTIFACTS', '/tmp/observer-depth-20261024/artifacts'))
SERVER, DRIVER = 'http://127.0.0.1:19158', 'http://127.0.0.1:19159'
qa.ROOT, qa.ARTIFACTS, qa.SERVER, qa.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER
fixture.ROOT, fixture.ARTIFACTS, fixture.SERVER, fixture.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER
STATE = fixture.STATE
REQUESTS = fixture.REQUESTS
STATE.update(visual_active=0, visual_max=0, visual_times=[], visual_mode='available')
BOOTSTRAP = fixture.BOOTSTRAP + r'''
window.__testVisibility=false;
Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__testVisibility});
Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>window.__testVisibility?'hidden':'visible'});
window.__pointRectangles=[];
const qaFillRect=CanvasRenderingContext2D.prototype.fillRect;
CanvasRenderingContext2D.prototype.fillRect=function(...args){
 if(this.canvas.id==='overview-spatial-canvas'){
   __pointRectangles.push(args);if(__pointRectangles.length>30000)__pointRectangles.splice(0,__pointRectangles.length-30000);
 }return qaFillRect.apply(this,args);
};
'''


def visuals():
    # The original fixture coupled every source to websocket mode. This test
    # deliberately separates fresh HTTP measurements from a silent websocket.
    original_mode=STATE['mode']
    if original_mode=='silent':STATE['mode']='available'
    values = fixture.visuals()
    STATE['mode']=original_mode
    mode = STATE['visual_mode']
    for key, item in values.items():
        if mode == 'empty':
            item['points'] = []
            item['totalPoints'] = 0
        elif mode in ('scanOnly', 'cloudExpired') and key not in ('scan', 'bluesea'):
            item['status'] = 'WAITING_UPSTREAM' if mode == 'scanOnly' else 'AVAILABLE'
            item['points'] = [] if mode == 'scanOnly' else item['points']
            item['ageS'] = None if mode == 'scanOnly' else 900
        elif mode == 'oversized' and key not in ('scan', 'bluesea'):
            item['points'] = [[(n % 100) / 10, (n // 100 % 100) / 10, (n % 13) / 10] for n in range(25000)]
            item['totalPoints'] = 25000
        elif mode == 'invalid':
            item['points'] = [['bad', 1, 2], [1], [1, 2, 'bad'], [1e100, 2, 3], None]
    return values


async def handle(request):
    REQUESTS[request.path] += 1
    if request.path == '/__test/state':
        return web.json_response({**STATE, 'requests': dict(REQUESTS)})
    if request.path == '/__test/mode':
        body = await request.json()
        if body.get('mode') not in ('available', 'stale', 'waiting', 'silent'):
            raise web.HTTPBadRequest()
        STATE['mode'] = body['mode']
        STATE['visual_mode'] = body.get('visual_mode', 'available')
        return web.json_response(STATE)
    if request.path == '/__test/visual-mode':
        STATE['visual_mode'] = (await request.json())['mode']
        return web.json_response(STATE)
    if request.path == '/api/session':
        return web.json_response({'authenticated': True, 'approved': True, 'role': 'admin', 'username': 'ISOLATED FIXTURE'})
    if request.path == '/native-camera/status':
        return web.json_response(fixture.native_status())
    if request.path == '/visuals.json':
        STATE['visual_active'] += 1
        STATE['visual_max'] = max(STATE['visual_active'], STATE['visual_max'])
        STATE['visual_times'].append(time.monotonic())
        try:
            if STATE['visual_mode']=='silent':
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    if request.transport is None or request.transport.is_closing():
                        raise web.HTTPServiceUnavailable(text='Isolated transport pause')
                    await asyncio.sleep(.05)
                raise web.HTTPServiceUnavailable(text='Isolated transport pause')
            await asyncio.sleep(.06)
            return web.json_response(visuals())
        finally:
            STATE['visual_active'] -= 1
    if request.path == '/ws':
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        STATE['ws_total'] += 1
        STATE['ws_active'] += 1
        STATE['ws_max'] = max(STATE['ws_active'], STATE['ws_max'])
        try:
            while not socket.closed:
                if STATE['mode'] != 'silent':
                    STATE['sequence'] += 1
                    await socket.send_json(fixture.snapshot(STATE['sequence']))
                await asyncio.sleep(.6)
        finally:
            STATE['ws_active'] -= 1
        return socket
    if request.path == '/__test/bootstrap.js':
        return web.Response(text=BOOTSTRAP, content_type='application/javascript')
    if request.path == '/__test/chart-hook.js':
        return web.Response(text=fixture.CHART_HOOK, content_type='application/javascript')
    if request.path.endswith('video.mp4'):
        raise web.HTTPServiceUnavailable(text='Isolated UI fixture; no recorded or real camera stream')
    if request.path.startswith(('/frame/', '/visual/')):
        raise web.HTTPServiceUnavailable(text='No image fixture available')
    name = 'index.html' if request.path == '/' else request.path.lstrip('/')
    path = ROOT / 'cloud/frontend' / name
    if path.parent != ROOT / 'cloud/frontend' or not path.is_file():
        raise web.HTTPNotFound()
    if name == 'index.html':
        html = path.read_text().replace('<script src="/cloud.js">', '<script src="/__test/bootstrap.js"></script><script src="/cloud.js">')
        html = html.replace('<script src="/charts.js"></script>', '<script src="/charts.js"></script><script src="/__test/chart-hook.js"></script>')
        return web.Response(text=html, content_type='text/html', headers={
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' ws://127.0.0.1:19158"})
    return web.FileResponse(path)


def serve():
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handle)
    web.run_app(app, host='127.0.0.1', port=19158, access_log=None)


class Browser(qa.Browser):
    def layout(self):
        return self.evaluate('''return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,
          overflow:[...document.querySelectorAll('.page.active *')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return r.width&&r.right>innerWidth+2&&s.position!=='fixed'&&s.visibility!=='hidden'})
          .slice(0,12).map(e=>({tag:e.tagName,id:e.id,class:String(e.className),right:e.getBoundingClientRect().right}))};''')

    def route(self, route):
        self.evaluate('RKObserverDashboard.openPage('+json.dumps(route)+');window.scrollTo(0,0)')
        self.wait('RKObserverDashboard.getRoute()==='+json.dumps(route))

    def spatial(self):
        return self.evaluate("return {...document.getElementById('overview-spatial-canvas').dataset}")

    def pointer_drag(self, selector, dx, dy):
        rectangle = self.evaluate('const r=document.querySelector('+json.dumps(selector)+').getBoundingClientRect();return {x:r.x+r.width*.5,y:r.y+r.height*.5}')
        x, y = round(rectangle['x']), round(rectangle['y'])
        self.call('/actions', {'actions':[{'type':'pointer','id':'mouse','parameters':{'pointerType':'mouse'},'actions':[
            {'type':'pointerMove','duration':0,'x':x,'y':y},{'type':'pointerDown','button':0},
            {'type':'pointerMove','duration':500,'x':x+dx,'y':y+dy},{'type':'pointerUp','button':0}]}]})
        self.call('/actions', method='DELETE')

    def wheel(self, selector, delta):
        rectangle=self.evaluate('const r=document.querySelector('+json.dumps(selector)+').getBoundingClientRect();return {x:r.x+r.width*.5,y:r.y+r.height*.5}')
        self.call('/actions', {'actions':[{'type':'wheel','id':'wheel','actions':[
            {'type':'scroll','duration':250,'x':round(rectangle['x']),'y':round(rectangle['y']),'deltaX':0,'deltaY':delta}]}]})
        self.call('/actions', method='DELETE')


def mode(value, visual='available'):
    return qa.request(SERVER+'/__test/mode','POST',{'mode':value,'visual_mode':visual})


def visual_mode(value):
    return qa.request(SERVER+'/__test/visual-mode','POST',{'mode':value})


def verify_assets(browser, result):
    names = ['index.html','app.js','charts.js','app.css','cloud.css','media.js']
    result['sourceDigests'] = {'cloud/frontend/'+name:hashlib.sha256((ROOT/'cloud/frontend'/name).read_bytes()).hexdigest() for name in names}
    result['browserRunFrontendDigests'] = dict(result['sourceDigests'])
    result['assetDigests'] = dict(result['sourceDigests'])
    result['sourceDigests']['cloud/backend/app.py']=hashlib.sha256((ROOT/'cloud/backend/app.py').read_bytes()).hexdigest()
    # Bind the browser artifact to the checked-out backend without claiming
    # the synthetic HTTP fixture executes the production backend. If a later
    # backend-only fix is accepted, preserve this original value and record its
    # independent regression evidence in backendPostAcceptanceCheck.
    result['backendSourceAtBrowserRun']=result['sourceDigests']['cloud/backend/app.py']
    urls = [SERVER+'/' if name=='index.html' else SERVER+'/'+name for name in names]
    fetched = browser.call('/execute/async', {'script':'''const done=arguments[arguments.length-1];Promise.all(arguments[0].map(async url=>{
      const r=await fetch(url);if(!r.ok)throw Error(r.status);let bytes=await r.arrayBuffer();
      if(new URL(url).pathname==='/'){let text=new TextDecoder().decode(bytes);
        text=text.replace('<script src="/__test/bootstrap.js"></script>','').replace('<script src="/__test/chart-hook.js"></script>','');
        bytes=new TextEncoder().encode(text);}
      const digest=await crypto.subtle.digest('SHA-256',bytes);
      return [url,[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('')];})).then(v=>done(Object.fromEntries(v))).catch(e=>done({error:String(e)}));''','args':[urls]})
    expected={url:result['assetDigests']['cloud/frontend/'+(url.rsplit('/',1)[1] or 'index.html')] for url in urls}
    assert fetched==expected,(fetched,expected)
    result['loadedResponseAssetSha256']=fetched
    result['indexResponseNormalization']='Removed only the two explicitly test-injected script tags before comparing exact source bytes; business HTML is unchanged.'
    baseline=json.loads((ROOT.parent/'baseline-manifest.json').read_text())['files']
    unchanged={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in baseline if name not in result['sourceDigests']}
    assert all(unchanged[name]==baseline[name] for name in unchanged),{name:digest for name,digest in unchanged.items() if digest!=baseline[name]}
    result['unchangedBaselineContracts']=unchanged


def verify_page(browser,result):
    browser.viewport(1920,1080)
    browser.call('/url',{'url':SERVER+'/#overview'})
    browser.wait("window.RKObserverDashboard&&RKObserverDashboard.getHistoryCount('networkRx')>=6")
    browser.wait("document.getElementById('overview-spatial-canvas')")
    time.sleep(1.4)
    result['initialSpatial']=browser.spatial()
    result['percentageGauges']=browser.evaluate("return [...document.querySelectorAll('#overview-kpis .kpi-gauge')].map(e=>({value:e.dataset.value,arc:e.querySelector('.kpi-gauge-value').getAttribute('stroke-dasharray'),label:e.closest('.kpi-card').querySelector('.kpi-label').textContent}))")
    assert len(result['percentageGauges'])==3,result['percentageGauges']
    assert sum(g['value']=='unknown' and g['arc'].startswith('0 ') for g in result['percentageGauges'])==1,result['percentageGauges']
    for gauge in result['percentageGauges']:
        if gauge['value']!='unknown':assert math.isclose(float(gauge['arc'].split()[0]),float(gauge['value'])),gauge
    assert not any(p.endswith('video.mp4') or '/frame/' in p for p in qa.request(SERVER+'/__test/state')['requests'])
    result['overviewDoesNotOpenVideoOrJPEG']=True
    before_poll=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    time.sleep(3.3)
    after_poll=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    assert 2<=after_poll-before_poll<=4,(before_poll,after_poll)
    result['overviewVisualRequestsAtMostOnePerSecond']={'windowS':3.3,'requests':after_poll-before_poll}
    result['layouts']={}
    routes=['overview','system/performance','system/environment','robot/motion','robot/actuators','robot/sensors','robot/docking','stm32','ros','npu','media/cameras','media/scans','media/clouds','media/maps']
    for size,width,height in [('desktop-1920',1920,1080),('desktop-1440',1440,960),('mobile',390,844)]:
        browser.viewport(width,height)
        for route in routes:
            browser.route(route)
            time.sleep(.7 if route.startswith('media/') else .35)
            layout=browser.layout()
            assert layout['width']==width and layout['height']==height,layout
            assert layout['scrollWidth']<=width+2 and not layout['overflow'],(size,route,layout)
            result['layouts'][size+'/'+route]=layout
            browser.screenshot(size+'-'+route.replace('/','-')+'.png')
            if route=='overview':
                browser.evaluate("document.querySelector('.telemetry-grid')?.scrollIntoView({block:'start'})")
                browser.screenshot(size+'-overview-trends.png')
                browser.evaluate("document.querySelector('.overview-spatial')?.scrollIntoView({block:'start'})")
                browser.screenshot(size+'-overview-spatial.png')
            if route=='media/cameras':
                controls=browser.evaluate("return [...document.querySelectorAll('#camera-gallery [data-record]')].map(e=>({key:e.dataset.record,text:e.textContent,disabled:e.disabled}))")
                assert len(controls)>=4 and all('录制' in c['text'] for c in controls),controls
                result.setdefault('recordingControls',{})[size]=controls
    browser.viewport(1440,960)
    browser.route('overview')
    time.sleep(1.2)
    result['spatialBefore']=browser.spatial()
    browser.pointer_drag('#overview-spatial-canvas',90,35)
    time.sleep(.3)
    result['spatialAfterDrag']=browser.spatial()
    browser.wheel('#overview-spatial-canvas',-250)
    time.sleep(.3)
    result['spatialAfterWheel']=browser.spatial()
    assert int(result['spatialBefore']['pointCount'])>0,result['spatialBefore']
    assert result['spatialBefore']['source'] in ('lidar','registered','depthPoints'),result['spatialBefore']
    assert result['spatialBefore']['frameId']=='TEST_ONLY_XYZ',result['spatialBefore']
    assert result['spatialBefore']['dimension']=='3',result['spatialBefore']
    assert result['spatialAfterDrag']['yaw']!=result['spatialBefore']['yaw'],(result['spatialBefore'],result['spatialAfterDrag'])
    assert result['spatialAfterWheel']['zoom']!=result['spatialAfterDrag']['zoom'],(result['spatialAfterDrag'],result['spatialAfterWheel'])
    browser.evaluate("document.querySelector('[data-overview-view=top]').click()")
    assert browser.spatial()['view']=='top',browser.spatial()
    browser.evaluate("document.querySelector('[data-overview-view=perspective]').click()")
    assert browser.spatial()['view']=='perspective',browser.spatial()
    result['spaceAndTopControlsWork']=True
    result['navigationEntrances']=browser.evaluate("return {coverage:document.querySelector('a[href=\"/coverage\"]')?.textContent,users:document.getElementById('users-link')?.hidden,logout:!!document.getElementById('logout-button')}")
    assert result['navigationEntrances']['coverage'] and result['navigationEntrances']['users'] is False and result['navigationEntrances']['logout']
    state=qa.request(SERVER+'/__test/state')
    assert state['ws_max']==1,state
    assert state['visual_max']==1,state
    result['oneWebSocketAndOneVisualRequest']={'wsMax':state['ws_max'],'wsTotal':state['ws_total'],'visualMax':state['visual_max']}
    for current in ('stale','waiting'):
        mode(current)
        browser.wait('__testHistory.networkRx.last.v===null&&__testHistory.cameraFrontFps.last.v===null')
        time.sleep(1.3)
        result['spatial-'+current]=browser.spatial()
        assert result['spatial-'+current]['pointCount']=='0',result['spatial-'+current]
        empty_gauges=browser.evaluate("return [...document.querySelectorAll('#overview-kpis .kpi-gauge')].every(e=>e.dataset.value==='unknown'&&e.querySelector('.kpi-gauge-value').getAttribute('stroke-dasharray').startsWith('0 '))")
        assert empty_gauges,(current,'Nonempty stale gauge')
        browser.evaluate('__testCalls.length=0')
        time.sleep(.8)
        assert not browser.evaluate('return __testCalls.filter(p=>p.radius===6)'),(current,'Stale chart endpoint markers')
    mode('available')
    browser.wait('__testHistory.networkRx.last.v!==null')
    time.sleep(1.2)
    for current in ('scanOnly','cloudExpired','empty','oversized','invalid'):
        visual_mode(current)
        time.sleep(1.4)
        result['spatial-'+current]=browser.spatial()
        browser.screenshot('spatial-'+current+'.png')
        if current in ('scanOnly','cloudExpired'):
            assert result['spatial-'+current]['source'] in ('bluesea','scan'),result['spatial-'+current]
            assert result['spatial-'+current]['dimension']=='2',result['spatial-'+current]
            assert int(result['spatial-'+current]['pointCount'])>0,result['spatial-'+current]
        elif current in ('empty','invalid'):
            assert result['spatial-'+current]['pointCount']=='0',result['spatial-'+current]
        elif current=='oversized':
            assert 0<int(result['spatial-'+current]['pointCount'])<=4000,result['spatial-'+current]
    visual_mode('available')
    time.sleep(1.3)
    before=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    browser.route('system/performance')
    time.sleep(2.5)
    after=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    assert after-before<=1,(before,after)
    result['visualRequestsStopOnOtherPage']={'before':before,'after':after}
    browser.route('overview')
    time.sleep(1.3)
    browser.evaluate("window.__testVisibility=true;document.dispatchEvent(new Event('visibilitychange'))")
    before=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    time.sleep(2.5)
    after=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    assert after-before<=1,(before,after)
    result['visualRequestsStopInBackground']={'before':before,'after':after}
    assert browser.spatial()['pointCount']=='0',browser.spatial()
    browser.evaluate("window.__testVisibility=false;document.dispatchEvent(new Event('visibilitychange'))")
    time.sleep(1.3)
    mode('silent')
    time.sleep(.8)
    count=browser.evaluate("return RKObserverDashboard.getHistoryCount('networkRx')")
    time.sleep(4.8)
    after_count=browser.evaluate("return RKObserverDashboard.getHistoryCount('networkRx')")
    result['silentDisconnect']={'before':count,'after':after_count,'spatial':browser.spatial(),'cloudText':browser.evaluate("return document.getElementById('cloud-robot-status').textContent")}
    assert count==after_count,result['silentDisconnect']
    assert '中断' in result['silentDisconnect']['cloudText'],result['silentDisconnect']
    # HTTP point data is independent of the telemetry websocket. A genuinely
    # fresh HTTP scan may stay visible while telemetry displays disconnect.
    assert int(result['silentDisconnect']['spatial']['pointCount'])>0,result['silentDisconnect']
    assert browser.evaluate("return [...document.querySelectorAll('#overview-kpis .kpi-gauge')].every(e=>e.dataset.value==='unknown'&&e.querySelector('.kpi-gauge-value').getAttribute('stroke-dasharray').startsWith('0 '))"),'Stale telemetry gauges must not show active arcs'
    visual_mode('silent')
    time.sleep(2.7)
    result['allDataSilentClearsSpatial']=browser.spatial()
    assert result['allDataSilentClearsSpatial']['pointCount']=='0',result['allDataSilentClearsSpatial']
    mode('available')
    browser.wait('__testHistory.networkRx.last.v!==null')
    browser.route('media/scans')
    browser.wait("!document.querySelector('[data-record=bluesea]').disabled")
    browser.evaluate("document.querySelector('[data-record=bluesea]').click()")
    browser.wait("document.querySelector('[data-record=bluesea]').getAttribute('aria-pressed')==='true'")
    time.sleep(1.5)
    browser.route('overview')
    browser.wait("document.querySelector('[data-record=bluesea]').getAttribute('aria-pressed')==='false'")
    browser.wait("!document.querySelector('[data-download=bluesea]').hidden")
    result['recordingStartsAndStopsOnNavigation']={
        'status':browser.evaluate("return document.getElementById('record-status-bluesea').textContent"),
        'download':browser.evaluate("return document.querySelector('[data-download=bluesea]').href")}
    assert '自动停止' in result['recordingStartsAndStopsOnNavigation']['status'],result['recordingStartsAndStopsOnNavigation']
    browser.wait("Number(document.getElementById('overview-spatial-canvas').dataset.pointCount)>0")
    # Test-only expiry stub keeps the document available for inspecting the
    # synchronous cleanup; a production expiry additionally redirects to login.
    browser.evaluate("window.__realCloudObserver=CloudObserver;window.CloudObserver={...CloudObserver,isExpired:()=>true};dispatchEvent(new Event('cloud-auth-lost'))")
    before=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    time.sleep(2.5)
    after=qa.request(SERVER+'/__test/state')['requests'].get('/visuals.json',0)
    result['authExpiryStopsAndClears']={'before':before,'after':after,'spatial':browser.spatial(),'fixture':'Test-only expired-session state and real cloud-auth-lost event; redirect suppressed to inspect cleanup'}
    assert result['authExpiryStopsAndClears']['spatial']['pointCount']=='0' and after-before<=1,result['authExpiryStopsAndClears']
    result['errors']=browser.evaluate('return __testErrors')
    assert not result['errors'],result['errors']


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-depth-qa-20261024'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[]
    browser=None
    result={'scope':'Isolated loopback synthetic telemetry and point clouds, no robot/production credentials/camera stream, real Firefox pointer and wheel actions, private Xvfb','pass':False}
    try:
        env={**os.environ,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority'
        authority.touch(mode=0o600,exist_ok=True)
        display=':99'
        subprocess.run(['xauth','-f',str(authority),'add',display,'MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb')
        assert xvfb.exists(),xvfb
        log=(ARTIFACTS/'depth-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),display,'-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=display,XAUTHORITY=str(authority))
        time.sleep(.5)
        for command,name in [([sys.executable,__file__,'--serve'],'depth-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19159','--profile-root',str(profile)],'depth-geckodriver')]:
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/__test/state');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Fixture or driver did not start')
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True}}}}},timeout=60)['value']
        browser=Browser(created)
        verify_page(browser,result)
        verify_assets(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('depth-failure.png')
                result['failureText']=browser.evaluate('return document.querySelector(".page.active")?.innerText||document.body.innerText')
                result['errors']=browser.evaluate('return window.__testErrors')
            except Exception as capture_error:result['captureFailure']=str(capture_error)
        raise
    finally:
        (ARTIFACTS/'final-browser-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if browser:
            try:browser.call('',method='DELETE')
            except OSError:pass
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()
        print(json.dumps({'pass':result['pass'],'failure':result.get('failure'),'report':str(ARTIFACTS/'final-browser-report.json')},ensure_ascii=False))


if __name__=='__main__':
    serve() if '--serve' in sys.argv else verify()
