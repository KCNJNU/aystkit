"""Real Firefox acceptance for the single shared front camera mounting plate.

Uses synthetic layouts, an ephemeral fixture account and a private Xvfb. The
production website, cameras, robot and desktop authorization are never accessed.
Run only after the runtime source and checked frontend build have been frozen.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
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
ARTIFACTS = Path(os.environ.get('COVERAGE_SHARED_ARTIFACTS', '/tmp/shared-plate-20261024/artifacts'))
SERVER, DRIVER = 'http://127.0.0.1:19188', 'http://127.0.0.1:19189'
STORAGE, BASELINE = qa.STORAGE, drag.BASELINE
SLOTS = {'camera2': 'plate-front-right', 'p100r': 'plate-front-depth'}
SIDES = ('front', 'rear', 'left', 'right')
for module in (qa, drag, frame, polish, photo):
    module.ROOT, module.ARTIFACTS, module.SERVER, module.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER

Browser = photo.Browser
sensors = photo.sensors


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='shared-plate-browser-') as directory:
        config = Config(robot_id='shared-plate-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'shared-plate-fixture',
            'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT / 'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19188, access_log=None)


def near(actual, expected, tolerance=2e-7):
    assert abs(actual-expected) <= tolerance, (actual, expected, tolerance)


def plate_geometry(config):
    chassis = config['chassis']; plate = chassis['cameraPlate']
    half = chassis['frameWidth']/2
    lower = chassis['height']+chassis['frameMidHeights']['front']
    upper = chassis['height']+chassis['frameHeight']
    low, high = -half+plate['width']/2, half-plate['width']/2
    center_y = low+(high-low)*plate['lateral']
    front_x = chassis.get('frameLength', chassis['length'])/2
    return {**plate, 'minCenterY': low, 'maxCenterY': high,
            'center': {'x': front_x+plate['thickness']/2, 'y': center_y, 'z': (lower+upper)/2},
            'height': upper-lower, 'opticalX': front_x+plate['thickness'],
            'sensorZ': lower+(upper-lower)*plate['sensorHeight']}


def endpoints(chassis, rail):
    hx, hy = chassis.get('frameLength', chassis['length'])/2, chassis['frameWidth']/2
    bottom, top = chassis['height'], chassis['height']+chassis['frameHeight']
    if rail in SLOTS.values():
        geometry = plate_geometry({'chassis': chassis})
        offset = (-1 if rail == SLOTS['camera2'] else 1)*geometry['spacing']/2
        return ({'x': geometry['opticalX'], 'y': geometry['minCenterY']+offset, 'z': geometry['sensorZ']},
                {'x': geometry['opticalX'], 'y': geometry['maxCenterY']+offset, 'z': geometry['sensorZ']})
    axis, level, side = rail.split('-')
    z = bottom+chassis['frameMidHeights'][side] if level == 'mid' else top if level == 'top' else bottom
    if axis == 'x':
        y = hy if side == 'left' else -hy
        return {'x': -hx, 'y': y, 'z': z}, {'x': hx, 'y': y, 'z': z}
    if axis == 'y':
        x = hx if side == 'front' else -hx
        return {'x': x, 'y': -hy, 'z': z}, {'x': x, 'y': hy, 'z': z}
    x, y = hx if level == 'front' else -hx, hy if side == 'left' else -hy
    return {'x': x, 'y': y, 'z': bottom}, {'x': x, 'y': y, 'z': top}


def assert_config(config, shared=True):
    assert config['layoutRevision'] == 4 and len(config['sensors']) == 8
    plate = plate_geometry(config)
    assert .001 <= plate['thickness'] <= .1
    assert .04 <= plate['width'] <= config['chassis']['frameWidth']
    assert .02 <= plate['spacing'] <= plate['width']-.02+1e-9
    assert 0 <= plate['lateral'] <= 1 and 0 <= plate['sensorHeight'] <= 1
    half = config['chassis']['frameWidth']/2
    assert plate['center']['y']-plate['width']/2 >= -half-1e-8
    assert plate['center']['y']+plate['width']/2 <= half+1e-8
    for sensor in config['sensors']:
        start, end = endpoints(config['chassis'], sensor['mount']['rail'])
        assert 0 <= sensor['mount']['t'] <= 1
        for axis in 'xyz':
            near(sensor[axis], start[axis]+(end[axis]-start[axis])*sensor['mount']['t'])
    if shared:
        pair = sensors(config)
        a, b = pair['camera2'], pair['p100r']
        for identifier, slot in SLOTS.items():
            assert pair[identifier]['mount']['rail'] == slot
            near(pair[identifier]['mount']['t'], plate['lateral'])
        near(a['x'], b['x']); near(a['z'], b['z'])
        near(b['y']-a['y'], plate['spacing'])
        near((a['y']+b['y'])/2, plate['center']['y'])


def board_state(browser):
    return browser.evaluate("const d=document.querySelector('.three-host').dataset;return Object.fromEntries(Object.entries(d).map(([k,v])=>{try{return [k,JSON.parse(v)]}catch{return [k,v]}}))")


def assert_mesh(browser, config):
    data=board_state(browser);expected=plate_geometry(config)
    for axis in 'xyz':
        near(data['plateGeometryCenter'][axis], expected['center'][axis])
        near(data['plateCenter'][axis], expected['center'][axis])
    for axis, field in (('x','thickness'),('y','width'),('z','height')):
        near(data['plateGeometryDimensions'][axis], expected[field])
        near(data['plateSize'][field], expected[field])
    near(data['plateBounds']['minCenterY'], expected['minCenterY'])
    near(data['plateBounds']['maxCenterY'], expected['maxCenterY'])
    assert data['plateMoveAxis']=='Y' and data['plateRailCount']==2
    for identifier in SLOTS:
        for axis in 'xyz':near(data['plateSensorPositions'][identifier][axis], sensors(config)[identifier][axis])
    return data


def errors(browser):
    current = browser.evaluate('return __coverageQA.errors')
    browser.all_errors.extend(current)
    assert not current, current


def imported(browser, config, shared=True):
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.import_config({'version': 1, 'config': config})
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')")
    time.sleep(.4)
    value = browser.config(); assert_config(value, shared=shared)
    return value


def refresh(browser, seed=None):
    errors(browser)
    if seed is not None:
        browser.evaluate('sessionStorage.setItem("__sharedFixtureSeed",'+json.dumps(json.dumps(seed))+');')
    browser.call('/refresh', {})
    browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
    time.sleep(.35)


def scroll_panel_to(browser, testid='front-plate-editor'):
    browser.evaluate("const p=document.querySelector('.panel-scroll'),e=document.querySelector("+json.dumps('[data-testid="'+testid+'"]')+");p.scrollTop+=e.getBoundingClientRect().top-p.getBoundingClientRect().top-12")


def unchanged_others(before, after):
    for identifier, sensor in sensors(before).items():
        if identifier not in SLOTS:
            assert sensors(after)[identifier] == sensor, (identifier, sensor, sensors(after)[identifier])


def rigid_move(before, after):
    assert_config(after)
    unchanged_others(before, after)
    for identifier in SLOTS:
        for axis in 'xz':
            near(sensors(after)[identifier][axis], sensors(before)[identifier][axis])
    near(after['chassis']['cameraPlate']['spacing'], before['chassis']['cameraPlate']['spacing'])
    assert sensors(after)['camera2']['y'] != sensors(before)['camera2']['y']


def verify_controls(browser, initial, result):
    browser.clickid('select-front-plate'); time.sleep(.4)
    assert browser.evaluate("return document.querySelector('[data-testid=select-front-plate]').getAttribute('aria-pressed')") == 'true'
    movement = []
    for lateral in (.15, .82):
        before = browser.config()
        browser.range('plate-lateral', lateral)
        after = browser.config(); rigid_move(before, after)
        near(after['chassis']['cameraPlate']['lateral'], lateral)
        movement.append({'lateral': lateral, 'pair': {k:sensors(after)[k] for k in SLOTS}})
    before = browser.config()
    geometry = plate_geometry(before)
    target = geometry['minCenterY']+(geometry['maxCenterY']-geometry['minCenterY'])*.32
    browser.set_input('plate-center-y', target)
    after = browser.config(); rigid_move(before, after)
    near(plate_geometry(after)['center']['y'], target)
    near(float(browser.evaluate("return document.querySelector('[data-testid=plate-lateral]').value")), .32, .0011)
    result['plateSliderAndNumericCenterMoveBothOpticalPointsRigidly'] = movement
    # Each attached sensor exposes only the lateral axis. Moving either slot
    # updates the shared plate, rather than tearing the pair apart.
    linked = []
    for identifier, lateral in (('camera2', .25), ('p100r', .72)):
        browser.sensor(identifier); controls = browser.input_state(identifier)
        assert controls['x']['disabled'] and controls['z']['disabled'] and not controls['y']['disabled'], controls
        before = browser.config(); browser.range('sensor-'+identifier+'-position', lateral)
        after = browser.config(); rigid_move(before, after)
        near(after['chassis']['cameraPlate']['lateral'], lateral)
        start, end = endpoints(after['chassis'], SLOTS[identifier])
        near(controls['y']['min'], start['y']); near(controls['y']['max'], end['y'])
        linked.append({'sensor': identifier, 'controls': controls, 'lateral': lateral})
    browser.sensor('camera2')
    geometry = plate_geometry(browser.config()); before = browser.config()
    browser.set_input('sensor-camera2-y', geometry['minCenterY']+geometry['spacing']/4)
    after = browser.config(); rigid_move(before, after)
    result['bothSensorEditorsLockXAndZAndMoveTheWholePlate'] = linked
    # Details retain useful dimensions without hiding the normal position and
    # thickness controls.
    browser.evaluate("document.querySelector('.plate-dimensions').open=true")
    browser.set_input('plate-width', .4); browser.set_input('plate-spacing', .21)
    value = browser.config(); assert_config(value)
    near(value['chassis']['cameraPlate']['width'], .4)
    near(value['chassis']['cameraPlate']['spacing'], .21)
    browser.set_input('plate-width', .1)
    value = browser.config(); assert_config(value)
    near(value['chassis']['cameraPlate']['spacing'], .08)
    browser.set_input('plate-width', .3); browser.set_input('plate-spacing', .16)
    browser.set_input('plate-center-y', 999)
    right = browser.config(); assert_config(right); near(right['chassis']['cameraPlate']['lateral'], 1)
    browser.set_input('plate-center-y', -999)
    left = browser.config(); assert_config(left); near(left['chassis']['cameraPlate']['lateral'], 0)
    result['plateWidthSpacingAndCenterAreClampedInsideTheFrame'] = {'left':plate_geometry(left),'right':plate_geometry(right)}
    browser.range('plate-lateral', .64)
    thickness = []
    for millimeters in (1, 8, 65, 100):
        before = browser.config(); browser.set_input('plate-thickness', millimeters)
        after = browser.config(); assert_config(after); unchanged_others(before, after)
        near(after['chassis']['cameraPlate']['thickness'], millimeters/1000)
        for identifier in SLOTS:
            for axis in 'yz': near(sensors(after)[identifier][axis], sensors(before)[identifier][axis])
            near(sensors(after)[identifier]['x']-sensors(before)[identifier]['x'],
                 after['chassis']['cameraPlate']['thickness']-before['chassis']['cameraPlate']['thickness'])
        thickness.append({'millimeters':millimeters,'geometry':plate_geometry(after),'scene':assert_mesh(browser,after)})
    attrs=browser.evaluate("const e=document.querySelector('[data-testid=plate-thickness]');return {min:e.min,max:e.max,step:e.step}")
    assert attrs == {'min':'1','max':'100','step':'1'},attrs
    result['thicknessOneToHundredMillimetersMovesTheVisibleBoardAndBothOpticalXOnly'] = thickness
    browser.set_input('plate-thickness', 24)
    browser.clickid('select-front-plate'); scroll_panel_to(browser)
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    time.sleep(.35)
    browser.screenshot('shared-plate-close-pair-and-controls.png')
    value=browser.config(); assert_config(value)
    return value


def verify_legacy(browser, initial, result):
    cases=[]
    for custom in (False, True):
        legacy=copy.deepcopy(initial);legacy['layoutRevision']=3;legacy['chassis'].pop('cameraPlate',None)
        pair=sensors(legacy);chassis=legacy['chassis']
        if custom:
            pair['camera2']['mount']={'rail':'z-front-right','t':.22}
            pair['p100r']['mount']={'rail':'z-front-left','t':.12}
        else:
            pair['camera2']['mount']={'rail':'plate-front-right','t':.63}
            pair['p100r']['mount']={'rail':'z-front-left','t':(.78439228-.25)/.9}
        for identifier in SLOTS:
            sensor=pair[identifier]
            if sensor['mount']['rail']=='plate-front-right':
                low=chassis['height']+chassis['frameMidHeights']['front'];high=chassis['height']+chassis['frameHeight']
                sensor.update(x=chassis['frameLength']/2,y=-.32*chassis['frameWidth'],z=low+(high-low)*.63)
            else:
                start,end=endpoints(chassis,sensor['mount']['rail'])
                for axis in 'xyz':sensor[axis]=start[axis]+(end[axis]-start[axis])*sensor['mount']['t']
        refresh(browser, {'current':legacy,'baseline':{'version':1,'config':legacy}})
        current=browser.config()
        baseline=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
        assert_config(current, shared=not custom);assert_config(baseline,shared=not custom)
        assert current==baseline
        if custom:
            for identifier in SLOTS:
                assert sensors(current)[identifier]['mount']==pair[identifier]['mount']
                for axis in 'xyz':near(sensors(current)[identifier][axis],pair[identifier][axis])
        else:
            near(current['chassis']['cameraPlate']['sensorHeight'], .63)
            near(sensors(current)['camera2']['y'],pair['camera2']['y'])
            near(sensors(current)['camera2']['z'],pair['camera2']['z'])
        cases.append({'custom':custom,'oldPair':pair,'migratedPair':{k:sensors(current)[k] for k in SLOTS},'geometry':plate_geometry(current)})
    result['revisionThreeActualStoredCurrentAndBaselineMigrateOrPreserveCustomMounts']=cases
    imported(browser,initial)
    browser.select_rail('z-front-right',sensor='camera2')
    browser.range('sensor-camera2-position',.22)
    browser.select_rail('z-front-left',sensor='p100r')
    browser.range('sensor-p100r-position',.12)
    custom=browser.config();assert_config(custom,shared=False)
    refresh(browser)
    assert browser.config()==custom
    result['revisionFourExplicitCustomMountsSurviveRefresh']=True
    imported(browser,initial)


def verify_comparison(browser, initial, result):
    fixture=copy.deepcopy(initial)
    fixture['analysis'].update(length=4,width=4,resolution=.05,occlusion=False,height=.2)
    for sensor in fixture['sensors']:sensor['enabled']=sensor['id'] in SLOTS
    fixture['chassis']['cameraPlate']['lateral']=.15
    current=imported(browser,fixture)
    browser.clickid('set-baseline');time.sleep(.25)
    baseline=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    before_metrics=browser.metrics()
    browser.range('plate-lateral',.85)
    moved=browser.config();rigid_move(current,moved)
    stored=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    assert stored==baseline
    assert browser.testid('baseline-note')=='同一切片与分析区域'
    assert sensors(stored)['camera2']['y']!=sensors(moved)['camera2']['y']
    deltas={key:drag.get_number(browser,key+'-delta') for key in ('coverage','blind','overlap')}
    result['baselineRetainsOriginalPlateLayoutWhileCurrentPlateMoves']={'baselinePair':{k:sensors(stored)[k] for k in SLOTS},
        'currentPair':{k:sensors(moved)[k] for k in SLOTS},'beforeMetrics':before_metrics,'afterMetrics':browser.metrics(),'displayedDeltas':deltas}
    browser.clickid('restore-baseline');time.sleep(.35)
    assert browser.config()==current
    for key in ('coverage','blind','overlap'):near(drag.get_number(browser,key+'-delta'),0)
    result['restoreBaselineRestoresSharedPlateLayoutAndCoverage']=True
    imported(browser,initial)


def verify_independent_frame_heights(browser, current, result):
    imported(browser,current)
    browser.evaluate("document.querySelector('[data-testid=chassis-mid-front]').closest('details').open=true")
    outcomes=[]
    for side, height in zip(SIDES,(.22,.43,.34,.60)):
        before=browser.config()
        browser.set_input('chassis-mid-'+side,height)
        after=browser.config();assert_config(after);assert_mesh(browser,after)
        expected={**before['chassis']['frameMidHeights'],side:height}
        assert after['chassis']['frameMidHeights']==expected
        for identifier in SLOTS:
            for axis in 'xy':near(sensors(after)[identifier][axis],sensors(before)[identifier][axis])
            if side!='front':near(sensors(after)[identifier]['z'],sensors(before)[identifier]['z'])
        near(sensors(after)['bluesea']['z'],after['chassis']['height'])
        outcomes.append({'side':side,'height':height,'pairZ':sensors(after)['camera2']['z'],'plateHeight':plate_geometry(after)['height']})
    result['fourMiddleMembersRemainIndependentAndOnlyFrontSetsBoardVerticalSpan']=outcomes
    imported(browser,current)


def drag_actual_board(browser, direction, distance, attempts):
    # A sensor's pick sphere intentionally wins over the board behind it. Test
    # actual board picking, trying the lower blank face before the upper face,
    # and verify the selected drag target before making any positional change.
    before=browser.config()
    for index in (1,0,2):
        browser.clickid('select-front-plate');time.sleep(.2)
        state=board_state(browser)
        candidates=state['platePickProjections']+[state['plateProjection']]
        point=candidates[index];start=[point['x'],point['y']]
        browser.pointer(start,release=False)
        down=board_state(browser)
        attempts.append({'candidate':index,'start':start,'plateDragActive':down.get('plateDragActive'),'selectedRail':down.get('selectedRail')})
        if down.get('plateDragActive') is True:
            end=[start[0]+direction[0]*distance,start[1]+direction[1]*distance]
            browser.call('/actions',{'actions':[{'type':'pointer','id':'drag-mouse','parameters':{'pointerType':'mouse'},'actions':[
                {'type':'pointerMove','origin':'viewport','x':round(end[0]),'y':round(end[1]),'duration':600},
                {'type':'pause','duration':250}]}]})
            return start
        browser.release();time.sleep(.2)
        assert browser.config()==before,'A test pick changed position before movement'
    raise AssertionError(('Neither actual board blank-face candidate was reachable',attempts))


def verify_real_drag(browser, current, result):
    imported(browser,current)
    browser.clickid('select-front-plate');time.sleep(.4)
    before=browser.config();scene=assert_mesh(browser,before)
    assert set(scene.get('gizmoProjections',{}))=={'Y'},scene.get('gizmoProjections')
    assert scene.get('selectedRailAxis')=='y' and scene.get('selectedRail')=='front-plate'
    rail=scene['plateRailProjections'];dx=rail['end']['x']-rail['start']['x'];dy=rail['end']['y']-rail['start']['y']
    length=math.hypot(dx,dy);assert length>10,(rail,length)
    direction=[dx/length,dy/length]
    attempts=[]
    start=drag_actual_board(browser,direction,170,attempts)
    time.sleep(.3)
    moved=browser.config();rigid_move(before,moved)
    assert board_state(browser)['dragActive'] is True
    browser.release();time.sleep(.3)
    moved=browser.config();assert_config(moved)
    near(moved['chassis']['cameraPlate']['lateral'],1,.025)
    result['realPointerOnPlasticBoardMovesPairToLateralFrameStop']={'start':start,'before':plate_geometry(before),'after':plate_geometry(moved),'actualBoardPickAttempts':attempts}
    start=drag_actual_board(browser,[-direction[0],-direction[1]],55,attempts)
    time.sleep(.25)
    assert browser.config()!=moved
    browser.call('/actions',{'actions':[{'type':'key','id':'shared-escape','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release();time.sleep(.3)
    assert browser.config()==moved
    assert board_state(browser)['dragActive'] is False
    result['escapeRollsBackBothOpticalPointsAndBoard']=True
    # The sole visible transform handle must move only Y too.
    handle=browser.anchor(kind='gizmo',axis='Y')
    before=browser.config()
    browser.pointer(handle,[handle[0]-direction[0]*140,handle[1]-direction[1]*140]);time.sleep(.35)
    after=browser.config();rigid_move(before,after)
    near(after['chassis']['cameraPlate']['lateral'],0,.03)
    result['realYTransformHandleMovesWholePlateAndClampsAtOppositeStop']={'before':plate_geometry(before),'after':plate_geometry(after)}
    imported(browser,current)


def verify_export(browser,current,result):
    browser.viewport(1440,960);browser.evaluate('scrollTo(0,0)')
    browser.clickid('set-baseline');time.sleep(.25)
    button=browser.evaluate("const e=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='导出配置');const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]")
    browser.pointer(button)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        files=list(browser.download_dir.glob('*.json'))
        if files:
            try:exported=json.loads(files[0].read_text());break
            except (OSError,ValueError):pass
        time.sleep(.15)
    else:raise AssertionError('Actual configuration JSON download did not finish')
    assert exported['config']==current
    assert_config(exported['config'])
    (ARTIFACTS/'shared-plate-exported-configuration.json').write_text(json.dumps(exported,ensure_ascii=False,indent=2))
    browser.range('plate-lateral',.03)
    browser.import_config(exported)
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')");time.sleep(.35)
    assert browser.config()==current
    refresh(browser);assert browser.config()==current
    browser.range('plate-lateral',.93);browser.clickid('restore-baseline');time.sleep(.35)
    assert browser.config()==current
    result['realJsonDownloadImportRefreshAndRestoreRetainExactSharedPlateAndEightDevices']={
        'bytes':files[0].stat().st_size,'board':current['chassis']['cameraPlate'],'sensorCount':len(current['sensors'])}


def verify_layouts(browser,current,result):
    layouts={}
    for name,width,height in (('desktop-1440',1440,960),('desktop-1920',1920,1080),('tablet-768',768,1024),('mobile-390',390,844)):
        browser.viewport(width,height);browser.evaluate('scrollTo(0,0)')
        browser.clickid('select-front-plate');scroll_panel_to(browser)
        browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
        time.sleep(.4)
        layout=browser.layout()
        assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        scene=assert_mesh(browser,current)
        assert scene['sensorDisplay']=='points' and scene['sensorPointCount']==8
        assert scene['frameRailCount']==18 and scene['aluminumMemberCount']==16 and scene['plateMountCount']==1,scene
        bounds=browser.evaluate("return ['plate-lateral','plate-center-y','plate-thickness'].map(id=>{const e=document.querySelector('[data-testid='+id+']'),r=e.getBoundingClientRect();return {id,left:r.left,right:r.right,width:r.width,disabled:e.disabled}})")
        assert all(v['left']>=0 and v['right']<=width+1 and v['width']>30 and not v['disabled'] for v in bounds),(name,bounds)
        browser.screenshot('shared-plate-'+name+'.png')
        layouts[name]={'layout':layout,'controls':bounds,'sceneCounts':{k:scene[k] for k in ('frameRailCount','aluminumMemberCount','plateMountCount','sensorPointCount')}}
        if name=='mobile-390':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
            browser.clickid('view-mounting');time.sleep(.35)
            browser.screenshot('shared-plate-mobile-scene.png')
    result['layouts']=layouts
    errors(browser);result['errors']=browser.all_errors


def verify_checked_build(result):
    path=ROOT.parent/'build-validation.json'
    if not path.exists():return
    checked=json.loads(path.read_text())
    assert checked['pass'] and result['assetDigests']==checked['assetDigests']
    assert all(checked['sourceDigests'].get(name)==digest for name,digest in result['sourceDigests'].items())
    assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in checked['sourceDigests'].items())
    result['checkedBuildProvenance']={'manifest':str(path),'manifestSha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'recursiveRuntimeSourceFiles':len(result['sourceDigests']),'checkedBuildInputFiles':len(checked['sourceDigests']),
        'allRuntimeSourceDigestsMatch':True,'allBuildInputDigestsStillMatch':True,'allBuiltAssetDigestsMatch':True}


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-shared-plate-qa-20261024'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'scope':'Isolated real Firefox/Three WebGL; synthetic configurations and ephemeral account; private Xvfb; no production or robot access','pass':False}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        downloads=tempfile.TemporaryDirectory(prefix='shared-download-',dir=profile)
        password=secrets.token_urlsafe(24)
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':94','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'shared-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':94','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':94',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'shared-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19189','--profile-root',str(profile)],'shared-geckodriver')):
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
        time.sleep(.4);initial=browser.config();assert_config(initial)
        result['webgl']=browser.evaluate('return {contexts:__coverageQA.contexts,drawCalls:__coverageQA.drawCalls}')
        near(initial['chassis']['cameraPlate']['thickness'],.008)
        front_label=browser.evaluate("return document.querySelector('[data-testid=sensor-camera2]').textContent")
        assert '前相机' in front_label and '前右' not in front_label,front_label
        assert sensors(initial)['bluesea']['mount']['rail']=='y-bottom-front'
        near(sensors(initial)['bluesea']['z'],initial['chassis']['height'])
        scene=assert_mesh(browser,initial)
        assert scene['frameRailCount']==18 and scene['aluminumMemberCount']==16 and scene['plateMountCount']==1,scene
        result['defaultTwoCamerasOneEightMillimeterPlateEightPointsSixteenAluminumMembersEighteenLogicalRails']={'geometry':plate_geometry(initial),'scene':scene}
        current=verify_controls(browser,initial,result)
        # Save early so the parent can review the clear board/pair screenshot
        # while the remaining genuine-pointer and persistence checks run.
        (ARTIFACTS/'shared-plate-controls-checkpoint.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        verify_legacy(browser,initial,result)
        verify_comparison(browser,initial,result)
        verify_independent_frame_heights(browser,current,result)
        verify_real_drag(browser,current,result)
        verify_export(browser,current,result)
        verify_layouts(browser,current,result)
        polish.verify_loaded_assets(browser,result)
        assert result['sourceDigests']==starting,'Runtime source changed during final browser acceptance'
        verify_checked_build(result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('shared-plate-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['failureConfig']=browser.config();result['failureScene']=board_state(browser)
                result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        for filename in ('browser-shared-plate-report.json','final-browser-report.json'):
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
