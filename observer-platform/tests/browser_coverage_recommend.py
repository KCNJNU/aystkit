"""Real Firefox acceptance for camera-only constrained recommendations.

Only loopback fixture accounts and a private Xvfb are used. Optimizer output is
checked with a separate Node bundle of computeCoverage on the original grid.
"""
from __future__ import annotations

import asyncio
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
import urllib.error
import urllib.request

from aiohttp import web
import browser_coverage_refine as refine

fixed, shared = refine.fixed, refine.shared
qa, polish = shared.qa, shared.polish
Browser = shared.Browser
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('COVERAGE_RECOMMEND_ARTIFACTS', '/tmp/camera-recommend-20261024/artifacts'))
SERVER, DRIVER = 'http://127.0.0.1:19218', 'http://127.0.0.1:19219'
STORAGE, BASELINE = shared.STORAGE, shared.BASELINE
CAMERAS = ('camera1', 'camera2', 'camera3', 'camera4', 'p100r')
SLOTS = {'camera2', 'p100r'}
for module in (refine, fixed, shared, shared.qa, shared.drag, shared.frame, shared.polish, shared.photo):
    module.ROOT, module.ARTIFACTS, module.SERVER, module.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='camera-recommend-browser-') as directory:
        config = Config(robot_id='camera-recommend-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'camera-recommend-fixture', 'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19218, access_log=None)


def preload_source():
    source = refine.preload_source()
    tracking = r'''
 window.__cameraQA={events:[],heartbeats:0};window.__cameraWorkers=[];
 setInterval(()=>__cameraQA.heartbeats++,25);
 const NativeWorker=window.Worker;
 window.Worker=class extends NativeWorker {
   constructor(...args){super(...args);this.__qaId=__cameraWorkers.length;__cameraWorkers.push(this);
     __cameraQA.events.push({type:'created',id:this.__qaId,url:String(args[0]),at:performance.now()});
     this.addEventListener('message',event=>{const d=event.data;__cameraQA.events.push({type:'received',id:this.__qaId,data:d,at:performance.now()})});
   }
   postMessage(...args){__cameraQA.events.push({type:'posted',id:this.__qaId,data:args[0],at:performance.now()});return super.postMessage(...args)}
   terminate(){__cameraQA.events.push({type:'terminated',id:this.__qaId,at:performance.now()});return super.terminate()}
 };
'''
    at = source.rfind('}')
    return source[:at] + tracking + source[at:]


