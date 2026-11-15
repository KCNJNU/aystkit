"""Real Firefox acceptance for the quieter, classified-glass coverage view.

Math references were captured before the presentation changes. Uses only an
isolated fixture, synthetic layouts, ephemeral account and a private display.
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
import browser_coverage_plate_fixed as fixed

shared=fixed.shared
ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=Path(os.environ.get('COVERAGE_REFINE_ARTIFACTS','/tmp/coverage-refine-20261024/artifacts'))
SERVER,DRIVER='http://127.0.0.1:19208','http://127.0.0.1:19209'
qa,drag,polish=shared.qa,shared.drag,shared.polish
STORAGE,BASELINE=shared.STORAGE,shared.BASELINE
Browser=shared.Browser
for module in (fixed,shared,shared.qa,shared.drag,shared.frame,shared.polish,shared.photo):
    module.ROOT,module.ARTIFACTS,module.SERVER,module.DRIVER=ROOT,ARTIFACTS,SERVER,DRIVER

original_errors=shared.errors


def errors(browser):
    original_errors(browser)
    current=browser.evaluate('return __coverageQA.shaderErrors||[]')
    browser.all_shader_errors.extend(current)
    assert not current,current


shared.errors=errors


def serve():
    sys.path.insert(0,str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config,hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-refine-browser-') as directory:
        config=Config(robot_id='coverage-refine-fixture',device_credentials=[{'token':secrets.token_urlsafe(36),
            'robot_id':'coverage-refine-fixture','source_id':'fixture','role':'primary'}],
            viewer_username='coverage-fixture',viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48),public_origin=SERVER,data_dir=directory,
            allow_insecure_local=True,frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config),host='127.0.0.1',port=19208,access_log=None)


def preload_source():
    seeding="""const fixtureSeed=sessionStorage.getItem('__sharedFixtureSeed');if(fixtureSeed){const seed=JSON.parse(fixtureSeed);localStorage.setItem('observer.sensor-coverage.v1',JSON.stringify(seed.current));localStorage.setItem('observer.sensor-coverage.baseline.v1',JSON.stringify(seed.baseline));sessionStorage.removeItem('__sharedFixtureSeed');}addEventListener('pointerdown',e=>window.__dragPointerId=e.pointerId,true);window.__coverageQA="""
    source=qa.PRELOAD.replace('window.__coverageQA=',seeding).replace('errors:[],contexts:[],drawCalls:0','errors:[],shaderErrors:[],contexts:[],drawCalls:0')
    tracking="""const compile=context.compileShader;context.compileShader=function(shader){compile.call(this,shader);if(!this.getShaderParameter(shader,this.COMPILE_STATUS))__coverageQA.shaderErrors.push(this.getShaderInfoLog(shader)||'Shader compilation failed')};
    const link=context.linkProgram;context.linkProgram=function(program){link.call(this,program);if(!this.getProgramParameter(program,this.LINK_STATUS))__coverageQA.shaderErrors.push(this.getProgramInfoLog(program)||'Program link failed')};
    for(const name of ['drawArrays'"""
    return source.replace("for(const name of ['drawArrays'",tracking)


def checkbox(browser,testid,value):
    browser.evaluate("const e=document.querySelector("+json.dumps('[data-testid="'+testid+'"]')+");if(e.checked!=="+json.dumps(value)+")e.click();")
    time.sleep(.2)


def scope(browser,value):
    browser.evaluate("const e=document.querySelector('[data-testid=volume-scope]');e.value="+json.dumps(value)+";e.dispatchEvent(new Event('change',{bubbles:true}));")
    time.sleep(.25)


def state(browser):
    return shared.board_state(browser)


def expected_metrics(stats):
    return [stats['percent'],stats['blindArea'],stats['overlap']/stats['total']*100 if stats['total'] else 0,stats['area']]


def check_surface(browser,fixture,overlap=True):
    config,stats=fixture['config'],fixture['stats']
    actual=state(browser)
    assert actual['coverageStyle']=='classified-glass-v1' and actual['renderMode']=='on-demand',actual
    expected={'blind':stats['blind'],'covered':stats['covered']-(stats['overlap'] if overlap else 0),'overlap':stats['overlap'] if overlap else 0}
    assert actual['surfaceClassCounts']==expected,(actual['surfaceClassCounts'],expected)
    assert actual['surfaceCellCount']==stats['total']
    assert actual['surfaceVertexCount']==stats['total']*6
    assert actual['surfaceBoundaryVertexCount']==actual['surfaceBoundarySegments']*2
    shared.near(actual['surfaceHeight'],config['analysis']['height'])
    if stats['total']:
        for key in ('surfaceHeightRange','surfaceBoundaryHeightRange'):
            shared.near(actual[key]['min'],config['analysis']['height'],2e-6)
            shared.near(actual[key]['max'],config['analysis']['height'],2e-6)
        assert 0<actual['surfaceBoundarySegments']<stats['total']*4
    else:
        assert actual['surfaceBoundarySegments']==0
    for found,target,tolerance in zip(browser.metrics(),expected_metrics(stats),(.051,.0051,.051,.0051)):
        shared.near(found,target,tolerance)
    return {k:actual[k] for k in ('surfaceClassCounts','surfaceCellCount','surfaceVertexCount','surfaceBoundarySegments','surfaceHeight')}


def verify_default_and_folds(browser,initial,result):
    defaults=browser.evaluate("return {layoutOpen:document.querySelector('[data-testid=layout-fold]').open,plateOpen:document.querySelector('[data-testid=front-plate-editor]').open,volume:document.querySelector('[data-testid=volume-toggle]').checked,scope:document.querySelector('[data-testid=volume-scope]').value,labels:document.querySelector('[data-testid=labels-toggle]').checked,overlap:document.querySelector('[data-testid=overlap-toggle]').checked}")
    assert defaults=={'layoutOpen':False,'plateOpen':False,'volume':True,'scope':'selected','labels':False,'overlap':True},defaults
    current=state(browser)
    assert current['drawnVolumeIds']==['camera1'] and current['labelIds']==['camera1'],current
    assert current['sensorPointCount']==8 and current['frameRailCount']==18 and current['aluminumMemberCount']==16 and current['plateMountCount']==1
    browser.screenshot('refine-default-perspective.png')
    browser.clickid('view-top');time.sleep(.3);browser.screenshot('refine-default-top.png')
    browser.clickid('view-perspective');time.sleep(.2)
    model=browser.evaluate("const d=document.querySelector('[data-testid=model-info]');return {open:d.open,text:d.querySelector('p').textContent}")
    assert model['open'] is False and model['text'].strip()
    browser.evaluate("document.querySelector('[data-testid=model-info]>summary').click()")
    assert browser.evaluate("return document.querySelector('[data-testid=model-info]').open")
    browser.evaluate("document.querySelector('[data-testid=model-info]').open=false")
    selection=[]
    for identifier in ('camera1','camera2','p100r','bluesea'):
        window_y=browser.evaluate('return scrollY')
        browser.sensor(identifier)
        browser.wait("(()=>{const p=document.querySelector('.panel-scroll').getBoundingClientRect(),e=document.querySelector('.sensor-card.is-open').getBoundingClientRect();return e.top>=p.top-1&&e.top<=p.top+50})()")
        assert browser.evaluate('return scrollY')==window_y
        assert browser.config()==initial
        source=browser.evaluate("const d=document.querySelector('.source-note');return {open:d.open,body:d.querySelector('p').textContent}")
        assert source['open'] is False and source['body'].strip()
        browser.evaluate("document.querySelector('.source-note').open=true")
        assert browser.evaluate("return document.querySelector('.source-note').open")
        browser.evaluate("document.querySelector('.source-note').open=false")
        selection.append(identifier)
    browser.clickid('select-front-plate');time.sleep(.3)
    assert browser.evaluate("return document.querySelector('[data-testid=front-plate-editor]').open")
    assert set(state(browser)['drawnVolumeIds'])=={'camera2','p100r'} and state(browser)['labelIds']==[]
    assert browser.config()==initial
    # Opening a hidden overview remains an intentional action.
    browser.evaluate("document.querySelector('[data-testid=layout-fold]>summary').click()")
    assert browser.evaluate("return document.querySelector('[data-testid=layout-fold]').open")
    browser.clickid('layout-map-camera2');browser.wait("document.querySelector('[data-testid=sensor-camera2-rail]')")
    browser.evaluate("document.querySelector('[data-testid=layout-fold]').open=false")
    before=browser.config()
    browser.evaluate("for(const e of document.querySelectorAll('.sensor-card input[type=number]')){if(!e.disabled){e.focus({preventScroll:true});e.blur();}}")
    time.sleep(.3);assert browser.config()==before
    visible_explanations=browser.evaluate("return [...document.querySelectorAll('.field-note,.plate-note,.mount-hint,.orbit-help,.estimate-label')].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden').map(e=>e.textContent)")
    assert not visible_explanations,visible_explanations
    result['defaultViewAndCollapsedPanelsReducePersistentTextWithoutHidingControls']={'defaults':defaults,'editorSelections':selection,'visibleObsoleteExplanations':visible_explanations}
    result['numericFocusAndBlurPreservesExactCanonicalLayout']=True
    browser.sensor('camera1');browser.clickid('view-mounting');time.sleep(.3)
    browser.screenshot('refine-default-selected-view.png')


def verify_math_and_display(browser,initial,result):
    fixtures=json.loads((ARTIFACTS/'pre-refine-math-reference.json').read_text())
    before=json.loads((ARTIFACTS/'pre-refine-provenance.json').read_text())
    unchanged=('coverage-config/src/geometry.ts','coverage-config/src/model.ts','coverage-config/src/frame.ts')
    for name in unchanged:
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==before[name],('Mathematical or mounting source changed during presentation work',name)
    result['coverageAndMountingSourcesUnchangedFromPreRefineReference']={name:before[name] for name in unchanged}
    outcomes={}
    for name in ('default','raised','empty','scanner','disabledSelected','noCells'):
        fixture=fixtures[name]
        shared.imported(browser,fixture['config'])
        checkbox(browser,'overlap-toggle',True)
        checkbox(browser,'volume-toggle',True);checkbox(browser,'labels-toggle',False);scope(browser,'selected')
        browser.sensor('bluesea' if name=='scanner' else 'camera1')
        surface=check_surface(browser,fixture)
        actual=state(browser)
        if name in ('empty','disabledSelected'):
            assert actual['drawnVolumeIds']==[] and actual['drawnScanIds']==[]
        elif name=='scanner':
            assert actual['drawnVolumeIds']==[] and actual['drawnScanIds']==['bluesea']
        outcomes[name]={'surface':surface,'metrics':browser.metrics(),'oldCounts':fixture['stats'],'volumes':actual['drawnVolumeIds'],'scans':actual['drawnScanIds']}
    fixture=fixtures['default'];shared.imported(browser,fixture['config']);browser.sensor('camera2')
    before_metrics=browser.metrics()
    checkbox(browser,'overlap-toggle',False)
    check_surface(browser,fixture,overlap=False)
    assert browser.metrics()==before_metrics
    checkbox(browser,'overlap-toggle',True);check_surface(browser,fixture)
    # A real slice slider changes only height, then both classifications and
    # metrics must equal the independently captured elevated slice reference.
    browser.evaluate("const e=document.querySelector('.slice-slider input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'.85');e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));")
    time.sleep(.35)
    assert browser.config()==fixtures['raised']['config']
    check_surface(browser,fixtures['raised'])
    assert browser.metrics()!=before_metrics
    result['sameConfigurationStatisticsAndExactSurfaceClassesMatchPreRefineMath']=outcomes
    result['overlapOnlyChangesDisplayClassificationAndSliceChangesGeometryAndMetrics']=True
    shared.imported(browser,initial)


def verify_scope_and_labels(browser,initial,result):
    browser.sensor('camera2');checkbox(browser,'volume-toggle',True);checkbox(browser,'labels-toggle',False);scope(browser,'selected')
    assert state(browser)['drawnVolumeIds']==['camera2'] and state(browser)['labelIds']==['camera2']
    before=browser.config();metrics=browser.metrics()
    scope(browser,'all')
    expected={s['id'] for s in initial['sensors'] if s['enabled'] and s['kind']!='scan'}
    assert set(state(browser)['drawnVolumeIds'])==expected
    checkbox(browser,'labels-toggle',True)
    assert set(state(browser)['labelIds'])=={s['id'] for s in initial['sensors']}
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.screenshot('refine-all-volumes-and-labels.png')
    checkbox(browser,'volume-toggle',False)
    assert state(browser)['drawnVolumeIds']==[] and state(browser)['drawnScanIds']==[]
    assert browser.evaluate("const e=document.querySelector('[data-testid=volume-scope]');return !e||e.disabled")
    checkbox(browser,'volume-toggle',True);scope(browser,'selected');checkbox(browser,'labels-toggle',False)
    assert browser.config()==before and browser.metrics()==metrics
    result['selectedAllAndOffViewsAndShortOrAllLabelsAreIndependentOfCoverageStatistics']=True
    count=state(browser).get('frameCount');time.sleep(.35)
    assert state(browser).get('frameCount')==count,'Scene continues rendering when idle'
    result['sceneRendersOnDemandWithoutContinuousIdleFrames']=True


def verify_baseline_and_critical_regressions(browser,initial,result):
    fixed.verify_each_point_drag(browser,initial,result)
    current=browser.config()
    browser.clickid('set-baseline');time.sleep(.25)
    stored=browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')
    browser.range('plate-lateral',.14)
    moved=browser.config();shared.rigid_move(current,moved)
    assert browser.evaluate('return JSON.parse(localStorage.getItem('+json.dumps(BASELINE)+')).config')==stored
    browser.clickid('restore-baseline');time.sleep(.35);assert browser.config()==current
    shared.verify_real_drag(browser,current,result)
    shared.verify_export(browser,current,result)
    result['plateBaselineRemainsIndependentAndRestoreRecoversExactLayout']=True
    return current


def verify_refined_layouts(browser,current,result):
    layouts={}
    for name,width,height in (('desktop-1440',1440,960),('desktop-1920',1920,1080),('tablet-768',768,1024),('mobile-390',390,844)):
        browser.viewport(width,height);browser.evaluate('scrollTo(0,0)');browser.sensor('camera2');browser.clickid('view-mounting');time.sleep(.35)
        checkbox(browser,'labels-toggle',False);scope(browser,'selected')
        browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
        layout=browser.layout()
        assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        assert state(browser)['drawnVolumeIds']==['camera2'] and state(browser)['labelIds']==['camera2']
        assert browser.config()==current
        browser.screenshot('refine-'+name+'.png')
        layouts[name]=layout
        if name=='mobile-390':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})")
            browser.screenshot('refine-mobile-scene.png')
    result['layouts']=layouts
    # Exact configuration and camera choice from the archived previous UI.
    old=json.loads((ARTIFACTS/'before-visual-configuration.json').read_text())['config']
    shared.imported(browser,old);browser.viewport(1440,960);browser.evaluate('scrollTo(0,0)');browser.sensor('camera2');browser.clickid('view-mounting');time.sleep(.35)
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.screenshot('after-visual-front-camera.png')
    result['visualComparison']={'before':'before-visual-front-camera.png','after':'after-visual-front-camera.png','configuration':'before-visual-configuration.json','sameConfiguration':browser.config()==old,'sensor':'camera2','view':'mounting'}
    assert result['visualComparison']['sameConfiguration']
    errors(browser);result['errors']=browser.all_errors;result['shaderErrors']=browser.all_shader_errors



def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-refine-qa-20261024';profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'scope':'Presentation refinement; pre-change math fixtures, real Firefox/Three WebGL, private display, ephemeral local fixture; no production or robot access','pass':False}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        downloads=tempfile.TemporaryDirectory(prefix='refine-download-',dir=profile)
        password=secrets.token_urlsafe(24)
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':92','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'refine-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':92','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':92',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'refine-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19209','--profile-root',str(profile)],'refine-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated browser services did not start')
        drag.verify_auth(result)
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
        verify_default_and_folds(browser,initial,result)
        verify_math_and_display(browser,initial,result)
        verify_scope_and_labels(browser,initial,result)
        current=verify_baseline_and_critical_regressions(browser,initial,result)
        verify_refined_layouts(browser,current,result)
        polish.verify_loaded_assets(browser,result)
        assert result['sourceDigests']==starting,'Runtime source changed during final browser acceptance'
        shared.verify_checked_build(result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('coverage-refine-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['failureConfig']=browser.config();result['failureScene']=shared.board_state(browser)
                result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        for filename in ('browser-coverage-refine-report.json','final-browser-report.json'):
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
