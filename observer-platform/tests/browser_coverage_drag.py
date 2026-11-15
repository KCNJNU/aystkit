"""Real Firefox pointer acceptance for sensor dragging and saved comparisons.

Uses isolated loopback backend, ephemeral credentials and synthetic geometry.
No production account, robot process or external network is used.
"""
from __future__ import annotations
import asyncio
import base64
import copy
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from aiohttp import web
import browser_coverage as qa

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.local/state/robot-cloud/coverage-drag-20261023'
ARTIFACTS = Path(os.environ.get('COVERAGE_DRAG_ARTIFACTS', str(STATE / 'artifacts')))
SERVER, DRIVER = 'http://127.0.0.1:19128', 'http://127.0.0.1:19129'
qa.ROOT, qa.ARTIFACTS, qa.SERVER, qa.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER
STORAGE, BASELINE = qa.STORAGE, 'observer.sensor-coverage.baseline.v1'


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-drag-browser-') as directory:
        config = Config(robot_id='coverage-drag-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'coverage-drag-fixture',
            'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT / 'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19128, access_log=None)


class Browser(qa.Browser):
    def testid(self, value):
        return self.evaluate('return document.querySelector(' + json.dumps('[data-testid="' + value + '"]') + ')?.textContent')

    def clickid(self, value):
        return self.evaluate('const element=document.querySelector(' + json.dumps('[data-testid="' + value + '"]') + ');if(!element)throw Error("Missing test ID");element.click();')

    def pointer(self, start, end=None, release=True, duration=600):
        steps = [{'type':'pointerMove','origin':'viewport','x':round(start[0]),'y':round(start[1]),'duration':0},
                 {'type':'pointerDown','button':0},{'type':'pause','duration':100}]
        if end:
            steps += [{'type':'pointerMove','origin':'viewport','x':round(end[0]),'y':round(end[1]),'duration':duration},
                      {'type':'pause','duration':250}]
        if release: steps += [{'type':'pointerUp','button':0}]
        return self.call('/actions',{'actions':[{'type':'pointer','id':'drag-mouse','parameters':{'pointerType':'mouse'},'actions':steps}]})

    def release(self):
        return self.call('/actions',{'actions':[{'type':'pointer','id':'drag-mouse','parameters':{'pointerType':'mouse'},'actions':[{'type':'pointerUp','button':0}]}]})

    def metrics(self):
        return self.evaluate("return [...document.querySelectorAll('.metrics article strong')].map(e=>Number(e.textContent))")

    def anchor(self, kind='sensor', sensor='camera1', axis='Z'):
        script = '''const host=document.querySelector('.three-host');
          const attribute=host?.getAttribute(KIND==='sensor'?'data-sensor-projections':'data-gizmo-projections');if(!attribute)return null;
          const anchors=JSON.parse(attribute);return KIND==='sensor'?anchors.find(a=>a.id===SENSOR):anchors[AXIS];'''
        value = self.wait_script(script.replace('KIND',json.dumps(kind)).replace('SENSOR',json.dumps(sensor)).replace('AXIS',json.dumps(axis)))
        return [value['x'],value['y']]

    def wait_script(self, script, timeout=15):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=self.evaluate(script)
            if value:return value
            time.sleep(.1)
        raise AssertionError('Anchor unavailable: '+script)


def assert_close(actual, expected, tolerance, label):
    assert abs(actual-expected)<=tolerance,(label,actual,expected)


def get_number(browser, testid):
    import re
    value=browser.testid(testid)
    assert value is not None,testid
    found=re.search(r'[+−\-]?\d+(?:\.\d+)?',value.replace('−','-'))
    assert found,(testid,value)
    result=float(found.group())
    return -result if '↓' in value else result


def assert_comparison(browser, reference, output):
    current=browser.metrics()
    for index,key,tolerance in [(0,'coverage',.16),(1,'blind',.016),(2,'overlap',.16)]:
        baseline=get_number(browser,key+'-baseline')
        delta=get_number(browser,key+'-delta')
        assert_close(baseline,reference[index],tolerance,key+' baseline')
        assert_close(delta,current[index]-reference[index],tolerance,key+' delta')
    output.update(current=current,baseline=reference,
                  displayed={key:browser.testid(key) for key in ['coverage-baseline','coverage-delta','blind-baseline','blind-delta','overlap-baseline','overlap-delta']})


def wait_config(browser, config):
    browser.wait('JSON.stringify(JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')))===JSON.stringify('+json.dumps(config,ensure_ascii=False)+')')
    time.sleep(.25)


def import_wait(browser, config):
    browser.import_config({'version':1,'config':config})
    browser.wait('document.querySelector("[role=status]")?.textContent.includes("已导入")')
    # Values may be normalized by the established sanitizer.
    browser.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].x==='+str(config['sensors'][0]['x']))
    time.sleep(.3)


