"""Focused real Firefox regression: front and depth cameras cannot leave the plate.

Reuses the shared-plate geometry, genuine pointer, export and responsive checks.
Only isolated local fixtures, a private display and an ephemeral account are used.
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
import browser_coverage_shared_plate as shared

ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=Path(os.environ.get('COVERAGE_FIXED_ARTIFACTS','/tmp/plate-fixed-20261024/artifacts'))
SERVER,DRIVER='http://127.0.0.1:19198','http://127.0.0.1:19199'
STORAGE,BASELINE=shared.STORAGE,shared.BASELINE
SLOTS=shared.SLOTS
qa,drag,polish=shared.qa,shared.drag,shared.polish
for module in (shared,shared.qa,shared.drag,shared.frame,shared.polish,shared.photo):
    module.ROOT,module.ARTIFACTS,module.SERVER,module.DRIVER=ROOT,ARTIFACTS,SERVER,DRIVER

original_assert=shared.assert_config


def assert_config(config,shared=True):
    assert config['layoutRevision']==5,config.get('layoutRevision')
    previous=copy.deepcopy(config);previous['layoutRevision']=4
    original_assert(previous,shared=shared)


shared.assert_config=assert_config
Browser=shared.Browser
sensors=shared.sensors
near=shared.near


def serve():
    sys.path.insert(0,str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config,hash_password
    with tempfile.TemporaryDirectory(prefix='plate-fixed-browser-') as directory:
        config=Config(robot_id='plate-fixed-fixture',device_credentials=[{'token':secrets.token_urlsafe(36),
            'robot_id':'plate-fixed-fixture','source_id':'fixture','role':'primary'}],
            viewer_username='coverage-fixture',viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48),public_origin=SERVER,data_dir=directory,
            allow_insecure_local=True,frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config),host='127.0.0.1',port=19198,access_log=None)


def off_plate_fixture(initial,revision=4,lateral=.31):
    fixture=copy.deepcopy(initial);fixture['layoutRevision']=revision
    fixture['chassis']['cameraPlate'].update(lateral=lateral,width=.35,spacing=.19,sensorHeight=.56,thickness=.037)
    pair=sensors(fixture)
    pair['camera2'].update(mount={'rail':'z-front-right','t':.26},yaw=-17.5,pitch=44.3,hfov=97.4,vfov=81.6,enabled=False)
    pair['p100r'].update(mount={'rail':'x-mid-left','t':.67},yaw=22,pitch=11.4,hfov=73.2,vfov=52.6,enabled=True)
    for identifier in SLOTS:
        sensor=pair[identifier];start,end=shared.endpoints(fixture['chassis'],sensor['mount']['rail'])
        for axis in 'xyz':sensor[axis]=start[axis]+(end[axis]-start[axis])*sensor['mount']['t']
    return fixture


def assert_forced(original,canonical):
    assert_config(canonical)
    assert canonical['chassis']['cameraPlate']==original['chassis']['cameraPlate']
    for identifier in SLOTS:
        before,after=sensors(original)[identifier],sensors(canonical)[identifier]
        for key in ('enabled','yaw','pitch','hfov','vfov','minRange','maxRange'):
            assert after[key]==before[key],(identifier,key,before[key],after[key])
        assert after['mount']['rail']==SLOTS[identifier]
        near(after['mount']['t'],canonical['chassis']['cameraPlate']['lateral'])
    shared.unchanged_others(original,canonical)


def verify_fixed_ui(browser,result):
    states=[]
    for identifier,slot in SLOTS.items():
        browser.sensor(identifier)
        state=browser.evaluate("const e=document.querySelector("+json.dumps('[data-testid="sensor-'+identifier+'-rail"]')+");return {disabled:e.disabled,value:e.value,options:[...e.options].map(o=>({value:o.value,text:o.textContent}))}")
        assert state['disabled'] and state['value']==slot and [o['value'] for o in state['options']]==[slot],state
        controls=browser.input_state(identifier)
        assert controls['x']['disabled'] and controls['z']['disabled'] and not controls['y']['disabled'],controls
        assert '固定在共用塑料板' in browser.evaluate("return document.querySelector('.sensor-card.is-open').textContent")
        states.append({'id':identifier,'rail':state,'axes':controls})
    result['bothCameraMountSelectorsAreReadOnlySingleSlotsWithOnlyYLateralEditing']=states


def verify_canonical_paths(browser,initial,result):
    legacy=off_plate_fixture(initial)
    baseline=off_plate_fixture(initial,lateral=.62)
    sensors(baseline)['camera2']['enabled']=True
    sensors(baseline)['p100r']['enabled']=False
    shared.refresh(browser,{'current':legacy,'baseline':{'version':1,'config':baseline}})
    current=browser.config()
    stored=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    assert_forced(legacy,current);assert_forced(baseline,stored)
    verify_fixed_ui(browser,result)
    result['savedRevisionFourProfileMountsAutomaticallyReturnToSlotsWithoutLosingOpticalSettings']={
        'oldPair':{k:sensors(legacy)[k] for k in SLOTS},'canonicalPair':{k:sensors(current)[k] for k in SLOTS},
        'preservedPlate':current['chassis']['cameraPlate']}
    browser.range('plate-lateral',.08)
    browser.clickid('restore-baseline');time.sleep(.35)
    restored=browser.config();assert_forced(baseline,restored)
    assert restored==stored
    result['restoringPreviouslyOffPlateBaselineForcesBothSlotsAndRetainsBaselineOptics']=True
    imports=[]
    for revision in (4,5):
        incoming=off_plate_fixture(initial,revision=revision,lateral=.24 if revision==4 else .74)
        imported=shared.imported(browser,incoming)
        assert_forced(incoming,imported)
        shared.refresh(browser);assert browser.config()==imported
        imports.append({'revision':revision,'pair':{k:sensors(imported)[k] for k in SLOTS},'plate':imported['chassis']['cameraPlate']})
    result['jsonImportAndFollowingRefreshCannotDetachEitherCameraEvenAtCurrentRevision']=imports
    shared.imported(browser,initial)


def verify_each_point_drag(browser,initial,result):
    shared.imported(browser,initial)
    outcomes=[]
    for identifier,sign in (('camera2',1),('p100r',-1)):
        browser.sensor(identifier);browser.clickid('view-mounting');time.sleep(.35)
        before=browser.config();state=shared.board_state(browser)
        assert set(state['gizmoProjections'])=={'Y'},state['gizmoProjections']
        rail=state['railProjections'];dx=rail['end']['x']-rail['start']['x'];dy=rail['end']['y']-rail['start']['y']
        length=(dx*dx+dy*dy)**.5;assert length>10
        start=browser.anchor(sensor=identifier)
        browser.pointer(start,[start[0]+sign*dx/length*90,start[1]+sign*dy/length*90],release=False)
        time.sleep(.25)
        during=browser.config();shared.rigid_move(before,during)
        assert shared.board_state(browser)['dragActive'] is True
        assert shared.board_state(browser)['selectedRail']==SLOTS[identifier]
        browser.release();time.sleep(.25)
        after=browser.config();assert_config(after);shared.assert_mesh(browser,after)
        near(after['chassis']['cameraPlate']['lateral'],1 if sign>0 else 0,.025)
        outcomes.append({'dragged':identifier,'before':{k:sensors(before)[k] for k in SLOTS},'after':{k:sensors(after)[k] for k in SLOTS}})
    result['realPointerOnEitherCameraOnlyMovesSharedYAndPreservesPairHeightAndSpacing']=outcomes
    browser.set_input('plate-thickness',24)
    thickened=browser.config();assert_config(thickened);shared.assert_mesh(browser,thickened)
    for identifier in SLOTS:
        near(sensors(thickened)[identifier]['x']-sensors(after)[identifier]['x'],.016)
        for axis in 'yz':near(sensors(thickened)[identifier][axis],sensors(after)[identifier][axis])
    result['thicknessStillUpdatesActualBoardAndBothOpticalXWithoutChangingYZ']=True
    browser.range('plate-lateral',.64)
    return browser.config()


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-plate-fixed-qa-20261024';profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'scope':'Focused fixed-board regression; real Firefox/Three WebGL, synthetic localStorage and imports, ephemeral local account, private Xvfb; no production or robot access','pass':False}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        downloads=tempfile.TemporaryDirectory(prefix='fixed-download-',dir=profile)
        password=secrets.token_urlsafe(24)
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':93','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'fixed-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':93','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':93',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'fixed-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19199','--profile-root',str(profile)],'fixed-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated browser services did not start')
        drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':downloads.name,'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=Path(downloads.name);browser.all_errors=[]
        result['browser']={key:created['capabilities'].get(key) for key in ('browserName','browserVersion','platformName')}
        seeding="""const fixtureSeed=sessionStorage.getItem('__sharedFixtureSeed');if(fixtureSeed){const seed=JSON.parse(fixtureSeed);localStorage.setItem('observer.sensor-coverage.v1',JSON.stringify(seed.current));localStorage.setItem('observer.sensor-coverage.baseline.v1',JSON.stringify(seed.baseline));sessionStorage.removeItem('__sharedFixtureSeed');}addEventListener('pointerdown',e=>window.__dragPointerId=e.pointerId,true);window.__coverageQA="""
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':qa.PRELOAD.replace('window.__coverageQA=',seeding)})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':"const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));",'args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.evaluate('localStorage.removeItem('+json.dumps(STORAGE)+');localStorage.removeItem('+json.dumps(BASELINE)+');sessionStorage.removeItem("__sharedFixtureSeed");')
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'})
        browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
        time.sleep(.35);initial=browser.config();assert_config(initial);shared.assert_mesh(browser,initial)
        result['webgl']=browser.evaluate('return {contexts:__coverageQA.contexts,drawCalls:__coverageQA.drawCalls}')
        verify_canonical_paths(browser,initial,result)
        current=verify_each_point_drag(browser,initial,result)
        shared.verify_real_drag(browser,current,result)
        shared.verify_export(browser,current,result)
        shared.verify_layouts(browser,current,result)
        browser.viewport(1440,960);browser.sensor('camera2');browser.clickid('view-mounting')
        shared.scroll_panel_to(browser,'sensor-camera2-editor');time.sleep(.35)
        browser.screenshot('fixed-front-camera-readonly-slot.png')
        browser.sensor('p100r');shared.scroll_panel_to(browser,'sensor-p100r-editor');time.sleep(.35)
        browser.screenshot('fixed-depth-camera-readonly-slot.png')
        shared.errors(browser);result['errors']=browser.all_errors
        polish.verify_loaded_assets(browser,result)
        assert result['sourceDigests']==starting,'Runtime source changed during final browser acceptance'
        shared.verify_checked_build(result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('plate-fixed-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['failureConfig']=browser.config();result['failureScene']=shared.board_state(browser)
                result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        for filename in ('browser-plate-fixed-report.json','final-browser-report.json'):
            (ARTIFACTS/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if browser:
            try:browser.call('',method='DELETE')
            except OSError:pass
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()
        if downloads:downloads.cleanup()
        print(json.dumps({'pass':result['pass'],'failure':result.get('failure'),'report':str(ARTIFACTS/'final-browser-report.json')},ensure_ascii=False))


if __name__=='__main__':
    serve() if '--serve' in sys.argv else verify()
