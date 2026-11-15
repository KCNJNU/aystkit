"""Targeted real Firefox visual acceptance for red blind areas and clear coverage classes."""
from __future__ import annotations
import asyncio, copy, hashlib, json, os, re, secrets, subprocess, sys, tempfile, time
from pathlib import Path
from aiohttp import web
from PIL import Image
import browser_coverage_recommend as recommendation

refine, shared = recommendation.refine, recommendation.shared
qa, polish, fixed = recommendation.qa, recommendation.polish, recommendation.fixed
Browser = recommendation.Browser
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get('COVERAGE_COLORS_ARTIFACTS', '/tmp/coverage-colors-20261024/artifacts'))
SERVER, DRIVER = 'http://127.0.0.1:19228', 'http://127.0.0.1:19229'
for module in (recommendation, refine, fixed, shared, shared.qa, shared.drag, shared.frame, shared.polish, shared.photo):
    module.ROOT, module.ARTIFACTS, module.SERVER, module.DRIVER = ROOT, ARTIFACTS, SERVER, DRIVER
STORAGE, BASELINE = shared.STORAGE, shared.BASELINE


def serve():
    sys.path.insert(0, str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-colors-browser-') as directory:
        config=Config(robot_id='coverage-color-fixture', device_credentials=[{'token': secrets.token_urlsafe(36), 'robot_id':'coverage-color-fixture','source_id':'fixture','role':'primary'}], viewer_username='coverage-fixture',viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),session_secret=secrets.token_urlsafe(48),public_origin=SERVER,data_dir=directory,allow_insecure_local=True,frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19228, access_log=None)


def capture_colors(browser, name):
    path=ARTIFACTS/(name+'.png');browser.screenshot(path.name)
    bounds=browser.evaluate("const r=document.querySelector('.three-host canvas').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom}")
    image=Image.open(path).convert('RGB');width,height=image.size
    box=(max(0,int(bounds['left'])+2),max(0,int(bounds['top'])+2),min(width,int(bounds['right'])-2),min(height,int(bounds['bottom'])-2))
    colors=list(image.crop(box).getdata())
    counts={
        'red':sum(r>75 and r>g*1.7 and r>b*1.6 for r,g,b in colors),
        'cyanGreen':sum(g>65 and g>r*1.25 and b>r*1.25 and g>=b*.9 for r,g,b in colors),
        'violet':sum(b>75 and b>r*1.1 and b>g*1.25 for r,g,b in colors),
        'pixels':len(colors),'canvasBounds':box,
    }
    return counts


