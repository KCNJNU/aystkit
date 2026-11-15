"""Real Firefox acceptance for frame-constrained sensor mounting.

An isolated loopback backend, ephemeral account and synthetic geometry are used.
No production credential, remote robot or robot control is accessed.
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

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('COVERAGE_FRAME_ARTIFACTS', str(Path.home()/'.local/state/robot-cloud/frame-mount-20261024/artifacts')))
SERVER, DRIVER = 'http://127.0.0.1:19138', 'http://127.0.0.1:19139'
STORAGE, BASELINE = qa.STORAGE, drag.BASELINE
qa.ROOT, qa.ARTIFACTS, qa.SERVER, qa.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER
drag.ROOT, drag.ARTIFACTS, drag.SERVER, drag.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-frame-browser-') as directory:
        config = Config(robot_id='coverage-frame-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'coverage-frame-fixture',
            'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT / 'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19138, access_log=None)


class Browser(drag.Browser):
    def select_rail(self, rail, sensor='camera1'):
        self.sensor(sensor)
        self.evaluate('''const select=document.querySelector(SELECTOR);if(!select)throw Error('No mount rail selector');
          select.value=RAIL;select.dispatchEvent(new Event('change',{bubbles:true}));'''
          .replace('SELECTOR', json.dumps(f'[data-testid="sensor-{sensor}-rail"]')).replace('RAIL', json.dumps(rail)))
        self.wait('JSON.parse(localStorage.getItem('+json.dumps(STORAGE)+')).sensors.find(s=>s.id==='+json.dumps(sensor)+').mount.rail==='+json.dumps(rail))

    def input_state(self, sensor='camera1'):
        return self.evaluate('''return Object.fromEntries(['x','y','z'].map(axis=>{const e=document.querySelector('[data-testid="sensor-'+SENSOR+'-'+axis+'"]');
          return [axis,{disabled:e.disabled,value:Number(e.value),min:Number(e.min),max:Number(e.max)}]}));'''.replace('SENSOR',json.dumps(sensor)))


def near(value, target, tolerance=1e-6):
    return abs(value-target) <= tolerance


def rail_endpoints(chassis, rail):
    hx, hy = chassis['length']*.65/2, chassis['frameWidth']/2
    z0, z1 = chassis['height'], chassis['height']+chassis['frameHeight']
    axis, level, side = rail.split('-')
    if axis == 'x':
        y, z = hy if side=='left' else -hy, z1 if level=='top' else z0
        return {'x':-hx,'y':y,'z':z}, {'x':hx,'y':y,'z':z}
    if axis == 'y':
        x, z = hx if side=='front' else -hx, z1 if level=='top' else z0
        return {'x':x,'y':-hy,'z':z}, {'x':x,'y':hy,'z':z}
    x, y = hx if level=='front' else -hx, hy if side=='left' else -hy
    return {'x':x,'y':y,'z':z0}, {'x':x,'y':y,'z':z1}


def assert_on_frame(config):
    for sensor in config['sensors']:
        mount=sensor.get('mount')
        assert isinstance(mount,dict) and 0<=mount.get('t',-1)<=1, sensor
        a,b=rail_endpoints(config['chassis'],mount['rail'])
        for axis in 'xyz':
            expected=a[axis]+(b[axis]-a[axis])*mount['t']
            assert near(sensor[axis],expected,2e-6),(sensor,axis,expected)


def import_wait(browser, config):
    browser.import_config({'version':1,'config':config})
    browser.wait("document.querySelector('[role=status]')?.textContent.includes('已导入')")
    time.sleep(.45)
    value=browser.config()
    assert_on_frame(value)
    return value


def mount_at(browser, config, rail, t):
    config=copy.deepcopy(config)
    config['sensors'][0]['mount']={'rail':rail,'t':t}
    value=import_wait(browser,config)
    assert value['sensors'][0]['mount']=={'rail':rail,'t':t},value['sensors'][0]
    browser.sensor('camera1')
    return value


def axis_direction(browser,axis):
    sensor=browser.anchor()
    handle=browser.anchor('axis',axis=axis.upper())
    dx,dy=handle[0]-sensor[0],handle[1]-sensor[1]
    norm=(dx*dx+dy*dy)**.5
    assert norm>5,(axis,sensor,handle)
    return [dx/norm,dy/norm]


def drag_direction(browser,start,direction,distance=130,release=True):
    rect=browser.evaluate("const r=document.querySelector('canvas').getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}")
    end=[min(rect['right']-20,max(rect['left']+20,start[0]+direction[0]*distance)),
         min(rect['bottom']-20,max(rect['top']+20,start[1]+direction[1]*distance))]
    browser.pointer(start,end,release=release)
    time.sleep(.35)
    return end


def verify_page(browser,result):
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    browser.wait("document.querySelector('[data-testid=sensor-camera1-rail]')")
    result['initialRender']=browser.evaluate('return __coverageQA')
    assert not result['initialRender']['errors']
    result['sourceSha256']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((ROOT/'coverage-config/src').glob('*')) if path.is_file()}
    result['buildSha256']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted((ROOT/'cloud/frontend/coverage').rglob('*')) if path.is_file()}
    result['sourceDigests']=result['sourceSha256']
    result['assetDigests']=result['buildSha256']
    result['loadedAssets']=browser.evaluate("return performance.getEntriesByType('resource').map(e=>e.name).filter(n=>n.includes('/coverage-assets/'))")
    initial=browser.config()
    assert_on_frame(initial)

    # Old saved XYZ configurations and their comparison baseline must migrate.
    legacy=copy.deepcopy(initial)
    for index,sensor in enumerate(legacy['sensors']):
        sensor.pop('mount',None)
        sensor.update(x=2.4-index*.8,y=1.8-index*.6,z=3-index*.45)
    browser.evaluate('localStorage.setItem('+json.dumps(STORAGE)+','+json.dumps(json.dumps(legacy))+');localStorage.setItem('+json.dumps(BASELINE)+','+json.dumps(json.dumps({'version':1,'config':legacy}))+');')
    browser.call('/refresh',{})
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    time.sleep(.4)
    migrated=browser.config()
    migrated_baseline=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    assert_on_frame(migrated)
    assert_on_frame(migrated_baseline)
    result['legacyCurrentAndBaselineMigration']={'before':legacy['sensors'],'current':migrated['sensors'],'baseline':migrated_baseline['sensors']}

    isolated=copy.deepcopy(initial)
    for sensor in isolated['sensors']:sensor['enabled']=sensor['id']=='camera1'
    isolated['sensors'][0].update(hfov=70,vfov=70,maxRange=2.5,pitch=40)
    isolated['analysis'].update(length=4,width=4,resolution=.05)
    isolated=mount_at(browser,isolated,'x-top-left',.5)
    browser.select_rail('x-top-left')
    options=browser.evaluate("return [...document.querySelector('[data-testid=sensor-camera1-rail]').options].map(o=>({id:o.value,label:o.textContent}))")
    assert len(options)==12,options
    result['railOptions']=options
    # Each selected rod exposes just its own axis, with the physical endpoints.
    checks=[]
    for option in options:
        rail=option['id'];browser.select_rail(rail)
        states=browser.input_state()
        a,b=rail_endpoints(browser.config()['chassis'],rail)
        for axis,state in states.items():
            assert state['disabled']==(axis!=rail[0]),(rail,states)
            if axis==rail[0]:
                assert near(state['min'],min(a[axis],b[axis])) and near(state['max'],max(a[axis],b[axis])),(rail,state,a,b)
        assert_on_frame(browser.config())
        checks.append({'rail':rail,'inputs':states})
    result['allTwelveRailsAxisAndInputLimits']=checks

    before=mount_at(browser,isolated,'x-top-left',.5)
    browser.button('俯视图');time.sleep(.4)
    browser.clickid('set-baseline');time.sleep(.25)
    baseline=browser.metrics()
    browser.screenshot('frame-baseline-top.png')
    direction=axis_direction(browser,'x')
    start=browser.anchor()
    end=drag_direction(browser,start,direction,release=False)
    moved=browser.config()
    assert_on_frame(moved)
    assert moved['sensors'][0]['x']!=before['sensors'][0]['x'],(before['sensors'][0],moved['sensors'][0])
    for axis in 'yz':assert near(moved['sensors'][0][axis],before['sensors'][0][axis])
    browser.release();time.sleep(.3)
    maxx=rail_endpoints(moved['chassis'],'x-top-left')[1]['x']
    assert near(moved['sensors'][0]['x'],maxx,.011),moved['sensors'][0]
    result['realPointerHorizontalRodToEndpoint']={'start':start,'end':end,'before':before['sensors'][0],'after':moved['sensors'][0]}
    result['comparisonAfterHorizontal']={}
    drag.assert_comparison(browser,baseline,result['comparisonAfterHorizontal'])
    browser.screenshot('frame-horizontal-endpoint.png')
    browser.call('/refresh',{})
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    assert browser.config()==moved
    result['refreshPersistsRailAndPosition']=True

    # Opposite movement reaches the opposite endpoint without leaving its rail.
    browser.sensor('camera1');browser.button('俯视图');time.sleep(.3)
    start=browser.anchor()
    drag_direction(browser,start,[-direction[0],-direction[1]],180)
    low=browser.config()
    assert_on_frame(low)
    minx=rail_endpoints(low['chassis'],'x-top-left')[0]['x']
    assert near(low['sensors'][0]['x'],minx,.011),low['sensors'][0]
    result['horizontalBothEndpointsClamped']=True

    # A column allows height adjustment, using a real visible transform handle.
    vertical=mount_at(browser,isolated,'z-rear-left',.5)
    browser.button('三维视角');time.sleep(.4)
    direction=axis_direction(browser,'z')
    start=browser.anchor('axis',axis='Z')
    end=drag_direction(browser,start,direction,170,release=False)
    during=browser.config()
    assert_on_frame(during)
    assert during['sensors'][0]['z']!=vertical['sensors'][0]['z'],(vertical['sensors'][0],during['sensors'][0])
    for axis in 'xy':assert near(during['sensors'][0][axis],vertical['sensors'][0][axis])
    browser.release();time.sleep(.3)
    assert near(during['sensors'][0]['z'],vertical['chassis']['height']+vertical['chassis']['frameHeight'],.011),during['sensors'][0]
    result['realPointerVerticalColumnToTop']={'handle':start,'end':end,'before':vertical['sensors'][0],'during':during['sensors'][0]}
    assert browser.metrics()!=baseline,(browser.metrics(),baseline)
    result['mountMovementChangesCoverage']={'baseline':baseline,'afterColumnMove':browser.metrics()}
    browser.screenshot('frame-column-top.png')
    direction=axis_direction(browser,'z')
    start=browser.anchor()
    end=drag_direction(browser,start,[-direction[0],-direction[1]],170)
    bottom=browser.config();assert_on_frame(bottom)
    assert near(bottom['sensors'][0]['z'],vertical['chassis']['height'],.011),bottom['sensors'][0]
    result['realPointerColumnMarkerToBottom']={'marker':start,'end':end,'after':bottom['sensors'][0]}

    # Escape restores the exact mount and coordinates from pointer down.
    cancellable=mount_at(browser,isolated,'z-rear-left',.5)
    browser.button('三维视角');time.sleep(.3)
    direction=axis_direction(browser,'z');start=browser.anchor('axis',axis='Z')
    drag_direction(browser,start,direction,40,release=False)
    assert browser.config()!=cancellable
    browser.call('/actions',{'actions':[{'type':'key','id':'cancel-keyboard','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release();time.sleep(.35)
    assert browser.config()==cancellable,(browser.config(),cancellable)
    result['escapeRestoresExactMount']=True

    # Typed edits clamp to the current rod; the other fields cannot be edited.
    browser.select_rail('y-bottom-front')
    states=browser.input_state()
    assert states['x']['disabled'] and states['z']['disabled'] and not states['y']['disabled']
    browser.field('Y · 左右',99)
    time.sleep(.3)
    typed=browser.config();assert_on_frame(typed)
    assert near(typed['sensors'][0]['y'],typed['chassis']['frameWidth']/2)
    assert near(browser.input_state()['y']['value'],typed['sensors'][0]['y'])
    result['railSwitchAndNumericEndpointClamp']=typed['sensors'][0]

    # Both valid canonical mounts and invalid/legacy coordinates are safe to import.
    illegal=copy.deepcopy(isolated)
    for i,sensor in enumerate(illegal['sensors']):
        sensor.update(x=99,y=-99,z=-99)
        sensor['mount']={'rail':'not-a-rail','t':999} if i%2 else {'rail':'z-front-right','t':999}
    imported=import_wait(browser,illegal)
    result['illegalImportCannotLeaveFrame']=imported['sensors']
    canonical=copy.deepcopy(isolated)
    for sensor in canonical['sensors']:
        sensor.update(x=99,y=-99,z=-99,mount={'rail':'x-top-right','t':.37})
    normalized=import_wait(browser,canonical)
    for sensor in normalized['sensors']:
        assert sensor['mount']=={'rail':'x-top-right','t':.37},sensor
    result['canonicalImportIgnoresConflictingXYZ']=True

    # Resizing uses normalized positions along rails for current AND baseline.
    original=mount_at(browser,isolated,'x-top-left',.21)
    browser.clickid('set-baseline');time.sleep(.25)
    reference=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    current=mount_at(browser,original,'z-rear-right',.72)
    browser.evaluate("[...document.querySelectorAll('details')].find(e=>e.textContent.includes('底盘与铝合金架')).open=true")
    for label,value in [('底盘长度',1.6),('底盘高度',.35),('框架高度',1.3),('框架宽度',.8)]:
        browser.field(label,value,'.additional-settings')
    time.sleep(.4)
    resized=browser.config();assert_on_frame(resized)
    assert resized['sensors'][0]['mount']==current['sensors'][0]['mount']
    for key in ['length','height','frameHeight','frameWidth']:
        assert resized['chassis'][key]!=original['chassis'][key]
    fair_reference=copy.deepcopy(reference)
    fair_reference['analysis']=copy.deepcopy(resized['analysis'])
    fair_reference['chassis']=copy.deepcopy(resized['chassis'])
    import_wait(browser,fair_reference);fair_metrics=browser.metrics()
    import_wait(browser,resized)
    result['resizePreservesRailAndNormalizedPosition']=resized['sensors'][0]
    result['comparisonUsesCurrentFrameDimensions']={}
    drag.assert_comparison(browser,fair_metrics,result['comparisonUsesCurrentFrameDimensions'])
    baseline_after=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    assert_on_frame(baseline_after)
    browser.screenshot('frame-resized-comparison.png')

    # Hidden edit mode, regular orbit and export remain usable.
    browser.sensor('camera1')
    browser.button('三维视角');time.sleep(.2)
    browser.clickid('drag-toggle')
    immutable=browser.config();start=browser.anchor()
    browser.pointer(start,[start[0]+45,start[1]+25]);time.sleep(.3)
    assert browser.config()==immutable
    browser.clickid('drag-toggle')
    browser.evaluate('''const create=URL.createObjectURL.bind(URL);URL.createObjectURL=blob=>{blob.text().then(t=>window.__coverageExport=JSON.parse(t));return create(blob)};
      const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){if(this.download)return;return click.call(this)};''')
    browser.button('导出配置');browser.wait('window.__coverageExport?.version===1')
    assert browser.evaluate('return __coverageExport.config')==browser.config()
    result['exportIncludesCanonicalMounts']=True
    browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
    result['layouts']={'desktop':browser.layout()}
    browser.screenshot('frame-desktop-final.png')
    browser.viewport(390,844);time.sleep(.5)
    result['layouts']['mobile']=browser.layout()
    browser.screenshot('frame-mobile.png')
    browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
    time.sleep(.2)
    browser.screenshot('frame-mobile-scene.png')
    for size,layout in result['layouts'].items():
        assert layout['scrollWidth']<=layout['width']+2 and not layout['overflow'],(size,layout)
    result['errors']=browser.evaluate('return __coverageQA.errors')
    assert not result['errors'],result['errors']


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-frame-qa-20261024'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None
    result={'scope':'Isolated loopback backend, ephemeral account, real Firefox W3C pointer actions and Three WebGL; no robot/production access','pass':False}
    password=secrets.token_urlsafe(24)
    try:
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        xvfb=ARTIFACTS/'xvfb-runtime/usr/bin/Xvfb'
        if xvfb.exists():
            authority=profile/'Xauthority'
            authority.touch(mode=0o600,exist_ok=True)
            subprocess.run(['xauth','-f',str(authority),'add',':97','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],
                           check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            log=(ARTIFACTS/'frame-xvfb.log').open('w');logs.append(log)
            processes.append(subprocess.Popen([str(xvfb),':97','-screen','0','1600x1000x24','-nolisten','tcp','-auth',str(authority)],
                                              stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
            env.update(DISPLAY=':97',XAUTHORITY=str(authority))
            time.sleep(.5)
        authority=Path('/run/user/1000/gdm/Xauthority')
        if not env.get('DISPLAY') and Path('/tmp/.X11-unix/X1').exists() and authority.exists():
            env.update(DISPLAY=':1',XAUTHORITY=str(authority))
        elif not env.get('DISPLAY') and Path('/tmp/.X11-unix/X0').exists() and (Path.home()/'.Xauthority').exists():
            env.update(DISPLAY=':0',XAUTHORITY=str(Path.home()/'.Xauthority'))
        for command,name in [([sys.executable,__file__,'--serve'],'frame-server'),
            (['/snap/bin/geckodriver','--host','127.0.0.1','--port','19139','--profile-root',str(profile)],'frame-geckodriver')]:
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
        verify_page(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('frame-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['renderAtFailure']=browser.evaluate('return window.__coverageQA')
                result['anchorsAtFailure']=browser.evaluate("return document.querySelector('.three-host')?.dataset")
                result['configAtFailure']=browser.config()
            except Exception as capture_error:result['captureFailure']=str(capture_error)
        raise
    finally:
        (ARTIFACTS/'browser-coverage-frame-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
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