def verify_page(browser,result):
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    browser.wait("document.querySelector('[data-testid=drag-toggle]')")
    result['initialRender']=browser.evaluate('return __coverageQA')
    assert not result['initialRender']['errors']
    result['loadedAssets']=browser.evaluate("return performance.getEntriesByType('resource').map(e=>e.name).filter(name=>name.includes('/coverage-assets/'))")
    result['sourceSha256']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((ROOT/'coverage-config/src').glob('*')) if path.is_file()}
    result['buildSha256']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT/'cloud/frontend/coverage').rglob('*')) if path.is_file()}
    result['loadedAssetSha256']={url:hashlib.sha256((ROOT/'cloud/frontend/coverage/assets'/url.rsplit('/',1)[1]).read_bytes()).hexdigest() for url in result['loadedAssets']}
    result['sourceDigests']=result['sourceSha256']
    result['assetDigests']=result['buildSha256']
    fetched=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];Promise.all(arguments[0].map(async url=>{
      const response=await fetch(url);if(!response.ok)throw Error(response.status);const digest=await crypto.subtle.digest('SHA-256',await response.arrayBuffer());
      return [url,[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('')];})).then(values=>done(Object.fromEntries(values))).catch(error=>done({error:String(error)}));''','args':[result['loadedAssets']]})
    assert fetched==result['loadedAssetSha256'],fetched
    result['loadedResponseAssetSha256']=fetched
    initial=browser.config()
    isolated=copy.deepcopy(initial)
    for sensor in isolated['sensors']:sensor['enabled']=sensor['id']=='camera1'
    import_wait(browser,isolated)
    browser.sensor('camera1')
    browser.button('俯视图')
    time.sleep(.4)
    browser.clickid('set-baseline')
    time.sleep(.25)
    before=browser.config()
    baseline=browser.metrics()
    browser.screenshot('drag-baseline.png')
    start=browser.anchor()
    rect=browser.evaluate("const r=document.querySelector('canvas').getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}")
    end=[min(rect['right']-50,start[0]+100),min(rect['bottom']-100,start[1]+75)]
    browser.pointer(start,end,release=False)
    browser.wait('Math.abs(JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].x-('+str(before['sensors'][0]['x'])+'))>.05||Math.abs(JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].y-('+str(before['sensors'][0]['y'])+'))>.05')
    dragging=browser.config()
    assert dragging['sensors'][0]['z']==before['sensors'][0]['z'],(before['sensors'][0],dragging['sensors'][0])
    assert browser.metrics()!=baseline,(baseline,browser.metrics())
    for axis in ['x','y','z']:
        value=browser.evaluate('return Number(document.querySelector('+json.dumps('[data-testid="sensor-camera1-'+axis+'"]')+').value)')
        assert_close(value,dragging['sensors'][0][axis],.011,'live '+axis+' left input')
    result['realPointerXYBeforeRelease']={'start':start,'end':end,'before':before['sensors'][0],'during':dragging['sensors'][0]}
    browser.release()
    time.sleep(.35)
    moved=browser.config()
    result['comparisonAfterXY']={}
    assert_comparison(browser,baseline,result['comparisonAfterXY'])
    browser.screenshot('drag-xy-and-comparison.png')
    assert any(abs(get_number(browser,key))>0 for key in ['coverage-delta','blind-delta'])
    browser.call('/refresh',{})
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    assert browser.config()==moved
    result['comparisonAfterReload']={}
    assert_comparison(browser,baseline,result['comparisonAfterReload'])
    browser.clickid('restore-baseline')
    time.sleep(.4)
    assert browser.config()['sensors']==before['sensors']
    for key in ['coverage-delta','blind-delta','overlap-delta']:assert get_number(browser,key)==0,(key,browser.testid(key))
    result['restoreBaselineAndReloadPersist']=True
    # Turning off editing must keep the same marker drag as a camera orbit.
    browser.button('俯视图')
    time.sleep(.3)
    browser.clickid('drag-toggle')
    immutable=browser.config()
    camera_before=browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")
    start=browser.anchor()
    browser.pointer(start,[start[0]+60,start[1]+35])
    time.sleep(.3)
    assert browser.config()==immutable
    assert browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")!=camera_before
    browser.clickid('drag-toggle')
    browser.button('俯视图')
    time.sleep(.25)
    # Dragging blank canvas must orbit without changing any installation data.
    camera_before=browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")
    browser.pointer([rect['right']-70,rect['top']+160],[rect['right']-140,rect['top']+220])
    assert browser.config()==immutable
    assert browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")!=camera_before
    result['disabledAndBlankDragPreserveSensors']=True
    # A real, visible Z-axis TransformControls handle must change only Z.
    browser.button('三维视角')
    time.sleep(.3)
    z_before=browser.config()
    z_start=browser.anchor('axis')
    browser.pointer(z_start,[z_start[0],z_start[1]-65],release=False)
    browser.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].z!=='+str(z_before['sensors'][0]['z']))
    z_during=browser.config()
    assert_close(z_during['sensors'][0]['x'],z_before['sensors'][0]['x'],.0001,'Z drag X')
    assert_close(z_during['sensors'][0]['y'],z_before['sensors'][0]['y'],.0001,'Z drag Y')
    assert 0<=z_during['sensors'][0]['z']<=4
    browser.release()
    result['realPointerZ']={'handle':z_start,'before':z_before['sensors'][0],'during':z_during['sensors'][0]}
    browser.screenshot('drag-z-handle.png')
    browser.clickid('set-baseline')
    time.sleep(.3)
    for key in ['coverage-delta','blind-delta','overlap-delta']:assert get_number(browser,key)==0
    result['replaceBaselineResetsDelta']=True
    # Physical drag paths use the same installation bounds as numeric inputs.
    bounded=browser.config()
    browser.call('/actions',{'actions':[{'type':'wheel','id':'zoom-wheel','actions':[
        {'type':'scroll','origin':'viewport','x':round((rect['left']+rect['right'])/2),'y':round((rect['top']+rect['bottom'])/2),
         'deltaX':0,'deltaY':600,'duration':400}]}]})
    time.sleep(.3)
    browser.field('Z · 高度',3.95)
    time.sleep(.3)
    start=browser.anchor('axis')
    browser.pointer(start,[start[0],max(rect['top']+30,start[1]-110)])
    assert browser.config()['sensors'][0]['z']==4,browser.config()['sensors'][0]
    browser.field('Z · 高度',.05)
    time.sleep(.3)
    start=browser.anchor('axis')
    browser.pointer(start,[start[0],min(rect['bottom']-40,start[1]+110)])
    assert browser.config()['sensors'][0]['z']==0,browser.config()['sensors'][0]
    import_wait(browser,bounded)
    result['dragZBoundsClampedAtZeroAndFour']=True
    xy_bounds=copy.deepcopy(bounded)
    xy_bounds['analysis'].update(length=12,width=12)
    for axis,bound,direction in [('x',6,[100,0]),('x',-6,[-100,0]),('y',6,[0,-100]),('y',-6,[0,100])]:
        candidate=copy.deepcopy(xy_bounds)
        candidate['sensors'][0].update(x=0,y=0)
        candidate['sensors'][0][axis]=bound-(.05 if bound>0 else -.05)
        import_wait(browser,candidate)
        browser.button('俯视图')
        time.sleep(.25)
        start=browser.anchor()
        browser.pointer(start,[start[0]+direction[0],start[1]+direction[1]])
        time.sleep(.25)
        assert browser.config()['sensors'][0][axis]==bound,(axis,bound,browser.config()['sensors'][0])
    import_wait(browser,bounded)
    result['dragXYBoundsClampedPlusMinusSix']=True
    # Context changes use current analysis/chassis for BOTH installations.
    stored=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    current=browser.config()
    current['sensors'][0]['x']+=.5
    current['analysis'].update(length=5,width=4,height=.65)
    current['chassis']['width']=.8
    import_wait(browser,current)
    same_context=copy.deepcopy(stored)
    same_context['analysis']=copy.deepcopy(current['analysis'])
    same_context['chassis']=copy.deepcopy(current['chassis'])
    import_wait(browser,same_context)
    context_reference=browser.metrics()
    import_wait(browser,current)
    result['comparisonSameAnalysisAndChassis']={}
    assert_comparison(browser,context_reference,result['comparisonSameAnalysisAndChassis'])
    # Escape and pointer cancellation release dragging and permit orbit again.
    for cancellation in ['escape','pointercancel','blur']:
        browser.button('三维视角')
        time.sleep(.2)
        original=browser.config()
        start=browser.anchor()
        browser.pointer(start,[start[0]+30,start[1]+15],release=False)
        if cancellation=='escape':
            browser.call('/actions',{'actions':[{'type':'key','id':'cancel-keyboard','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
        elif cancellation=='pointercancel':
            browser.evaluate("document.querySelector('canvas').dispatchEvent(new PointerEvent('pointercancel',{pointerId:window.__dragPointerId||0,bubbles:true}))")
        else:browser.evaluate("window.dispatchEvent(new Event('blur'))")
        browser.release()
        time.sleep(.25)
        value=browser.config()
        assert value==original,(cancellation,original,value)
        before_draw=browser.evaluate('return __coverageQA.drawCalls')
        camera_before=browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")
        browser.pointer([rect['right']-70,rect['top']+160],[rect['right']-130,rect['top']+190])
        assert browser.config()==value
        assert browser.evaluate("return document.querySelector('.three-host').dataset.cameraPosition")!=camera_before
        browser.wait('__coverageQA.drawCalls>'+str(before_draw))
        result['cancel_'+cancellation]=True
    # Existing import/export and defaults remain valid.
    browser.import_config({'version':99,'config':initial})
    browser.wait("document.querySelector('[role=status]')?.textContent.includes('导入失败')")
    browser.evaluate('''const create=URL.createObjectURL.bind(URL);URL.createObjectURL=blob=>{blob.text().then(text=>window.__coverageExport=JSON.parse(text));return create(blob)};
      const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){if(this.download)return;return click.call(this)};''')
    browser.button('导出配置')
    browser.wait('window.__coverageExport?.version===1')
    assert browser.evaluate('return __coverageExport.config')==browser.config()
    result['existingImportExportCompatibility']=True
    disabled=browser.config()
    browser.evaluate("document.querySelector('[data-testid=sensor-camera1] button[role=switch]').click()")
    browser.wait("!JSON.parse(document.querySelector('.three-host').dataset.sensorProjections).some(s=>s.id==='camera1')")
    browser.wait("document.querySelector('.coverage-metric strong').textContent==='0.0'")
    browser.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].enabled===false')
    browser.evaluate("document.querySelector('[data-testid=sensor-camera1] button[role=switch]').click()")
    browser.wait("JSON.parse(document.querySelector('.three-host').dataset.sensorProjections).some(s=>s.id==='camera1')")
    time.sleep(.25)
    assert browser.config()==disabled
    result['sensorEnableDisableStillWorks']=True
    browser.screenshot('drag-desktop-final.png')
    result['layouts']={'desktop':browser.layout()}
    browser.viewport(390,844)
    time.sleep(.4)
    result['layouts']['mobile']=browser.layout()
    browser.screenshot('drag-mobile.png')
    for size,layout in result['layouts'].items():
        assert layout['scrollWidth']<=layout['width']+2 and not layout['overflow'],(size,layout)
    result['errors']=browser.evaluate('return __coverageQA.errors')
    assert not result['errors'],result['errors']


def verify_auth(result):
    with qa.OPENER.open(SERVER+'/coverage') as response:
        assert response.geturl()==SERVER+'/login',response.geturl()
    cookie=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(cookie))
    body=json.dumps({'username':'drag-pending-fixture','password':secrets.token_urlsafe(20)}).encode()
    request=urllib.request.Request(SERVER+'/api/register',body,{'Content-Type':'application/json','Origin':SERVER})
    with opener.open(request) as response:assert response.status in [200,201]
    with opener.open(SERVER+'/coverage') as response:assert response.geturl()==SERVER+'/pending',response.geturl()
    result['authAnonymousAndPendingStillBlocked']=True


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-drag-qa-20261023'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[]
    browser=None
    result={'scope':'Isolated loopback backend, ephemeral account, genuine W3C pointer actions and real Three WebGL; no robot/production access','pass':False}
    password=secrets.token_urlsafe(24)
    try:
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=Path('/run/user/1000/gdm/Xauthority')
        if not env.get('DISPLAY') and Path('/tmp/.X11-unix/X1').exists() and authority.exists():
            env.update(DISPLAY=':1',XAUTHORITY=str(authority))
        for command,name in [([sys.executable,__file__,'--serve'],'drag-server'),
                (['/snap/bin/geckodriver','--host','127.0.0.1','--port','19129','--profile-root',str(profile)],'drag-geckodriver')]:
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated backend/geckodriver did not start')
        verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{
            'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{
                'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],
                'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,
                         'gfx.webrender.software':True,'webgl.enable-software-renderer':True}}}}},timeout=60)['value']
        browser=Browser(created)
        preload=qa.PRELOAD.replace('window.__coverageQA=',"addEventListener('pointerdown',e=>window.__dragPointerId=e.pointerId,true);window.__coverageQA=")
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':preload})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];
          fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})})
          .then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));''',
            'args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.viewport(1440,960)
        browser.call('/url',{'url':SERVER+'/coverage'})
        verify_page(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('drag-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['renderAtFailure']=browser.evaluate('return window.__coverageQA')
                result['anchorsAtFailure']=browser.evaluate("return document.querySelector('.three-host')?.dataset")
            except Exception as capture_error:result['captureFailure']=str(capture_error)
        raise
    finally:
        (ARTIFACTS/'browser-coverage-drag-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        (ARTIFACTS/'final-browser-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if browser:
            try:browser.call('',method='DELETE')
            except OSError:pass
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    serve() if '--serve' in sys.argv else verify()
