"""Isolated Firefox acceptance for independently adjustable middle frame faces.

Synthetic configurations, ephemeral accounts and a private Xvfb only. No robot,
production account, real camera or desktop authority is accessed.
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
import browser_coverage_photo as photo

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('COVERAGE_MIDDLE_ARTIFACTS', str(Path.home()/'.local/state/robot-cloud/middle-faces-20261024/artifacts')))
SERVER, DRIVER = 'http://127.0.0.1:19178', 'http://127.0.0.1:19179'
STORAGE, BASELINE = qa.STORAGE, drag.BASELINE
SIDES = ('front', 'rear', 'left', 'right')
MID_RAILS = {'front': 'y-mid-front', 'rear': 'y-mid-rear', 'left': 'x-mid-left', 'right': 'x-mid-right'}
ATTACHED = {'front': 'camera4', 'rear': 'stl19p', 'left': 'camera1', 'right': 'mid360'}
PLATE_RAIL = 'plate-front-right'
for module in (qa, drag, frame, polish, photo):
    module.ROOT, module.ARTIFACTS, module.SERVER, module.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='middle-face-browser-') as directory:
        config = Config(robot_id='middle-face-fixture', device_credentials=[{'token': secrets.token_urlsafe(36),
            'robot_id': 'middle-face-fixture', 'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19178, access_log=None)


Browser = photo.Browser
sensors = photo.sensors


def endpoints(chassis, rail):
    hx, hy = chassis['frameLength']/2, chassis['frameWidth']/2
    bottom, top = chassis['height'], chassis['height']+chassis['frameHeight']
    if rail == PLATE_RAIL:
        y = -.32*chassis['frameWidth']
        front=(chassis.get('frameMidHeights') or {}).get('front',chassis.get('frameMidHeight',chassis['frameHeight']/2))
        return {'x':hx,'y':y,'z':bottom+front}, {'x':hx,'y':y,'z':top}
    axis, level, side = rail.split('-')
    if level == 'mid':
        z = bottom + (chassis.get('frameMidHeights') or {}).get(side,chassis.get('frameMidHeight',chassis['frameHeight']/2))
    else:
        z = top if level == 'top' else bottom
    if axis == 'x':
        y = hy if side == 'left' else -hy
        return {'x': -hx, 'y': y, 'z': z}, {'x': hx, 'y': y, 'z': z}
    if axis == 'y':
        x = hx if side == 'front' else -hx
        return {'x': x, 'y': -hy, 'z': z}, {'x': x, 'y': hy, 'z': z}
    x = hx if level == 'front' else -hx
    y = hy if side == 'left' else -hy
    return {'x': x, 'y': y, 'z': bottom}, {'x': x, 'y': y, 'z': top}


def assert_on_frame(config):
    assert len(config['sensors']) == 8
    heights = config['chassis']['frameMidHeights']
    assert set(heights) == set(SIDES), heights
    for side in SIDES:
        assert .05-1e-9 <= heights[side] <= config['chassis']['frameHeight']-.05+1e-9, (side, heights)
    for sensor in config['sensors']:
        mount = sensor['mount']; start, end = endpoints(config['chassis'], mount['rail'])
        assert 0 <= mount['t'] <= 1
        for axis in 'xyz':
            expected = start[axis]+(end[axis]-start[axis])*mount['t']
            assert abs(sensor[axis]-expected) < 2e-6, (sensor, axis, expected)


def import_layout(browser, config):
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.import_config({'version': 1, 'config': config})
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')")
    time.sleep(.4)
    result = browser.config(); assert_on_frame(result)
    return result


def zero_deltas(browser):
    for key in ('coverage-delta', 'blind-delta', 'overlap-delta'):
        assert drag.get_number(browser, key) == 0, (key, browser.testid(key))


def open_frame_settings(browser):
    browser.evaluate("const e=document.querySelector('[data-testid=chassis-mid-front]');if(!e)throw Error('No independent mid controls');e.closest('details').open=true")


def verify_legacy(browser, initial, result):
    outcomes = []
    for count, scalar, custom in ((6, .32, False), (8, .37, False), (8, .39, True)):
        legacy = copy.deepcopy(initial)
        legacy['sensors'] = legacy['sensors'][:count]
        legacy.pop('layoutRevision', None)
        legacy['chassis'].pop('frameMidHeights', None)
        legacy['chassis']['frameMidHeight'] = scalar
        sensors(legacy)['camera2']['mount'] = {'rail':'z-front-right','t':.63 if custom else (.9-.25)/.9}
        if 'bluesea' in sensors(legacy):
            sensors(legacy)['bluesea']['mount'] = {'rail':'y-mid-front','t':.27 if custom else .5}
        for sensor in legacy['sensors']:
            start,end=endpoints(legacy['chassis'],sensor['mount']['rail'])
            for axis in 'xyz':sensor[axis]=start[axis]+(end[axis]-start[axis])*sensor['mount']['t']
        # The app correctly flushes its current layout at pagehide. Seed the
        # following document before its app module executes, rather than having
        # the old document overwrite this migration fixture during refresh.
        seed={'current':legacy,'baseline':{'version':1,'config':legacy}}
        browser.evaluate('sessionStorage.setItem("__middleFixtureSeed",'+json.dumps(json.dumps(seed))+');')
        browser.call('/refresh', {})
        browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
        time.sleep(.4)
        value = browser.config()
        baseline = browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
        assert_on_frame(value); assert_on_frame(baseline)
        assert value['chassis']['frameMidHeights'] == dict.fromkeys(SIDES, scalar), value['chassis']
        assert baseline['chassis']['frameMidHeights'] == dict.fromkeys(SIDES, scalar), baseline['chassis']
        assert value['layoutRevision'] == 3 and baseline['layoutRevision'] == 3
        for sensor in legacy['sensors']:
            migrated=sensors(value)[sensor['id']]['mount']
            if sensor['id']=='camera2' and not custom:
                start,end=endpoints(value['chassis'],PLATE_RAIL)
                expected_t=max(0,min(1,(sensor['z']-start['z'])/(end['z']-start['z'])))
                assert migrated['rail']==PLATE_RAIL and abs(migrated['t']-expected_t)<1e-8,migrated
                assert abs(sensors(value)['camera2']['z']-sensor['z'])<1e-8
            elif sensor['id']=='bluesea' and not custom:
                assert migrated=={'rail':'y-bottom-front','t':.5},migrated
            else:
                assert migrated == sensor['mount'],(sensor['id'],migrated,sensor['mount'])
        zero_deltas(browser)
        outcomes.append({'storedDeviceCount':count,'scalar':scalar,'customMounts':custom,
            'migrated':value['chassis']['frameMidHeights'], 'camera2':sensors(value)['camera2']['mount'],
            'bluesea':sensors(value)['bluesea']['mount']})
    result['legacySingleHeightCurrentAndBaselineMigration'] = outcomes
    # An explicit choice made after migration must not be forced again.
    import_layout(browser,initial)
    browser.select_rail('z-front-right',sensor='camera2')
    browser.select_rail('y-mid-front',sensor='bluesea')
    restored=browser.config()
    assert restored['layoutRevision']==3
    assert sensors(restored)['camera2']['mount']['rail']=='z-front-right'
    assert abs(sensors(restored)['camera2']['mount']['t']-(.9-.25)/.9)<1e-8
    assert sensors(restored)['bluesea']['mount']=={'rail':'y-mid-front','t':.5}
    result['revisionThreeExplicitMountChoicesAreNotRemigrated']=True
    import_layout(browser, initial)


def verify_faces(browser, initial, result):
    config = copy.deepcopy(initial)
    config['chassis']['frameMidHeights'] = dict.fromkeys(SIDES, .45)
    config['analysis'].update(length=4, width=4, resolution=.05, occlusion=False)
    for sensor in config['sensors']:
        sensor['enabled'] = sensor['id'] in ('camera1', 'camera2')
    for side, identifier in ATTACHED.items():
        sensors(config)[identifier]['mount'] = {'rail': MID_RAILS[side], 't': .5}
    value = import_layout(browser, config)
    browser.sensor('camera1'); browser.clickid('set-baseline'); time.sleep(.3)
    open_frame_settings(browser)
    outcomes = []
    values = {'front': .22, 'rear': .43, 'left': .34, 'right': .60}
    for side in SIDES:
        before = browser.config()
        browser.set_input('chassis-mid-'+side, values[side])
        after = browser.config(); assert_on_frame(after)
        expected = dict(before['chassis']['frameMidHeights']); expected[side] = values[side]
        assert after['chassis']['frameMidHeights'] == expected, (side, after['chassis'])
        for identifier, original in sensors(before).items():
            updated = sensors(after)[identifier]
            assert updated['mount'] == original['mount']
            assert updated['x'] == original['x'] and updated['y'] == original['y']
            if original['mount']['rail'] == MID_RAILS[side]:
                assert abs(updated['z']-(after['chassis']['height']+values[side])) < 1e-8
                assert updated['z'] != original['z']
            elif original['mount']['rail']==PLATE_RAIL and side=='front':
                start,end=endpoints(after['chassis'],PLATE_RAIL)
                expected_z=start['z']+(end['z']-start['z'])*original['mount']['t']
                assert abs(updated['z']-expected_z)<1e-8 and updated['z']!=original['z'],updated
            else:
                assert updated['z'] == original['z'], (side, identifier, original, updated)
        zero_deltas(browser)
        outcomes.append({'side':side,'before':before['chassis']['frameMidHeights'],
                         'after':after['chassis']['frameMidHeights'],'attached':sensors(after)[ATTACHED[side]]})
    value = browser.config()
    assert value['chassis']['frameMidHeights'] == values
    result['eachFaceMovesOnlyItsOwnMemberAndAttachedPoints'] = outcomes
    assert sensors(value)['bluesea']['mount']['rail']=='y-bottom-front'
    assert sensors(value)['camera2']['mount']['rail']==PLATE_RAIL
    assert abs(sensors(value)['bluesea']['z']-value['chassis']['height'])<1e-8
    result['blueSeaStaysOnBottomAndFrontRightCameraFollowsPlasticPlate']=True
    result['frameOnlyChangesUseFairCurrentConditions'] = True
    browser.call('/refresh', {})
    browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    assert browser.config() == value
    result['independentHeightsPersistAfterRefresh'] = True
    browser.sensor('camera1'); browser.clickid('set-baseline'); time.sleep(.3)
    browser.clickid('view-mounting');open_frame_settings(browser)
    browser.evaluate("const panel=document.querySelector('.panel-scroll'),e=document.querySelector('[data-testid=chassis-mid-front]');panel.scrollTop+=e.getBoundingClientRect().top-panel.getBoundingClientRect().top-42")
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()");time.sleep(.3)
    browser.screenshot('middle-faces-four-independent-heights.png')
    return value


def verify_drag_and_clamp(browser, current, result):
    browser.sensor('camera1'); browser.button('俯视图'); time.sleep(.35)
    before = browser.config(); start = browser.anchor()
    direction = frame.axis_direction(browser, 'x')
    end = frame.drag_direction(browser, start, direction, 150, release=False)
    during = browser.config(); assert_on_frame(during)
    assert sensors(during)['camera1']['x'] != sensors(before)['camera1']['x']
    for axis in 'yz': assert sensors(during)['camera1'][axis] == sensors(before)['camera1'][axis]
    assert during['chassis'] == before['chassis']
    browser.release(); time.sleep(.3)
    moved = browser.config()
    assert abs(sensors(moved)['camera1']['x']-endpoints(moved['chassis'],'x-mid-left')[1]['x']) < .011
    result['realPointerIndependentMiddleRailEndpoint'] = {'start':start,'end':end,'before':sensors(before)['camera1'],'after':sensors(moved)['camera1']}
    start = browser.anchor(); frame.drag_direction(browser,start,[-direction[0],-direction[1]],60,release=False)
    browser.call('/actions', {'actions':[{'type':'key','id':'middle-escape','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release(); time.sleep(.3)
    assert browser.config() == moved
    assert photo.state(browser)['dragging'] == 'false'
    result['escapeRestoresMountAndFourIndependentHeights'] = True
    browser.clickid('restore-baseline'); time.sleep(.4)
    assert browser.config() == current
    zero_deltas(browser)
    # The front-right camera is on a plastic mounting plate, not a full-height
    # aluminium post: its editable Z range follows the front middle member.
    browser.sensor('camera2');browser.button('三维视角');time.sleep(.35)
    controls=browser.input_state(sensor='camera2')
    low,high=endpoints(current['chassis'],PLATE_RAIL)
    assert controls['x']['disabled'] and controls['y']['disabled'] and not controls['z']['disabled'],controls
    assert abs(controls['z']['min']-low['z'])<1e-8 and abs(controls['z']['max']-high['z'])<1e-8,controls
    plate_before=browser.config();point=browser.anchor(sensor='camera2')
    browser.pointer(point,[point[0],point[1]-150]);time.sleep(.35)
    plate_top=browser.config();assert_on_frame(plate_top)
    assert abs(sensors(plate_top)['camera2']['z']-high['z'])<.011, sensors(plate_top)['camera2']
    point=browser.anchor(sensor='camera2')
    browser.pointer(point,[point[0],point[1]+150]);time.sleep(.35)
    plate_bottom=browser.config();assert_on_frame(plate_bottom)
    assert abs(sensors(plate_bottom)['camera2']['z']-low['z'])<.011,sensors(plate_bottom)['camera2']
    for position in (plate_top,plate_bottom):
        assert position['chassis']==current['chassis']
        for axis in 'xy':assert sensors(position)['camera2'][axis]==sensors(plate_before)['camera2'][axis]
        assert sensors(position)['bluesea']==sensors(plate_before)['bluesea']
    result['frontRightCameraRealPointerPlateLimits']={'controls':controls,'top':sensors(plate_top)['camera2'],'bottom':sensors(plate_bottom)['camera2']}
    browser.clickid('restore-baseline');time.sleep(.4)
    assert browser.config()==current
    open_frame_settings(browser)
    browser.field('框架高度', .5, scope='.additional-settings'); time.sleep(.4)
    shrunk = browser.config(); assert_on_frame(shrunk)
    expected = {side:min(value,.45) for side,value in current['chassis']['frameMidHeights'].items()}
    assert shrunk['chassis']['frameMidHeights'] == expected, (shrunk['chassis'],expected)
    for identifier, sensor in sensors(current).items():
        assert sensors(shrunk)[identifier]['mount'] == sensor['mount']
    zero_deltas(browser)
    result['shortFrameClampsEachFaceIndependently'] = {'before':current['chassis'],'after':shrunk['chassis']}
    browser.clickid('restore-baseline'); time.sleep(.4)
    assert browser.config() == current
    result['restoreBaselineRecoversAllFourHeights'] = True


def verify_export(browser, current, result):
    browser.viewport(1440,960); browser.evaluate('scrollTo(0,0)')
    button = browser.evaluate("const e=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='导出配置');const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]")
    browser.pointer(button)
    until = time.monotonic()+15
    while time.monotonic()<until:
        files = list(browser.download_dir.glob('*.json'))
        if files:
            try:
                exported = json.loads(files[0].read_text()); break
            except (OSError,ValueError): pass
        time.sleep(.15)
    else: raise AssertionError('Actual JSON export download did not finish')
    assert exported['config'] == current
    assert exported['config']['chassis']['frameMidHeights'] == current['chassis']['frameMidHeights']
    result['actualJsonDownloadHasAllIndependentHeights'] = {'bytes':files[0].stat().st_size,
        'heights':exported['config']['chassis']['frameMidHeights'],'sensorCount':len(exported['config']['sensors'])}
    changed = copy.deepcopy(current); changed['chassis']['frameMidHeights'] = dict.fromkeys(SIDES,.15)
    import_layout(browser, changed)
    browser.import_config(exported)
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')"); time.sleep(.4)
    assert browser.config() == current
    result['exportedFileImportsAndRestoresExactLayout'] = True


def verify_layouts(browser, current, result):
    layouts = {}
    for name,width,height in (('desktop-1440',1440,960),('desktop-1920',1920,1080),('mobile',390,844)):
        browser.viewport(width,height); browser.evaluate('scrollTo(0,0)')
        browser.sensor('camera1'); browser.clickid('view-mounting'); open_frame_settings(browser)
        browser.evaluate("const panel=document.querySelector('.panel-scroll'),e=document.querySelector('[data-testid=chassis-mid-front]');panel.scrollTop+=e.getBoundingClientRect().top-panel.getBoundingClientRect().top-42")
        browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()");time.sleep(.4)
        layout = browser.layout(); assert layout['scrollWidth']<=width+2 and not layout['overflow'], (name,layout)
        assert photo.state(browser)['display']=='points' and photo.state(browser)['count']==8 and photo.state(browser)['rails']==17
        actual = browser.evaluate("return Object.fromEntries(['front','rear','left','right'].map(side=>[side,Number(document.querySelector('[data-testid=chassis-mid-'+side+']').value)]))")
        assert actual == current['chassis']['frameMidHeights'],actual
        layouts[name] = layout
        browser.screenshot('middle-faces-'+name+'.png')
        if name=='mobile':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
            browser.clickid('view-mounting');time.sleep(.3)
            browser.screenshot('middle-faces-mobile-scene.png')
    result['layouts'] = layouts
    result['errors'] = browser.evaluate('return __coverageQA.errors')
    assert not result['errors'],result['errors']


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile = Path.home()/'snap/firefox/common/observer-middle-faces-qa-20261024'; profile.mkdir(parents=True,exist_ok=True)
    processes,logs = [],[]; browser = None; download_session = None
    result = {'scope':'Isolated Firefox/Three, synthetic layouts, ephemeral account and private display; no robot or production credentials','pass':False}
    try:
        download_session = tempfile.TemporaryDirectory(prefix='middle-download-',dir=profile)
        password = secrets.token_urlsafe(24)
        env = {**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority'; authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':95','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'middle-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':95','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':95',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'middle-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19179','--profile-root',str(profile)],'middle-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated browser services did not start')
        drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':download_session.name,'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=Path(download_session.name)
        seeding="""const fixtureSeed=sessionStorage.getItem('__middleFixtureSeed');if(fixtureSeed){const seed=JSON.parse(fixtureSeed);localStorage.setItem('observer.sensor-coverage.v1',JSON.stringify(seed.current));localStorage.setItem('observer.sensor-coverage.baseline.v1',JSON.stringify(seed.baseline));sessionStorage.removeItem('__middleFixtureSeed');}addEventListener('pointerdown',e=>window.__dragPointerId=e.pointerId,true);window.__coverageQA="""
        preload=qa.PRELOAD.replace('window.__coverageQA=',seeding)
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':preload})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':"const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));",'args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.evaluate('localStorage.removeItem('+json.dumps(STORAGE)+');localStorage.removeItem('+json.dumps(BASELINE)+');sessionStorage.removeItem("__middleFixtureSeed");')
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'})
        browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
        time.sleep(.4);initial=browser.config();assert_on_frame(initial)
        assert initial['layoutRevision']==3
        assert sensors(initial)['bluesea']['mount']['rail']=='y-bottom-front'
        assert sensors(initial)['camera2']['mount']['rail']==PLATE_RAIL
        assert abs(sensors(initial)['bluesea']['z']-initial['chassis']['height'])<1e-8
        counts=browser.evaluate("const d=document.querySelector('.three-host').dataset;return {all:Number(d.frameRailCount),aluminium:Number(d.aluminumMemberCount),plates:Number(d.plateMountCount)}")
        assert counts=={'all':17,'aluminium':16,'plates':1},counts
        result['confirmedPhysicalDefaultsAndMemberCounts']={'counts':counts,'bluesea':sensors(initial)['bluesea'],'camera2':sensors(initial)['camera2']}
        verify_legacy(browser,initial,result)
        current=verify_faces(browser,initial,result)
        verify_drag_and_clamp(browser,current,result)
        verify_export(browser,current,result)
        verify_layouts(browser,current,result)
        polish.verify_loaded_assets(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('middle-faces-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['failureConfig']=browser.config();result['failureScene']=photo.state(browser)
                result['errors']=browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        for filename in ('browser-middle-faces-report.json','final-browser-report.json'):
            (ARTIFACTS/filename).write_text(json.dumps(result,ensure_ascii=False,indent=2))
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
