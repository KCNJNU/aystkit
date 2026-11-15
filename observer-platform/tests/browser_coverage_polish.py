"""Firefox acceptance for the refined sensor coverage configuration interface.

Runs an isolated loopback backend with ephemeral users and a private Xvfb.
No production credential, desktop authorization, robot or remote host is used.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from aiohttp import web
import browser_coverage as qa
import browser_coverage_drag as drag
import browser_coverage_frame as frame

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('COVERAGE_POLISH_ARTIFACTS', '/tmp/sensor-polish-20261024/artifacts'))
SERVER, DRIVER = 'http://127.0.0.1:19148', 'http://127.0.0.1:19149'
STORAGE = qa.STORAGE
for module in (qa, drag, frame):
    module.ROOT, module.ARTIFACTS, module.SERVER, module.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-polish-browser-') as directory:
        config = Config(robot_id='coverage-polish-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'coverage-polish-fixture',
            'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT / 'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19148, access_log=None)


class Browser(frame.Browser):
    def wait(self, expression, timeout=15):
        # The original acceptance predates the six tiles plus independent editor.
        expression = expression.replace("document.querySelectorAll('.sensor-card').length===6",
                                        "document.querySelectorAll('.sensor-select').length===6")
        return super().wait(expression, timeout)

    def sensor(self, identifier):
        self.clickid('sensor-' + identifier)
        self.wait('document.querySelector(' + json.dumps('[data-testid="sensor-' + identifier + '-rail"]') + ')')

    def clickid(self, value):
        return self.evaluate('''const element=document.querySelector(SELECTOR);if(!element)throw Error('Missing test ID: '+TESTID);
          const button=element.matches('.sensor-select')?element:element.querySelector('.sensor-select');
          (button||element).dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));'''
          .replace('SELECTOR', json.dumps('[data-testid="'+value+'"]')).replace('TESTID', json.dumps(value)))

    def range(self, testid, value):
        self.evaluate('''const element=document.querySelector(SELECTOR);if(!element)throw Error('Missing range input');
          const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          setter.call(element,String(VALUE));element.dispatchEvent(new Event('input',{bubbles:true}));
          element.dispatchEvent(new Event('change',{bubbles:true}));'''
          .replace('SELECTOR',json.dumps('[data-testid="'+testid+'"]')).replace('VALUE',json.dumps(value)))
        time.sleep(.35)

    def field(self, label, value, scope='.sensor-card.is-open'):
        aliases={'X · 前后':'X 前后','Y · 左右':'Y 左右','Z · 高度':'Z 高度',
                 '向下倾斜':'向下倾角 · Pitch','自定义偏航角':'偏航角 · Yaw'}
        return super().field(aliases.get(label,label),value,scope)


def verify_polish(browser, result):
    browser.wait("document.querySelectorAll('.sensor-select').length===6&&__coverageQA.drawCalls>0")
    initial=browser.config()
    assert len(initial['sensors'])==6
    selection=[]
    for sensor in initial['sensors']:
        identifier=sensor['id']
        window_y=browser.evaluate('return scrollY')
        browser.sensor(identifier)
        assert browser.evaluate('return document.querySelectorAll(".sensor-card.is-open .sensor-body").length')==1
        browser.wait('document.querySelector('+json.dumps('[data-testid="sensor-'+identifier+'-position"]')+')')
        browser.wait("(()=>{const p=document.querySelector('.panel-scroll').getBoundingClientRect(),e=document.querySelector('.sensor-card.is-open').getBoundingClientRect();return e.top>=p.top-1&&e.top<=p.top+45})()")
        assert browser.evaluate('return scrollY')==window_y
        selection.append({'id':identifier,'panel':True})
    for sensor in reversed(initial['sensors']):
        identifier=sensor['id']
        browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
        window_y=browser.evaluate('return scrollY')
        browser.clickid('layout-map-'+identifier)
        browser.wait('document.querySelector('+json.dumps('[data-testid="sensor-'+identifier+'-rail"]')+')')
        browser.wait("(()=>{const p=document.querySelector('.panel-scroll').getBoundingClientRect(),e=document.querySelector('.sensor-card.is-open').getBoundingClientRect();return e.top>=p.top-1&&e.top<=p.top+45})()")
        assert browser.evaluate('return scrollY')==window_y
        browser.wait('''(()=>{const host=document.querySelector('.three-host'),point=JSON.parse(host.dataset.sensorProjections||'[]').find(s=>s.id===SENSOR);
          return point&&Math.abs(Number(host.dataset.selectedScreenX)-point.x)<.01&&Math.abs(Number(host.dataset.selectedScreenY)-point.y)<.01})()'''.replace('SENSOR',json.dumps(identifier)))
    result['sixTilesAndLayoutMapSelection']=selection
    result['selectionScrollsOnlyEditorIntoPanel']=True
    browser.sensor('camera1')
    browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
    browser.screenshot('polish-desktop-overview-initial.png')
    browser.clickid('view-mounting')
    time.sleep(.45)
    assert browser.config()==initial
    result['mountingCloseViewKeepsConfiguration']=True
    browser.screenshot('polish-desktop-mounting-close.png')
    browser.button('三维视角')
    isolated=copy.deepcopy(initial)
    for sensor in isolated['sensors']:sensor['enabled']=sensor['id']=='camera1'
    isolated['sensors'][0].update(hfov=70,vfov=70,maxRange=2.5,pitch=40)
    isolated['analysis'].update(length=4,width=4,resolution=.05)
    frame.import_wait(browser,isolated)
    browser.sensor('camera1')
    browser.select_rail('x-top-left')
    browser.range('sensor-camera1-position',.2)
    before=browser.config()
    before_metrics=browser.metrics()
    before_draw=browser.evaluate('return __coverageQA.drawCalls')
    browser.range('sensor-camera1-position',.78)
    after=browser.config()
    frame.assert_on_frame(after)
    assert abs(after['sensors'][0]['mount']['t']-.78)<.011,after['sensors'][0]
    assert after['sensors'][0]['x']!=before['sensors'][0]['x']
    browser.wait('__coverageQA.drawCalls>'+str(before_draw))
    assert browser.metrics()!=before_metrics,(before_metrics,browser.metrics())
    result['positionSliderLiveFrameMotion']={'before':before['sensors'][0],'after':after['sensors'][0],'beforeMetrics':before_metrics,'afterMetrics':browser.metrics()}
    yaw_checks=[]
    for yaw in (0,90,180,-90):
        testid='sensor-camera1-yaw-'+str(yaw)
        button=browser.evaluate('return !!document.querySelector('+json.dumps('[data-testid="'+testid+'"]')+')')
        if button:
            browser.clickid(testid)
        else:
            browser.evaluate('''const button=[...document.querySelectorAll('.sensor-card.is-open button')]
              .find(e=>e.getAttribute('aria-label')===LABEL);if(!button)throw Error('Missing yaw shortcut '+LABEL);button.click();'''
              .replace('LABEL',json.dumps({0:'朝前',90:'朝左',180:'朝后',-90:'朝右'}[yaw])))
        browser.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors[0].yaw==='+str(yaw))
        yaw_checks.append({'yaw':yaw,'metrics':browser.metrics()})
    assert len({tuple(item['metrics']) for item in yaw_checks})>1,yaw_checks
    result['directionShortcutsPersistAndRedraw']=yaw_checks
    browser.button('恢复默认')
    time.sleep(.4)
    assert browser.config()==initial


def verify_layouts(browser,result):
    browser.button('恢复默认')
    browser.sensor('camera1')
    browser.button('三维视角')
    time.sleep(.3)
    defaults=browser.config()
    metric_before=browser.metrics()
    browser.evaluate('''const input=document.querySelector('.slice-slider input');
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'1.05');
      input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));''')
    browser.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).analysis.height===1.05')
    assert browser.evaluate("return document.querySelector('.slice-caption b').textContent")=='1.05'
    assert browser.metrics()!=metric_before
    result['heightSliderChangesCoverage']={'before':metric_before,'at1_05m':browser.metrics()}
    browser.button('恢复默认');time.sleep(.3)
    assert browser.config()==defaults
    browser.clickid('set-baseline');time.sleep(.3)
    browser.range('sensor-camera1-position',.43)
    assert browser.config()!=defaults
    browser.clickid('restore-baseline');time.sleep(.4)
    assert browser.config()==defaults
    result['restoreBaselineAfterRefinedControls']=True
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.evaluate("scrollTo(0,0);document.querySelector('.panel-scroll').scrollTop=0")
    layouts={}
    for name,width,height in [('desktop-1440',1440,960),('desktop-1920',1920,1080),('mobile',390,844)]:
        browser.viewport(width,height)
        browser.evaluate("scrollTo(0,0);document.querySelector('.panel-scroll').scrollTop=0")
        time.sleep(.55)
        layout=browser.layout()
        assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        assert layout['canvas'] and min(layout['canvas'][0].values())>250,layout
        layout['toolbarControls']=browser.evaluate('''const parent=document.querySelector('.visual-toolbar').getBoundingClientRect();
          return [...document.querySelectorAll('.visual-toolbar button,.display-toggles label')].map(e=>{const r=e.getBoundingClientRect();
            return {text:e.textContent.trim(),top:r.top,bottom:r.bottom,left:r.left,right:r.right,
              clipped:r.top<parent.top-1||r.bottom>parent.bottom+1||r.left<parent.left-1||r.right>parent.right+1}});''')
        assert all(not control['clipped'] for control in layout['toolbarControls']),(name,layout['toolbarControls'])
        layout['tileRectangles']=browser.evaluate("return [...document.querySelectorAll('.sensor-select')].map(e=>{const r=e.getBoundingClientRect();return {width:r.width,height:r.height,left:r.left,top:r.top}})")
        assert all(r['height']>=40 and r['width']>=80 for r in layout['tileRectangles']),layout
        layouts[name]=layout
        browser.screenshot('polish-'+name+'.png')
        if name=='mobile':
            window_y=browser.evaluate('return scrollY')
            browser.sensor('camera3')
            browser.wait("(()=>{const p=document.querySelector('.panel-scroll').getBoundingClientRect(),e=document.querySelector('.sensor-card.is-open').getBoundingClientRect();return e.top>=p.top-1&&e.top<=p.top+45})()")
            assert browser.evaluate('return scrollY')==window_y
            result['mobileSelectionScrollsOnlyParameterPanel']=True
            browser.screenshot('polish-mobile-editor.png')
            browser.sensor('camera1')
            browser.wait("(()=>{const p=document.querySelector('.panel-scroll').getBoundingClientRect(),e=document.querySelector('.sensor-card.is-open').getBoundingClientRect();return e.top>=p.top-1&&e.top<=p.top+45})()")
            browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
            time.sleep(.2)
            browser.screenshot('polish-mobile-scene.png')
    result['refinedLayouts']=layouts
    result['errors']=browser.evaluate('return __coverageQA.errors')
    assert not result['errors'],result['errors']


def verify_loaded_assets(browser,result):
    result['sourceDigests']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted((ROOT/'coverage-config/src').rglob('*')) if path.is_file()}
    result['assetDigests']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted((ROOT/'cloud/frontend/coverage').rglob('*')) if path.is_file()}
    loaded=browser.evaluate("return performance.getEntriesByType('resource').map(e=>e.name).filter(n=>n.includes('/coverage-assets/'))")
    expected={url:hashlib.sha256((ROOT/'cloud/frontend/coverage/assets'/url.rsplit('/',1)[1]).read_bytes()).hexdigest() for url in loaded}
    fetched=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];Promise.all(arguments[0].map(async url=>{
      const response=await fetch(url);if(!response.ok)throw Error(response.status);const digest=await crypto.subtle.digest('SHA-256',await response.arrayBuffer());
      return [url,[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('')];})).then(values=>done(Object.fromEntries(values))).catch(error=>done({error:String(error)}));''','args':[loaded]})
    assert fetched==expected,(fetched,expected)
    result['loadedAssets']=loaded
    result['loadedResponseAssetSha256']=fetched


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-polish-qa-20261024'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[]
    browser=None
    result={'scope':'Isolated loopback backend, ephemeral account, real Firefox W3C pointer actions, private Xvfb and Three WebGL; no robot/production/desktop authorization access','pass':False}
    password=secrets.token_urlsafe(24)
    try:
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority'
        authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':98','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb')
        assert xvfb.exists(),xvfb
        log=(ARTIFACTS/'polish-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':98','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],
                                          stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':98',XAUTHORITY=str(authority))
        time.sleep(.5)
        for command,name in [([sys.executable,__file__,'--serve'],'polish-server'),
            (['/snap/bin/geckodriver','--host','127.0.0.1','--port','19149','--profile-root',str(profile)],'polish-geckodriver')]:
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated backend/geckodriver did not start')
        drag.verify_auth(result)
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
        verify_polish(browser,result)
        # Preserve the entire previously passed frame mounting acceptance suite.
        frame.verify_page(browser,result)
        verify_layouts(browser,result)
        verify_loaded_assets(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('polish-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['renderAtFailure']=browser.evaluate('return window.__coverageQA')
                result['anchorsAtFailure']=browser.evaluate("return document.querySelector('.three-host')?.dataset")
                result['configAtFailure']=browser.config()
            except Exception as capture_error:result['captureFailure']=str(capture_error)
        raise
    finally:
        (ARTIFACTS/'browser-coverage-polish-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
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