def node_reference(config):
    path = ARTIFACTS/'math-input.json'
    path.write_text(json.dumps(config))
    result = subprocess.run(['node', str(ARTIFACTS/'math-reference.mjs'), str(path)], cwd=ROOT/'coverage-config', check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def prepare_reference():
    path = ARTIFACTS/'math-reference.ts'
    path.write_text("import fs from 'node:fs';\nimport {computeCoverage} from '" + str(ROOT/'coverage-config/src/geometry.ts') + "';\nimport {sanitizeConfig} from '" + str(ROOT/'coverage-config/src/model.ts') + "';\nconst config=sanitizeConfig(JSON.parse(fs.readFileSync(process.argv[2],'utf8')));\nconst overall=computeCoverage(config), camera=computeCoverage({...config,sensors:config.sensors.map(s=>({...s,enabled:s.enabled&&(s.kind==='camera'||s.kind==='depth')}))});\nconsole.log(JSON.stringify({config,overall:Object.fromEntries(Object.entries(overall).filter(([k])=>k!=='cells')),camera:Object.fromEntries(Object.entries(camera).filter(([k])=>k!=='cells'))}));\n")
    subprocess.run(['node_modules/esbuild/bin/esbuild', str(path), '--bundle', '--platform=node', '--format=esm', '--outfile='+str(ARTIFACTS/'math-reference.mjs')], cwd=ROOT/'coverage-config', check=True, capture_output=True)


def imported(browser, config):
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.import_config({'version': 1, 'config': config})
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')")
    time.sleep(.35)
    canonical = browser.config()
    fixed.assert_config(canonical)
    assert canonical == node_reference(config)['config']
    return canonical


def baseline(browser):
    return browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')


def status(browser):
    return browser.evaluate("return document.querySelector('[data-testid=recommend-panel]')?.dataset.status||null")


def start(browser, expected=None):
    count = browser.evaluate("return __cameraQA.events.filter(e=>e.type==='created').length")
    browser.clickid('recommend-start')
    browser.wait("__cameraQA.events.filter(e=>e.type==='created').length>"+str(count))
    browser.wait("(()=>{const p=document.querySelector('[data-testid=recommend-panel]');return p&&p.dataset.status!=='running'})()", timeout=30)
    found = status(browser)
    if expected is not None:
        assert found == expected, (found, expected)
    events = browser.evaluate('return __cameraQA.events')
    results = [e for e in events if e['type'] == 'received' and e['data'].get('type') == 'result']
    assert results, events
    response = results[-1]
    worker_id = response['id']
    assert any(e['type'] == 'terminated' and e['id'] == worker_id for e in events), events
    progress = [e['data']['progress'] for e in events if e['type'] == 'received' and e['id'] == worker_id and e['data'].get('type') == 'progress']
    assert progress and progress[0] == 0 and progress[-1] == 1 and progress == sorted(progress), progress
    return response['data']['result']


def constraints(before, after):
    fixed.assert_config(after)
    assert before['analysis'] == after['analysis']
    chassis_before, chassis_after = copy.deepcopy(before['chassis']), copy.deepcopy(after['chassis'])
    chassis_before['cameraPlate'].pop('lateral');chassis_after['cameraPlate'].pop('lateral')
    assert chassis_before == chassis_after
    moving=[]
    for identifier, sensor in shared.sensors(before).items():
        proposed=shared.sensors(after)[identifier]
        if identifier not in CAMERAS or (not sensor['enabled'] and identifier not in SLOTS):
            assert sensor == proposed, (identifier, sensor, proposed)
        for key in set(sensor)-{'x','y','z','mount'}:
            assert proposed[key] == sensor[key], (identifier, key)
        assert proposed['mount']['rail'] == sensor['mount']['rail']
        if any(abs(sensor[axis]-proposed[axis])>1e-9 for axis in 'xyz'):
            moving.append(identifier)
    if not any(shared.sensors(before)[identifier]['enabled'] for identifier in SLOTS):
        assert before['chassis']['cameraPlate'] == after['chassis']['cameraPlate']
    return moving


def check_result(browser, before, outcome):
    reference_before = node_reference(before)
    reference_after = node_reference(outcome['config'])
    assert reference_after['config'] == outcome['config']
    moved = constraints(before, outcome['config'])
    assert set(moved) == set(outcome['movedIds'])
    for key, reference in (('before',reference_before), ('after',reference_after)):
        metric=outcome[key]
        for field, expected in {
            'cameraPercent':reference['camera']['percent'],'overallPercent':reference['overall']['percent'],
            'cameraBlindArea':reference['camera']['blindArea'],'overallBlindArea':reference['overall']['blindArea'],
            'cameraCovered':reference['camera']['covered'],'overallCovered':reference['overall']['covered'],
            'totalCells':reference['overall']['total']}.items():
            shared.near(metric[field],expected,1e-9)
        for name, expected in (('camera',reference['camera']['percent']), ('overall',reference['overall']['percent'])):
            value=browser.evaluate("return document.querySelector('[data-testid=recommend-"+name+'-'+key+"]').textContent")
            shared.near(float(value.rstrip('%')),expected,.0051)
    assert outcome['after']['cameraCovered'] >= outcome['before']['cameraCovered']
    assert outcome['after']['overallCovered'] >= outcome['before']['overallCovered']
    if outcome['status']=='improved':
        assert outcome['after']['cameraCovered']>outcome['before']['cameraCovered'] or outcome['after']['overallCovered']>outcome['before']['overallCovered']
    rows=browser.evaluate("return [...document.querySelectorAll('[data-testid^=recommend-row-]')].map(row=>({id:row.dataset.testid.replace('recommend-row-',''),cells:[...row.querySelectorAll('td')].map(td=>({axis:td.dataset.axis,value:Number(td.dataset.value)}))}))")
    assert {row['id'] for row in rows} == set(CAMERAS)
    for row in rows:
        for cell in row['cells']:
            assert cell['value'] == shared.sensors(outcome['config'])[row['id']][cell['axis']]
    assert browser.config() == before, 'Recommendation automatically mutated layout'
    return {'before':outcome['before'],'after':outcome['after'],'movedIds':moved,'evaluations':outcome['evaluations'],'elapsedMs':outcome['elapsedMs'],'gridCells':reference_before['overall']['total']}


def verify_preview_apply_undo(browser, fixture, result, name):
    before=imported(browser,fixture)
    browser.clickid('set-baseline');time.sleep(.2)
    saved=baseline(browser)
    outcome=start(browser,'improved')
    details=check_result(browser,before,outcome)
    browser.clickid('set-baseline');time.sleep(.2)
    assert status(browser)=='improved', 'Changing comparison baseline invalidated independent recommendation'
    assert baseline(browser)==saved
    browser.evaluate("document.querySelector('[data-testid=recommend-preview]').click()")
    time.sleep(.3)
    scene=shared.board_state(browser)
    assert set(scene['recommendationIds']) == set(outcome['movedIds'])
    assert scene['recommendationPointCount']==len(outcome['movedIds'])
    assert scene['recommendationMotionCount']==len(outcome['movedIds'])
    assert scene['sensorPointCount']==8 and scene['frameRailCount']==18 and scene['aluminumMemberCount']==16 and scene['plateMountCount']==1
    assert browser.config()==before and baseline(browser)==saved
    for point in scene['recommendationPositions']:
        for axis in 'xyz':
            shared.near(point[axis],shared.sensors(outcome['config'])[point['id']][axis])
            shared.near(point['current'][axis],shared.sensors(before)[point['id']][axis])
    browser.clickid('view-mounting');time.sleep(.3)
    browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0;document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.screenshot('recommend-'+name+'-preview.png')
    browser.evaluate("document.querySelector('[data-testid=recommend-preview]').click()")
    time.sleep(.2);assert shared.board_state(browser)['recommendationIds']==[]
    browser.clickid('recommend-apply');time.sleep(.4)
    after=browser.config();assert after==outcome['config'];constraints(before,after)
    assert status(browser)=='applied' and baseline(browser)==saved
    assert shared.board_state(browser)['recommendationIds']==[]
    browser.screenshot('recommend-'+name+'-applied.png')
    # Baseline may change independently while the undo snapshot stays usable.
    browser.clickid('set-baseline');time.sleep(.2)
    assert baseline(browser)==after
    rerun=start(browser)
    assert browser.evaluate("return !!document.querySelector('[data-testid=recommend-undo]')"),status(browser)
    browser.clickid('recommend-undo');time.sleep(.4)
    assert browser.config()==before and baseline(browser)==after
    assert status(browser) is None
    assert not browser.evaluate("return !!document.querySelector('[data-testid=recommend-undo]')")
    result[name]={**details,'previewScene':{k:scene[k] for k in ('recommendationIds','recommendationPositions','recommendationPointCount','recommendationMotionCount')},'applyUndoPreservedIndependentBaseline':True,'rerunStatusBeforeUndo':rerun['status']}
    return before


def verify_worker_lifecycle(browser, initial, result):
    fixture=copy.deepcopy(initial);fixture['analysis'].update(length=12,width=12,resolution=.04)
    for sensor in fixture['sensors']:
        sensor['maxRange']=30
    big=imported(browser,fixture)
    browser.sensor('camera1');time.sleep(.2)
    trace_start=browser.evaluate('return __cameraQA.events.length')
    # Execute real controls from browser timers, without WebDriver round trips
    # that can outlast the highly optimized native search itself.
    responsive=browser.call('/execute/async',{'script':"""const done=arguments[arguments.length-1],hb=__cameraQA.heartbeats,at=performance.now();
      document.querySelector('[data-testid=recommend-start]').click();
      setTimeout(()=>{const before=document.querySelector('[data-testid=recommend-panel]')?.dataset.status;
        document.querySelector('[data-testid=set-baseline]').click();
        const after=document.querySelector('[data-testid=recommend-panel]')?.dataset.status;
        const progress=__cameraQA.events.filter(e=>e.type==='received'&&e.data.type==='progress').map(e=>e.data.progress);
        document.querySelector('[data-testid=recommend-cancel]')?.click();
        setTimeout(()=>done({before,after,heartbeats:__cameraQA.heartbeats-hb,latency:performance.now()-at,progress}),20);
      },60);""",'args':[]})
    assert responsive['before']=='running' and responsive['after']=='running',responsive
    assert responsive['heartbeats']>0 and responsive['latency']<2500,responsive
    assert responsive['progress'],responsive
    assert status(browser) is None and browser.config()==big
    assert browser.evaluate("return !document.querySelector('[data-testid=recommend-start]').disabled")
    changed=browser.call('/execute/async',{'script':"""const done=arguments[arguments.length-1];document.querySelector('[data-testid=recommend-start]').click();
      setTimeout(()=>{const running=document.querySelector('[data-testid=recommend-panel]')?.dataset.status;
        const e=document.querySelector('[data-testid=sensor-camera1-position]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'.21');e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
        setTimeout(()=>done({running,panel:document.querySelector('[data-testid=recommend-panel]')?.dataset.status||null}),80);
      },60);""",'args':[]})
    assert changed['running']=='running' and changed['panel'] is None,changed
    time.sleep(.3)
    assert status(browser) is None and browser.config()!=big
    assert shared.board_state(browser)['recommendationIds']==[]
    events=browser.evaluate('return __cameraQA.events.slice('+str(trace_start)+')')
    created=[e for e in events if e['type']=='created'];terminated=[e for e in events if e['type']=='terminated']
    assert len(created)==2 and {e['id'] for e in created}<={e['id'] for e in terminated},events
    posted=[e['data'] for e in events if e['type']=='posted']
    assert len(posted)==2 and posted[0]['generation']!=posted[1]['generation']
    browser.clickid('restore-baseline');time.sleep(.4);assert browser.config()==big
    assert status(browser) is None
    hidden=browser.call('/execute/async',{'script':"""const done=arguments[arguments.length-1];document.querySelector('[data-testid=recommend-start]').click();setTimeout(()=>{const running=document.querySelector('[data-testid=recommend-panel]')?.dataset.status;dispatchEvent(new Event('pagehide'));setTimeout(()=>done({running,panel:document.querySelector('[data-testid=recommend-panel]')?.dataset.status||null}),30)},60);""",'args':[]})
    assert hidden['running']=='running' and hidden['panel'] is None,hidden
    result['realWorkerCancellationAndStaleConfigurationSafety']={'mainThreadTimerAndBaselineControl':responsive,'runningBaselineDoesNotCancel':True,'cancelAndChangedConfigTerminateNativeWorker':True,'generations':[p['generation'] for p in posted],'pagehideCancels':True}
    imported(browser,initial)


def verify_invalidation_and_empty(browser, fixture, initial, result):
    cases=[]
    actions=[('sensor-parameter',lambda: (browser.sensor('camera1'),browser.field('水平视野',92))),
             ('enabled',lambda: browser.evaluate("document.querySelector('[data-testid=sensor-camera1] [role=switch]').click()")),
             ('board-dimension',lambda: (browser.clickid('select-front-plate'),browser.set_input('plate-thickness',27))),
             ('analysis',lambda: (browser.evaluate("[...document.querySelectorAll('.additional-settings details')].find(d=>d.textContent.includes('分析区域')).open=true"),browser.field('高度切片',.55,'.additional-settings'))),
             ('import',lambda: imported(browser,initial)),
             ('restore-baseline',lambda: browser.clickid('restore-baseline'))]
    for name, action in actions:
        before=imported(browser,fixture)
        if name=='restore-baseline':
            browser.clickid('set-baseline');time.sleep(.15)
            browser.sensor('camera1');browser.range('sensor-camera1-position',.31)
            before=browser.config()
        outcome=start(browser)
        assert status(browser) in ('improved','no-improvement')
        if outcome['status']=='improved':
            browser.evaluate("document.querySelector('[data-testid=recommend-preview]').click()")
        action();time.sleep(.4)
        assert browser.config()!=before,(name,'Config did not change')
        assert status(browser) is None,(name,status(browser))
        assert shared.board_state(browser)['recommendationIds']==[]
        assert not browser.evaluate("return !!document.querySelector('[data-testid=recommend-apply]')")
        cases.append(name)
    off=copy.deepcopy(initial)
    for sensor in off['sensors']:
        if sensor['id'] in CAMERAS:sensor['enabled']=False
    imported(browser,off)
    assert browser.evaluate("return document.querySelector('[data-testid=recommend-start]').disabled")
    empty=copy.deepcopy(initial);empty['analysis'].update(length=.5,width=.5)
    imported(browser,empty);noarea=start(browser,'no-area');assert noarea['before']['totalCells']==0
    assert not browser.evaluate("return !!document.querySelector('[data-testid=recommend-apply]')")
    impossible=copy.deepcopy(initial)
    for sensor in impossible['sensors']:
        sensor['enabled']=sensor['id']=='camera1'
        sensor['maxRange']=.01;sensor['minRange']=0
    no_gain=imported(browser,impossible);outcome=start(browser,'no-improvement')
    check_result(browser,no_gain,outcome)
    assert outcome['config']==no_gain and outcome['movedIds']==[]
    assert browser.evaluate("return document.querySelector('[data-testid=recommend-apply]').disabled&&document.querySelector('[data-testid=recommend-preview]').disabled")
    result['parameterChangesInvalidateFinishedResultsAndPreview']=cases
    result['noCameraNoAreaAndNoImprovementHaveNoApplicableRecommendation']=True


def verify_auth_assets(browser, result):
    assets=sorted((ROOT/'cloud/frontend/coverage/assets').glob('*'))
    workers=[p for p in assets if p.name.startswith('camera-optimizer-') and p.suffix=='.js']
    assert len(workers)==1,[p.name for p in assets]
    worker_path='/coverage-assets/assets/'+workers[0].name
    def response_status(opener,url):
        try:
            with opener.open(SERVER+url) as response:return response.status
        except urllib.error.HTTPError as error:return error.code
    assert response_status(qa.OPENER,worker_path)==401
    cookie=http.cookiejar.CookieJar()
    pending=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(cookie))
    registration=urllib.request.Request(SERVER+'/api/register',json.dumps({'username':'recommend-pending-fixture','password':secrets.token_urlsafe(20)}).encode(),{'Content-Type':'application/json','Origin':SERVER})
    with pending.open(registration) as response:assert response.status==201
    assert response_status(pending,worker_path)==403
    urls=[SERVER+'/coverage-assets/assets/'+path.name for path in assets]
    fetched=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];Promise.all(arguments[0].map(async url=>{const r=await fetch(url);const hash=await crypto.subtle.digest('SHA-256',await r.arrayBuffer());return [url,{status:r.status,hash:[...new Uint8Array(hash)].map(n=>n.toString(16).padStart(2,'0')).join(''),type:r.headers.get('Content-Type'),csp:r.headers.get('Content-Security-Policy')}]})).then(v=>done(Object.fromEntries(v))).catch(e=>done({error:String(e)}));''','args':[urls]})
    for path,url in zip(assets,urls):
        value=fetched[url]
        assert value['status']==200 and value['hash']==hashlib.sha256(path.read_bytes()).hexdigest(),(path,value)
        if path.suffix=='.js':assert 'javascript' in value['type'],value
    worker=fetched[SERVER+worker_path]
    assert "default-src 'self'" in worker['csp'] and "script-src 'self'" in worker['csp'],worker
    events=browser.evaluate("return __cameraQA.events.filter(e=>e.type==='created')")
    assert events and all(e['url']==SERVER+worker_path for e in events),events
    result['workerAndAllBuiltAssetsAreSameOriginApprovedOnlyAndDigestVerified']={'worker':workers[0].name,'anonymousStatus':401,'pendingStatus':403,'approvedStatus':200,'responseAssets':fetched}


def verify_responsive(browser, fixture, result):
    before=imported(browser,fixture);outcome=start(browser,'improved')
    browser.evaluate("document.querySelector('[data-testid=recommend-preview]').click()")
    layouts={}
    for name,width,height in (('desktop-1440',1440,960),('desktop-1920',1920,1080),('tablet-768',768,1024),('mobile-390',390,844)):
        browser.viewport(width,height);browser.evaluate("scrollTo(0,0);document.querySelector('.panel-scroll').scrollTop=0;document.querySelector('button[aria-label=关闭提示]')?.click()")
        time.sleep(.3)
        layout=browser.layout()
        assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        controls=browser.evaluate("return [...document.querySelectorAll('[data-testid=recommend-start],[data-testid=recommend-panel] table,[data-testid=recommend-preview],[data-testid=recommend-apply]')].map(e=>{const r=e.getBoundingClientRect();return {test:e.dataset.testid||'table',width:r.width,left:r.left,right:r.right}})")
        assert all(e['left']>=0 and e['right']<=width+1 for e in controls),(name,controls)
        assert browser.config()==before and status(browser)=='improved'
        browser.screenshot('recommend-'+name+'.png')
        layouts[name]={**layout,'recommendControls':controls}
        if name=='mobile-390':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
            browser.screenshot('recommend-mobile-scene.png')
    result['layouts']=layouts
    result['responsivePreviewPreservesLayoutAndRecommendedMetrics']=True


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-camera-recommend-qa-20261024'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'scope':'Real Firefox/Three WebGL and native dedicated Worker; independent exact-grid geometry; isolated loopback fixture, synthetic accounts, private display; no production or hardware access','pass':False}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        prepare_reference()
        fixtures=json.loads((ARTIFACTS/'optimizer-fixtures.json').read_text())
        downloads=tempfile.TemporaryDirectory(prefix='recommend-download-',dir=profile)
        password=secrets.token_urlsafe(24)
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':91','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'recommend-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':91','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':91',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'recommend-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19219','--profile-root',str(profile)],'recommend-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated browser services did not start')
        shared.drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':downloads.name,'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=Path(downloads.name);browser.all_errors=[];browser.all_shader_errors=[]
        result['browser']={key:created['capabilities'].get(key) for key in ('browserName','browserVersion','platformName')}
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':preload_source()})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':"const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));",'args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.evaluate('localStorage.removeItem('+json.dumps(STORAGE)+');localStorage.removeItem('+json.dumps(BASELINE)+');sessionStorage.removeItem("__sharedFixtureSeed");')
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'})
        browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
        time.sleep(.35);initial=browser.config();fixed.assert_config(initial);shared.assert_mesh(browser,initial)
        result['webgl']=browser.evaluate('return {contexts:__coverageQA.contexts,drawCalls:__coverageQA.drawCalls}')
        verify_preview_apply_undo(browser,initial,result,'fiveCameras')
        verify_worker_lifecycle(browser,initial,result)
        verify_preview_apply_undo(browser,fixtures['single'],result,'singleCamera')
        verify_preview_apply_undo(browser,fixtures['board'],result,'sharedBoardWithDisabledPartner')
        verify_invalidation_and_empty(browser,fixtures['single'],initial,result)
        verify_responsive(browser,fixtures['single'],result)
        verify_auth_assets(browser,result)
        refine.errors(browser);result['errors']=browser.all_errors;result['shaderErrors']=browser.all_shader_errors
        polish.verify_loaded_assets(browser,result)
        assert result['sourceDigests']==starting,'Runtime source changed during final browser acceptance'
        shared.verify_checked_build(result)
        result['nativeWorkerTrace']=browser.evaluate("return __cameraQA.events.map(e=>e.type==='received'&&e.data.type==='result'?{...e,data:{...e.data,result:{status:e.data.result.status,before:e.data.result.before,after:e.data.result.after,movedIds:e.data.result.movedIds,evaluations:e.data.result.evaluations}}}:e.type==='posted'?{...e,data:{type:e.data.type,generation:e.data.generation,keyShaLength:e.data.key.length}}:e)")
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('camera-recommend-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['failureConfig']=browser.config();result['failureScene']=shared.board_state(browser)
                result['workerFailureTrace']=browser.evaluate('return __cameraQA.events')
                result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
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
        if downloads:downloads.cleanup()
        print(json.dumps({'pass':result['pass'],'failure':result.get('failure'),'report':str(ARTIFACTS/'final-browser-report.json')},ensure_ascii=False))


if __name__=='__main__':
    serve() if '--serve' in sys.argv else verify()