def verify_visuals(browser,initial,result):
    reference=recommendation.node_reference(initial)
    fixture={'config':initial,'stats':reference['overall']}
    invariant_names=('geometry.ts','surface.ts','model.ts','frame.ts','optimizer.ts','optimizer.worker.ts','useCameraRecommendation.ts','Scene.tsx')
    before=json.loads((ROOT.parent/'maintained-source-before.json').read_text())
    for name in invariant_names:
        relative='coverage-config/src/'+name
        assert hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==before[relative],relative
    result['mathMountingRecommendationAndSceneSourcesUnchanged']={name:before['coverage-config/src/'+name] for name in invariant_names}
    result['defaultSurface']=refine.check_surface(browser,fixture)
    legend=browser.evaluate("return Object.fromEntries(['covered','blind','overlap'].map(name=>{const e=document.querySelector('.legend .'+name),s=getComputedStyle(e);return [name,{background:s.backgroundColor,image:s.backgroundImage,border:s.borderColor,width:s.width,height:s.height}]}))")
    blind=re.findall(r'rgb\((\d+), (\d+), (\d+)\)',legend['blind']['image']);assert blind,legend
    red,green,blue=map(int,blind[0]);assert red>200 and red>green*2 and red>blue*2,legend
    assert 'gradient' in legend['blind']['image'],legend
    assert len({legend[n]['background'] for n in legend})==3
    metric=browser.evaluate("const e=document.querySelector('.blind-metric');return {number:getComputedStyle(e.querySelector('strong')).color,dot:getComputedStyle(e.querySelector('.status-dot')).backgroundColor}")
    result['legendAndBlindMetricStyles']={'legend':legend,'blindMetric':metric}
    before_config=browser.config();before_metrics=browser.metrics()
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    result['perspectiveColorPixels']=capture_colors(browser,'colors-default-perspective')
    browser.clickid('view-top');refine.checkbox(browser,'volume-toggle',False);time.sleep(.3)
    top=capture_colors(browser,'colors-default-top')
    assert top['red']>1000 and top['cyanGreen']>1000 and top['violet']>1000,top
    result['topColorPixels']=top
    assert browser.config()==before_config and browser.metrics()==before_metrics
    refine.checkbox(browser,'overlap-toggle',False)
    result['overlapOffSurface']=refine.check_surface(browser,fixture,overlap=False)
    assert browser.metrics()==before_metrics and browser.config()==before_config
    refine.checkbox(browser,'overlap-toggle',True);refine.check_surface(browser,fixture)
    allblind=copy.deepcopy(initial)
    for sensor in allblind['sensors']:sensor['enabled']=False
    canonical=recommendation.imported(browser,allblind)
    blind_ref=recommendation.node_reference(canonical)
    result['allBlindSurface']=refine.check_surface(browser,{'config':canonical,'stats':blind_ref['overall']})
    assert blind_ref['overall']['covered']==0 and blind_ref['overall']['blind']==blind_ref['overall']['total']
    browser.clickid('view-top');time.sleep(.3)
    blind_pixels=capture_colors(browser,'colors-all-blind-top')
    assert blind_pixels['red']>blind_pixels['pixels']*.10,blind_pixels
    result['allBlindColorPixels']=blind_pixels
    recommendation.imported(browser,initial);browser.clickid('view-perspective');refine.checkbox(browser,'volume-toggle',True)
    layouts={}
    for name,width,height in (('desktop-1440',1440,960),('mobile-390',390,844)):
        browser.viewport(width,height);browser.evaluate('scrollTo(0,0)');time.sleep(.2)
        layout=browser.layout();assert layout['scrollWidth']<=width+2 and not layout['overflow'],layout
        browser.screenshot('colors-'+name+'.png');layouts[name]=layout
        if name=='mobile-390':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})");time.sleep(.2);browser.screenshot('colors-mobile-scene.png')
        assert browser.config()==initial and browser.metrics()==before_metrics
    result['layouts']=layouts
    browser.viewport(1440,960);browser.evaluate('scrollTo(0,0)')
    recommendation.start(browser)
    recommendation.verify_auth_assets(browser,result)
    refine.errors(browser);result['errors']=browser.all_errors;result['shaderErrors']=browser.all_shader_errors
    polish.verify_loaded_assets(browser,result);shared.verify_checked_build(result)
    result['presentationAndOverlapToggleDoNotChangeConfigOrMetrics']=True


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-colors-qa-20261024';profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'scope':'Targeted real Firefox/WebGL color and exact surface regression; private display and synthetic loopback accounts; no production/robot access','pass':False}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        recommendation.prepare_reference()
        downloads=tempfile.TemporaryDirectory(prefix='colors-download-',dir=profile)
        password=secrets.token_urlsafe(24);env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':90','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'colors-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':90','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env));env.update(DISPLAY=':90',XAUTHORITY=str(authority));time.sleep(.5)
        for command,name in (([sys.executable,__file__,'--serve'],'colors-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19229','--profile-root',str(profile)],'colors-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log);processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated browser services did not start')
        shared.drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':downloads.name,'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=Path(downloads.name);browser.all_errors=[];browser.all_shader_errors=[]
        result['browser']={key:created['capabilities'].get(key) for key in ('browserName','browserVersion','platformName')}
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':recommendation.preload_source()})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':"const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));",'args':['coverage-fixture',password]});assert login['status']==200,login
        browser.evaluate('localStorage.removeItem('+json.dumps(STORAGE)+');localStorage.removeItem('+json.dumps(BASELINE)+');sessionStorage.removeItem("__sharedFixtureSeed");')
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'});browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0");time.sleep(.3)
        initial=browser.config();fixed.assert_config(initial);shared.assert_mesh(browser,initial)
        result['webgl']=browser.evaluate('return {contexts:__coverageQA.contexts,drawCalls:__coverageQA.drawCalls}')
        verify_visuals(browser,initial,result)
        assert result['sourceDigests']==starting,'Source changed during visual acceptance'
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:browser.screenshot('coverage-colors-failure.png');result['failureText']=browser.evaluate('return document.body.innerText');result['failureScene']=shared.board_state(browser);result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
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

if __name__=='__main__':serve() if '--serve' in sys.argv else verify()
