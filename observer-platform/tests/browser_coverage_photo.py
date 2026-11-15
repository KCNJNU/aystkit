"""Firefox acceptance for the photograph-based frame and eight sensor points.

Uses synthetic layouts, ephemeral accounts, loopback-only services and a private
Xvfb. No production account, desktop authority or robot process is accessed.
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
import browser_coverage_polish as polish

ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=Path(os.environ.get('COVERAGE_PHOTO_ARTIFACTS', str(Path.home()/'.local/state/robot-cloud/frame-photo-20261024/artifacts')))
SERVER,DRIVER='http://127.0.0.1:19168','http://127.0.0.1:19169'
STORAGE,BASELINE=qa.STORAGE,drag.BASELINE
IDS=['camera1','camera2','camera3','camera4','mid360','p100r','bluesea','stl19p']
for module in (qa,drag,frame,polish):
    module.ROOT,module.ARTIFACTS,module.SERVER,module.DRIVER=ROOT,ARTIFACTS,SERVER,DRIVER


def serve():
    sys.path.insert(0,str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config,hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-photo-browser-') as directory:
        config=Config(robot_id='coverage-photo-fixture',device_credentials=[{'token':secrets.token_urlsafe(36),
            'robot_id':'coverage-photo-fixture','source_id':'fixture','role':'primary'}],
            viewer_username='coverage-fixture',viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48),public_origin=SERVER,data_dir=directory,
            allow_insecure_local=True,frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config),host='127.0.0.1',port=19168,access_log=None)


class Browser(polish.Browser):
    def wait(self,expression,timeout=15):
        expression=expression.replace("document.querySelectorAll('.sensor-select').length===6",
                                      "document.querySelectorAll('.sensor-select').length===8")
        return super().wait(expression,timeout)

    def set_input(self,testid,value):
        self.evaluate('''const e=document.querySelector(SELECTOR);if(!e)throw Error('Missing input');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,String(VALUE));
          e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));e.blur();'''
          .replace('SELECTOR',json.dumps('[data-testid="'+testid+'"]')).replace('VALUE',json.dumps(value)))
        time.sleep(.4)


def sensors(config):return {sensor['id']:sensor for sensor in config['sensors']}

def endpoints(chassis,rail):
    hx,hy=chassis['frameLength']/2,chassis['frameWidth']/2
    bottom=chassis['height'];top=bottom+chassis['frameHeight'];mid=bottom+chassis['frameMidHeight']
    axis,level,side=rail.split('-')
    if axis=='x':
        y=hy if side=='left' else -hy;z={'bottom':bottom,'mid':mid,'top':top}[level]
        return {'x':-hx,'y':y,'z':z},{'x':hx,'y':y,'z':z}
    if axis=='y':
        x=hx if side=='front' else -hx;z={'bottom':bottom,'mid':mid,'top':top}[level]
        return {'x':x,'y':-hy,'z':z},{'x':x,'y':hy,'z':z}
    x=hx if level=='front' else -hx;y=hy if side=='left' else -hy
    return {'x':x,'y':y,'z':bottom},{'x':x,'y':y,'z':top}


def assert_on_frame(config):
    assert sorted(sensors(config))==sorted(IDS),config['sensors']
    assert config['chassis']['frameLength']>0
    assert 0<config['chassis']['frameMidHeight']<config['chassis']['frameHeight']
    for sensor in config['sensors']:
        mount=sensor['mount'];assert 0<=mount['t']<=1,sensor
        start,end=endpoints(config['chassis'],mount['rail'])
        for axis in 'xyz':
            expected=start[axis]+(end[axis]-start[axis])*mount['t']
            assert abs(sensor[axis]-expected)<2e-6,(sensor,axis,expected)
        if sensor['kind']=='scan':assert sensor['vfov']==0 and sensor.get('verticalOffset',0)==0,sensor


def import_layout(browser,config):
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.import_config({'version':1,'config':config})
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')")
    time.sleep(.4)
    value=browser.config();assert_on_frame(value);return value


def state(browser):
    return browser.evaluate('''const host=document.querySelector('.three-host');return {display:host.dataset.sensorDisplay,
      count:Number(host.dataset.sensorPointCount),rails:Number(host.dataset.frameRailCount),
      points:JSON.parse(host.dataset.sensorProjections||'[]'),gizmo:JSON.parse(host.dataset.gizmoProjections||'{}'),
      selected:host.dataset.selectedRail,dragging:host.dataset.dragActive};''')


def changed(browser,config,**values):
    config=copy.deepcopy(config);config.update(values);return import_layout(browser,config)


def verify_initial_and_migration(browser,result):
    browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    initial=browser.config();assert_on_frame(initial)
    assert sorted(sensors(initial))==sorted(IDS)
    points=state(browser)
    assert points['display']=='points' and points['count']==8 and points['rails']==16,points
    assert len(points['points'])==8 and len({p['id'] for p in points['points']})==8,points
    values=sensors(initial)
    for lower,upper in [('bluesea','mid360'),('stl19p','camera1')]:
        assert values[lower]['z']<values[upper]['z'],(lower,upper,values)
        for axis in 'xy':assert abs(values[lower][axis]-values[upper][axis])<1e-6,(lower,upper,values)
        assert values[lower]['kind']=='scan' and values[lower]['enabled'] is False
    result['eightPointsAndRelativeInstallation']={'points':points,'chassis':initial['chassis'],
        'relations':{key:values[key] for key in ('mid360','bluesea','camera1','stl19p')}}
    for identifier in IDS:
        browser.sensor(identifier)
        browser.wait('document.querySelector('+json.dumps('[data-testid="sensor-'+identifier+'-rail"]')+')')
        browser.wait('''JSON.parse(document.querySelector('.three-host').dataset.sensorProjections).some(s=>s.id===ID)'''.replace('ID',json.dumps(identifier)))
    browser.sensor('bluesea');time.sleep(.2)
    assert state(browser)['gizmo']=={},state(browser)
    result['disabledScannerRemainsVisibleAndNotDraggable']=True
    browser.sensor('camera1');browser.button('三维视角');time.sleep(.3)
    point=browser.anchor(sensor='bluesea')
    browser.pointer(point);time.sleep(.3)
    browser.wait('document.querySelector("[data-testid=sensor-bluesea-rail]")')
    assert browser.config()==initial and state(browser)['gizmo']=={}
    immutable=browser.config();point=browser.anchor(sensor='bluesea')
    browser.pointer(point,[point[0]+28,point[1]+18]);time.sleep(.3)
    assert browser.config()==immutable
    result['disabledPointCanBeSelectedButNotTranslated']=True
    browser.sensor('camera1');browser.button('三维视角');time.sleep(.2);browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
    browser.screenshot('photo-factory-default.png')
    browser.clickid('view-mounting');time.sleep(.4)
    assert browser.config()==initial
    browser.screenshot('photo-factory-mounting.png')
    # Older six-device storage has no new dimensions. Both the current layout and
    # its baseline migrate using the same relative mount coordinates.
    legacy=copy.deepcopy(initial);legacy['sensors']=[s for s in legacy['sensors'] if s['id'] not in ('bluesea','stl19p')]
    legacy['chassis'].pop('frameLength',None);legacy['chassis'].pop('frameMidHeight',None)
    browser.evaluate('localStorage.setItem('+json.dumps(STORAGE)+','+json.dumps(json.dumps(legacy))+');localStorage.setItem('+json.dumps(BASELINE)+','+json.dumps(json.dumps({'version':1,'config':legacy}))+');')
    browser.call('/refresh',{})
    browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    time.sleep(.4)
    current=browser.config();baseline=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    assert_on_frame(current);assert_on_frame(baseline)
    assert current['chassis']['frameLength']==legacy['chassis']['length']
    for original in legacy['sensors']:
        assert sensors(current)[original['id']]['mount']==original['mount']
    assert all(not sensors(current)[identifier]['enabled'] for identifier in ('bluesea','stl19p'))
    for key in ('coverage-delta','blind-delta','overlap-delta'):assert drag.get_number(browser,key)==0,(key,browser.testid(key))
    result['legacySixStorageAndBaselineMigration']={'current':current,'baseline':baseline}
    browser.call('/refresh',{});browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    assert browser.config()==current
    import_layout(browser,initial)
    return initial


def verify_rails_and_motion(browser,initial,result):
    isolated=copy.deepcopy(initial)
    for sensor in isolated['sensors']:sensor['enabled']=sensor['id']=='camera1'
    isolated['analysis'].update(length=4,width=4,resolution=.05,occlusion=False)
    isolated['sensors'][0].update(hfov=70,vfov=70,maxRange=2.5,pitch=40,mount={'rail':'x-mid-left','t':.5})
    isolated=import_layout(browser,isolated);browser.sensor('camera1')
    options=browser.evaluate("return [...document.querySelector('[data-testid=sensor-camera1-rail]').options].map(o=>o.value)")
    assert len(options)==16 and len(set(options))==16,options
    assert {'x-mid-left','x-mid-right','y-mid-front','y-mid-rear'}.issubset(set(options)),options
    for rail in options:
        browser.select_rail(rail);value=browser.config();assert_on_frame(value)
        controls=browser.input_state();start,end=endpoints(value['chassis'],rail)
        for axis,input_value in controls.items():
            assert input_value['disabled']==(axis!=rail[0]),(rail,controls)
            if axis==rail[0]:
                assert abs(input_value['min']-start[axis])<1e-6 and abs(input_value['max']-end[axis])<1e-6,(rail,controls)
    result['sixteenRailOptionsAndAxisLocks']=options
    isolated['sensors'][0]['mount']={'rail':'x-mid-left','t':.5}
    before=import_layout(browser,isolated);browser.sensor('camera1');browser.button('俯视图');time.sleep(.35)
    browser.clickid('set-baseline');time.sleep(.3)
    metrics=browser.metrics();direction=frame.axis_direction(browser,'x');start=browser.anchor()
    end=frame.drag_direction(browser,start,direction,150,release=False)
    during=browser.config();assert_on_frame(during)
    assert during['sensors'][0]['x']!=before['sensors'][0]['x'],(before,during)
    assert during['sensors'][0]['y']==before['sensors'][0]['y'] and during['sensors'][0]['z']==before['sensors'][0]['z']
    browser.release();time.sleep(.3)
    after=browser.config();limit=endpoints(after['chassis'],'x-mid-left')[1]['x']
    assert abs(after['sensors'][0]['x']-limit)<.011,after['sensors'][0]
    assert browser.metrics()!=metrics,(metrics,browser.metrics())
    result['realPointerMiddleRailDragToEndpoint']={'start':start,'end':end,'before':before['sensors'][0],'after':after['sensors'][0]}
    browser.screenshot('photo-middle-rail-endpoint.png')
    # Escape restores the exact member and position and releases the orbit control.
    before=browser.config();start=browser.anchor();frame.drag_direction(browser,start,[-direction[0],-direction[1]],60,release=False)
    browser.call('/actions',{'actions':[{'type':'key','id':'escape-key','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release();time.sleep(.3)
    assert browser.config()==before,(before,browser.config())
    assert state(browser)['dragging']=='false'
    result['escapeRestoresCanonicalMount']=True
    browser.clickid('restore-baseline');time.sleep(.4)
    for key in ('coverage-delta','blind-delta','overlap-delta'):assert drag.get_number(browser,key)==0
    result['restoreMiddleRailBaseline']=True
    import_layout(browser,initial)


def verify_dimensions_and_comparison(browser,initial,result):
    browser.sensor('camera1');browser.clickid('set-baseline');time.sleep(.3)
    before=browser.config()
    browser.evaluate("const e=document.querySelector('[data-testid=chassis-frame-length]').closest('details');e.open=true")
    browser.set_input('chassis-frame-length',1.28)
    browser.set_input('chassis-frame-mid-height',.35)
    value=browser.config();assert_on_frame(value)
    assert abs(value['chassis']['frameLength']-1.28)<1e-6 and abs(value['chassis']['frameMidHeight']-.35)<1e-6,value['chassis']
    for identifier,sensor in sensors(before).items():assert sensors(value)[identifier]['mount']==sensor['mount'],identifier
    for key in ('coverage-delta','blind-delta','overlap-delta'):assert drag.get_number(browser,key)==0,(key,browser.testid(key))
    browser.call('/refresh',{});browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    assert browser.config()==value
    result['dimensionControlsPreserveMountAndFairComparison']={'before':before['chassis'],'after':value['chassis']}
    browser.clickid('restore-baseline');time.sleep(.4)
    assert browser.config()==before
    result['dimensionRestoreAndRefreshPersistence']=True
    import_layout(browser,initial)


def verify_scan_planes(browser,initial,result):
    cases=[]
    for identifier in ('bluesea','stl19p'):
        config=copy.deepcopy(initial)
        for sensor in config['sensors']:sensor['enabled']=sensor['id']==identifier
        scan=sensors(config)[identifier];scan.update(pitch=0,hfov=360,maxRange=2)
        config['analysis'].update(height=scan['z'],length=4,width=4,resolution=.05,occlusion=False)
        value=import_layout(browser,config);browser.sensor(identifier);same=browser.metrics()
        assert same[0]>0 and sensors(value)[identifier]['vfov']==0,(identifier,same,value)
        config=copy.deepcopy(value);config['analysis']['height']+=.05
        import_layout(browser,config);different=browser.metrics();assert different[0]==0,(identifier,different)
        config=copy.deepcopy(value);sensors(config)[identifier]['pitch']=15
        import_layout(browser,config);tilted=browser.metrics();assert tilted[0]==0,(identifier,tilted)
        config=copy.deepcopy(value);sensors(config)[identifier]['enabled']=False
        import_layout(browser,config);disabled=browser.metrics();assert disabled[0]==0
        visible=state(browser);assert identifier in {p['id'] for p in visible['points']} and visible['count']==8
        assert visible['gizmo']=={},visible
        cases.append({'id':identifier,'sameHeight':same,'differentHeight':different,'tilted':tilted,'disabled':disabled})
    result['twoDimensionalScanPlanesNeverBecomeVolumes']=cases
    import_layout(browser,initial)


def verify_export_and_layouts(browser,initial,result):
    browser.button('恢复默认');time.sleep(.4)
    assert browser.config()==initial
    button=browser.evaluate('''const e=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='导出配置');
      if(!e)throw Error('Export button missing');const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];''')
    browser.pointer(button)
    until=time.monotonic()+15;download=None
    while time.monotonic()<until:
        files=list(browser.download_dir.glob('*.json'))
        if files:
            download=files[0]
            try:
                exported=json.loads(download.read_text())
                break
            except (OSError,ValueError):pass
        time.sleep(.15)
    else:raise AssertionError('Actual JSON download did not complete')
    assert exported['config']==browser.config(),exported
    result['actualExportDownload']={'name':download.name,'bytes':download.stat().st_size,'sensorCount':len(exported['config']['sensors'])}
    result['exportContainsEightCanonicalSensorsAndFrameDimensions']=True
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    layouts={}
    for name,width,height in [('desktop-1440',1440,960),('desktop-1920',1920,1080),('mobile',390,844)]:
        browser.viewport(width,height);browser.evaluate("scrollTo(0,0);document.querySelector('.panel-scroll').scrollTop=0")
        browser.sensor('camera1');browser.button('三维视角');browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
        time.sleep(.45)
        layout=browser.layout();assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        assert browser.evaluate('return document.querySelectorAll(".sensor-select").length')==8
        assert state(browser)['display']=='points' and state(browser)['count']==8
        layouts[name]=layout;browser.screenshot('photo-'+name+'.png')
        browser.clickid('view-mounting');time.sleep(.3)
        if name=='mobile':browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})");time.sleep(.2)
        browser.screenshot('photo-'+name+'-mounting.png')
    result['layouts']=layouts
    result['errors']=browser.evaluate('return __coverageQA.errors');assert not result['errors'],result['errors']



def verify_summary_responsiveness(browser,result):
    browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    browser.sensor('camera1');browser.viewport(1440,960);browser.clickid('view-mounting');time.sleep(.4)
    desktop=browser.evaluate("const e=document.querySelector('.selected-sensor-summary'),r=e.getBoundingClientRect();return {height:r.height,width:r.width,detailsVisible:getComputedStyle(e.querySelector('dl')).display!=='none',values:e.querySelectorAll('dd').length}")
    assert desktop['detailsVisible'] and desktop['values']==4 and desktop['height']>130,desktop
    browser.screenshot('photo-post-desktop-full-summary.png')
    browser.viewport(390,844);browser.clickid('view-mounting')
    browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
    browser.clickid('view-mounting');time.sleep(.4)
    mobile=browser.evaluate("""const e=document.querySelector('.selected-sensor-summary'),r=e.getBoundingClientRect(),host=document.querySelector('.three-host');
      return {height:r.height,width:r.width,left:r.left,right:r.right,top:r.top,bottom:r.bottom,detailsVisible:getComputedStyle(e.querySelector('dl')).display!=='none',
        points:JSON.parse(host.dataset.sensorProjections).filter(p=>p.id==='mid360'||p.id==='camera1'),title:e.querySelector('.summary-title strong').textContent};""")
    assert not mobile['detailsVisible'] and 30<mobile['height']<95 and mobile['width']<=140,mobile
    for point in mobile['points']:
        assert 0<=point['x']<=390 and 0<=point['y']<=844,(point,mobile)
        assert not (mobile['left']-4<point['x']<mobile['right']+4 and mobile['top']-4<point['y']<mobile['bottom']+15),(point,mobile)
    layout=browser.layout();assert layout['scrollWidth']<=392 and not layout['overflow'],layout
    browser.screenshot('photo-post-mobile-compact-summary.png')
    errors=browser.evaluate('return __coverageQA.errors');assert not errors,errors
    result['mobileSummaryPostAcceptance']={'scope':'CSS-only compact selected-device summary; previous full feature acceptance retained',
        'desktop':desktop,'mobile':mobile,'mobileLayout':layout,'errors':errors,'pass':True}
    result['errors']=errors


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-photo-qa-20261024';profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;download_session=None
    post_css='--post-css' in sys.argv
    previous=json.loads((ARTIFACTS/'final-browser-report.json').read_text()) if post_css else None
    if previous is not None:assert previous['pass'] is True and not previous['errors']
    result={'scope':'Isolated Firefox/Three WebGL, synthetic configurations, ephemeral account and private display; no robot or production credential','pass':False}
    try:
        download_session=tempfile.TemporaryDirectory(prefix='photo-download-',dir=profile)
        download_dir=Path(download_session.name)
        password=secrets.token_urlsafe(24);env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':96','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'photo-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':96','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':96',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in [([sys.executable,__file__,'--serve'],'photo-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19169','--profile-root',str(profile)],'photo-geckodriver')]:
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log);processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated services did not start')
        drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':str(download_dir),'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=download_dir
        preload=qa.PRELOAD.replace('window.__coverageQA=',"addEventListener('pointerdown',e=>window.__dragPointerId=e.pointerId,true);window.__coverageQA=")
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':preload})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));''','args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'})
        if post_css:
            result=previous
            result['fullAcceptanceBeforeMobileSummaryCSS']=copy.deepcopy(previous.get('fullAcceptanceBeforeMobileSummaryCSS')) or {key:copy.deepcopy(previous[key]) for key in ('sourceDigests','assetDigests','loadedAssets','loadedResponseAssetSha256','layouts')}
            result['fullAcceptanceBeforeMobileSummaryCSS']['pass']=True
            result['pass']=False
        else:
            initial=verify_initial_and_migration(browser,result)
            verify_rails_and_motion(browser,initial,result)
            verify_dimensions_and_comparison(browser,initial,result)
            verify_scan_planes(browser,initial,result)
            verify_export_and_layouts(browser,initial,result)
        verify_summary_responsiveness(browser,result)
        polish.verify_loaded_assets(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('photo-failure.png');result['failureText']=browser.evaluate('return document.body.innerText')
                result['renderAtFailure']=browser.evaluate('return window.__coverageQA');result['anchorsAtFailure']=state(browser);result['configAtFailure']=browser.config()
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        for filename in ('browser-coverage-photo-report.json','final-browser-report.json'):(ARTIFACTS/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if browser:
            try:browser.call('',method='DELETE')
            except OSError:pass
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()
        if download_session:download_session.cleanup()
        print(json.dumps({'pass':result['pass'],'failure':result.get('failure'),'report':str(ARTIFACTS/'final-browser-report.json')},ensure_ascii=False))


if __name__=='__main__':serve() if '--serve' in sys.argv else verify()
