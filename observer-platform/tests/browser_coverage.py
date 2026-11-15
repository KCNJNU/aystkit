"""Real Firefox acceptance: isolated backend, ephemeral account, real WebGL.

Uses no robot data, production account, remote host or production credential.
Run with the project Python venv. Assets use the maintained frontend; backend
uses an independently isolated baseline when COVERAGE_QA_BACKEND_ROOT is set.
"""
from __future__ import annotations
import asyncio
import base64
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
import websockets

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.local/state/robot-cloud/coverage-20261023'
ARTIFACTS = Path(os.environ.get('COVERAGE_QA_ARTIFACTS', str(STATE / 'artifacts')))
BACKEND = Path(os.environ.get('COVERAGE_QA_BACKEND_ROOT', str(STATE / 'work')))
SERVER, DRIVER = 'http://127.0.0.1:19118', 'http://127.0.0.1:19119'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STORAGE = 'observer.sensor-coverage.v1'
PRELOAD = r'''() => {
 window.__coverageQA={errors:[],contexts:[],drawCalls:0};
 addEventListener('error',e=>__coverageQA.errors.push(String(e.message)));
 addEventListener('unhandledrejection',e=>__coverageQA.errors.push(String(e.reason)));
 const originalError=console.error.bind(console);
 console.error=(...args)=>{__coverageQA.errors.push(args.map(String).join(' '));originalError(...args)};
 const getContext=HTMLCanvasElement.prototype.getContext;
 HTMLCanvasElement.prototype.getContext=function(...args){
  const context=getContext.apply(this,args);
  if(context&&/^webgl/.test(args[0])&&!context.__coverageTracked){
   context.__coverageTracked=true;
   __coverageQA.contexts.push({kind:args[0],version:context.getParameter(context.VERSION),renderer:context.getParameter(context.RENDERER)});
   for(const name of ['drawArrays','drawElements','drawArraysInstanced','drawElementsInstanced']){
    const draw=context[name];if(draw)context[name]=function(...parameters){__coverageQA.drawCalls++;return draw.apply(this,parameters)};
   }
  }return context;
 };
}'''


def request(url, method='GET', data=None, timeout=20):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def serve():
    sys.path.insert(0, str(BACKEND if BACKEND.exists() else ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config, hash_password
    with tempfile.TemporaryDirectory(prefix='coverage-browser-data-') as directory:
        config = Config(robot_id='coverage-browser-fixture', device_credentials=[{
            'token': secrets.token_urlsafe(36), 'robot_id': 'coverage-browser-fixture',
            'source_id': 'fixture', 'role': 'primary'}],
            viewer_username='coverage-fixture', viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),
            session_secret=secrets.token_urlsafe(48), public_origin=SERVER, data_dir=directory,
            allow_insecure_local=True, frontend_dir=str(ROOT / 'cloud/frontend'))
        web.run_app(create_app(config), host='127.0.0.1', port=19118, access_log=None)


class Browser:
    def __init__(self, created):
        self.session, self.websocket = created['sessionId'], created['capabilities']['webSocketUrl']

    def call(self, endpoint, data=None, method='POST'):
        return request(DRIVER + '/session/' + self.session + endpoint, method, data)['value']

    def evaluate(self, script):
        return self.call('/execute/sync', {'script': script, 'args': []})

    def wait(self, expression, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = self.evaluate('return (' + expression + ')')
            if value:
                return value
            time.sleep(.15)
        raise AssertionError('Timed out: ' + expression)

    async def bidi(self, commands):
        async with websockets.connect(self.websocket, proxy=None) as socket:
            results = []
            for identifier, (method, params) in enumerate(commands, 1):
                await socket.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
                while True:
                    response = json.loads(await socket.recv())
                    if response.get('id') == identifier:
                        if response.get('type') == 'error':
                            raise RuntimeError(response)
                        results.append(response['result'])
                        break
            return results

    def viewport(self, width, height):
        tree = asyncio.run(self.bidi([('browsingContext.getTree', {})]))[0]
        asyncio.run(self.bidi([('browsingContext.setViewport', {
            'context': tree['contexts'][0]['context'], 'viewport': {'width': width, 'height': height},
            'devicePixelRatio': 1})]))

    def screenshot(self, name):
        (ARTIFACTS / name).write_bytes(base64.b64decode(self.call('/screenshot', method='GET')))

    def config(self):
        return self.evaluate('return JSON.parse(localStorage.getItem(' + json.dumps(STORAGE) + '))')

    def button(self, text):
        self.evaluate('''const text=TEXT;const button=[...document.querySelectorAll('button')]
          .find(e=>e.textContent.trim()===text);if(!button)throw Error('Missing button: '+text);button.click();'''.replace('TEXT', json.dumps(text)))

    def sensor(self, identifier):
        self.evaluate('''const id=IDENTIFIER;const button=[...document.querySelectorAll('.sensor-select')]
          .find(e=>e.textContent.toLowerCase().includes(id.toLowerCase()));if(!button)throw Error('Missing sensor: '+id);
          if(button.getAttribute('aria-expanded')!=='true')button.click();'''.replace('IDENTIFIER', json.dumps(identifier)))
        self.wait("document.querySelector('.sensor-card.is-open .sensor-body')")

    def field(self, label, value, scope='.sensor-card.is-open'):
        script = '''const root=document.querySelector(SCOPE);const label=[...root.querySelectorAll('label.numeric-field')]
          .find(e=>e.querySelector('.field-label')?.childNodes[0]?.textContent.trim()===LABEL);
          if(!label)throw Error('Missing numeric field: '+LABEL);const input=label.querySelector('input');
          const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          input.focus();setter.call(input,String(VALUE));input.dispatchEvent(new Event('input',{bubbles:true}));
          return true;'''
        self.evaluate(script.replace('SCOPE', json.dumps(scope)).replace('LABEL', json.dumps(label)).replace('VALUE', json.dumps(value)))
        return self.evaluate('const input=document.activeElement;input.blur();return Number(input.value);')

    def import_config(self, value):
        self.evaluate('''const input=document.querySelector('input[type=file]');const transfer=new DataTransfer();
          transfer.items.add(new File([PAYLOAD],'fixture.json',{type:'application/json'}));input.files=transfer.files;
          input.dispatchEvent(new Event('change',{bubbles:true}));'''.replace('PAYLOAD', json.dumps(json.dumps(value))))

    def layout(self):
        return self.evaluate('''return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,
          canvas:[...document.querySelectorAll('canvas')].map(e=>{const r=e.getBoundingClientRect();return {width:r.width,height:r.height}}),
          overflow:[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return r.width&&r.right>innerWidth+2&&s.position!=='fixed'&&s.visibility!=='hidden'})
          .slice(0,8).map(e=>({tag:e.tagName,class:String(e.className),right:e.getBoundingClientRect().right}))};''')


def verify_page(browser, result):
    browser.wait("document.querySelectorAll('.sensor-card').length===6")
    browser.wait('__coverageQA.drawCalls>0')
    time.sleep(.7)
    result['initialText'] = browser.evaluate('return document.body.innerText')
    result['initialRender'] = browser.evaluate('return __coverageQA')
    result['loadedAssets'] = browser.evaluate("return performance.getEntriesByType('resource').map(e=>e.name).filter(name=>name.includes('/coverage-assets/'))")
    assert result['initialRender']['contexts'] and result['initialRender']['drawCalls'] > 0
    assert not result['initialRender']['errors'], result['initialRender']['errors']
    browser.screenshot('coverage-desktop-initial.png')
    initial = browser.config()
    assert [s['id'] for s in initial['sensors']] == ['camera1', 'camera2', 'camera3', 'camera4', 'mid360', 'p100r']
    baseline = browser.evaluate("return Number(document.querySelector('.coverage-metric strong').textContent)")
    assert 0 < baseline <= 100, baseline
    result['initialCoveragePercent'] = baseline
    for index, sensor in enumerate(initial['sensors']):
        identifier = sensor['id']
        browser.sensor(identifier)
        for label, key, value in [('X · 前后','x',-.6+index*.15),('Y · 左右','y',.12+index*.02),
                ('Z · 高度','z',1+index*.03),('向下倾斜','pitch',30+index),
                ('水平视野','hfov',300 if identifier=='mid360' else 90),('垂直视野','vfov',58+index),
                ('最小距离','minRange',.15),('查看距离' if sensor['kind']=='camera' else '最大距离','maxRange',2.5)]:
            before = browser.evaluate('return __coverageQA.drawCalls')
            displayed = browser.field(label, value)
            assert abs(displayed-value)<1e-6, (identifier,label,displayed,value)
            browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).sensors[{index}].{key}==={value}")
            browser.wait(f'__coverageQA.drawCalls>{before}')
        browser.evaluate("const select=document.querySelector('.sensor-card.is-open select');select.value='custom';select.dispatchEvent(new Event('change',{bubbles:true}));")
        browser.wait("[...document.querySelectorAll('.sensor-card.is-open .field-label')].some(e=>e.textContent.startsWith('自定义偏航角'))")
        browser.field('自定义偏航角',45+index)
        browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).sensors[{index}].yaw==={45+index}")
    result['allSensorControlsAndCustomYaw'] = True
    edited = browser.config()
    browser.sensor('camera1')
    for label,key,value,bound in [('X · 前后','x',100,6),('Z · 高度','z',100,4),
            ('水平视野','hfov',500,179),('查看距离','maxRange',100,30)]:
        displayed=browser.field(label,value)
        assert displayed==bound,(label,displayed,bound)
        browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).sensors[0].{key}==={bound}")
        browser.field(label,edited['sensors'][0][key])
    assert browser.config()==edited
    result['boundsClampVisibleAndCalculatedValues']=True
    assert not browser.evaluate('return __coverageQA.errors')
    browser.call('/refresh',{})
    browser.wait("document.querySelectorAll('.sensor-card').length===6&&__coverageQA.drawCalls>0")
    assert browser.config()==edited
    result['persistentAfterReload']=True
    browser.evaluate("document.querySelectorAll('.sensor-card button[role=switch][aria-checked=true]').forEach(button=>button.click())")
    browser.wait("document.querySelector('.coverage-metric strong').textContent==='0.0'")
    stats=browser.evaluate("return [...document.querySelectorAll('.metrics article strong')].map(e=>Number(e.textContent))")
    assert stats[0]==stats[2]==0 and abs(stats[1]-stats[3])<.011, stats
    assert all(not s['enabled'] for s in browser.config()['sensors'])
    result['allOffStatistics']=stats
    browser.screenshot('coverage-all-sensors-off.png')
    isolated=json.loads(json.dumps(edited))
    for sensor in isolated['sensors']:
        sensor['enabled']=sensor['id']=='camera1'
    isolated['sensors'][0]['hfov']=110
    browser.import_config({'version':1,'config':isolated})
    browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).sensors[0].hfov===110")
    browser.sensor('camera1')
    broad=browser.evaluate("return Number(document.querySelector('.coverage-metric strong').textContent)")
    browser.field('水平视野',20)
    browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).sensors[0].hfov===20")
    narrow=browser.evaluate("return Number(document.querySelector('.coverage-metric strong').textContent)")
    assert 0<narrow<broad, (narrow,broad)
    result['fovChangesStatistics']={'wide110':broad,'narrow20':narrow}
    browser.import_config({'version':1,'config':edited})
    browser.wait("document.querySelector('[role=status]')?.textContent.includes('已导入')")
    assert browser.config()==edited
    browser.import_config({'version':99,'config':initial})
    browser.wait("document.querySelector('[role=status]')?.textContent.includes('导入失败')")
    assert browser.config()==edited
    for invalid in [None,{'version':1,'config':None}]:
        browser.import_config(invalid)
        browser.wait("document.querySelector('[role=status]')?.textContent.includes('导入失败')")
        assert browser.config()==edited
        message=browser.evaluate("return document.querySelector('[role=status]').textContent")
        assert not any(word in message for word in ['TypeError','Cannot read','Unexpected','can’t access','can\'t access']),message
    result['importValidation']=True
    browser.evaluate('''const create=URL.createObjectURL.bind(URL);URL.createObjectURL=blob=>{
       blob.text().then(text=>window.__coverageExport=JSON.parse(text));return create(blob)};
       const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){
       if(this.download){window.__coverageExportFilename=this.download;return;}return click.call(this)};''')
    browser.button('导出配置')
    browser.wait('window.__coverageExport?.version===1')
    assert browser.evaluate('return __coverageExport.config')==edited
    assert browser.evaluate('return __coverageExportFilename')=='传感器覆盖配置.json'
    result['exportRoundtrip']=True
    browser.evaluate("[...document.querySelectorAll('details')].find(e=>e.textContent.includes('分析区域与统计')).open=true")
    browser.field('高度切片',.6,'.additional-settings')
    browser.wait("document.querySelector('.slice-caption b').textContent==='0.60'")
    for label,value in [('区域长度 · X',7),('区域宽度 · Y',5),('网格精度',.08)]:
        browser.field(label,value,'.additional-settings')
    browser.evaluate("[...document.querySelectorAll('details')].find(e=>e.textContent.includes('底盘与铝合金架')).open=true")
    for label,value in [('底盘长度',1.2),('底盘宽度',.7),('底盘高度',.3),('框架高度',1.1),('框架宽度',.6)]:
        browser.field(label,value,'.additional-settings')
    assert browser.config()['analysis']['length']==7 and browser.config()['chassis']['length']==1.2
    result['analysisAndChassisControls']=True
    browser.button('俯视图')
    browser.wait("document.querySelector('.view-switch button.active').textContent.includes('俯视图')")
    time.sleep(.4)
    browser.screenshot('coverage-top-view.png')
    browser.button('三维视角')
    browser.button('恢复默认')
    browser.wait(f"JSON.parse(localStorage.getItem('{STORAGE}')).analysis.length===6")
    assert browser.config()==initial
    result['viewAndDefaultControls']=True
    browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0")
    time.sleep(.3)
    result['layouts']={'desktop':browser.layout()}
    browser.screenshot('coverage-desktop-final.png')
    browser.viewport(390,844)
    time.sleep(.5)
    result['layouts']['mobile']=browser.layout()
    browser.screenshot('coverage-mobile.png')
    for size,layout in result['layouts'].items():
        assert layout['scrollWidth']<=layout['width']+2 and not layout['overflow'],(size,layout)
    result['errors']=browser.evaluate('return __coverageQA.errors')
    assert not result['errors'],result['errors']
    verify_navigation(browser,result)


def verify_navigation(browser,result):
    browser.call('/url',{'url':SERVER+'/#robot/sensors'})
    browser.wait("window.RKObserverDashboard&&RKObserverDashboard.getRoute()==='robot/sensors'")
    browser.wait("document.querySelector('#page-robot-sensors a[href=\"/coverage\"], .page.active a[href=\"/coverage\"]')")
    nav=browser.evaluate('''return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
      buttons:[...document.querySelectorAll('.sidebar nav .nav,.sidebar nav .coverage-link')].map(e=>{const r=e.getBoundingClientRect();return {text:e.innerText,left:r.left,right:r.right}})}''')
    assert nav['scrollWidth']<=nav['width']+2,nav
    assert all(-2<=e['left'] and e['right']<=nav['width']+2 for e in nav['buttons']),nav
    assert len(nav['buttons'])==8,nav
    result['existingObservationMobileNavigation']=nav
    browser.screenshot('observation-mobile-coverage-link.png')
    assert not browser.evaluate('return __coverageQA.errors')


def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-coverage-qa-20261023'
    profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[]
    browser=None
    result={'scope':'Isolated loopback backend and ephemeral account; no robot or production credentials','pass':False}
    password=secrets.token_urlsafe(24)
    try:
        env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=Path('/run/user/1000/gdm/Xauthority')
        if not env.get('DISPLAY') and Path('/tmp/.X11-unix/X1').exists() and authority.exists():
            env.update(DISPLAY=':1',XAUTHORITY=str(authority))
        for command,name in [([sys.executable,__file__,'--serve'],'coverage-server'),
                (['/snap/bin/geckodriver','--host','127.0.0.1','--port','19119','--profile-root',str(profile)],'coverage-geckodriver')]:
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            try:
                request(SERVER+'/healthz');request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated backend/geckodriver did not start')
        created=request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{
            'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{
                'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],
                'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,
                         'gfx.webrender.software':True,'webgl.enable-software-renderer':True}}}}},timeout=60)['value']
        browser=Browser(created)
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':PRELOAD})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':'''const done=arguments[arguments.length-1];
          fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({username:arguments[0],password:arguments[1]})})
          .then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));''',
            'args':['coverage-fixture',password]})
        assert login['status']==200,login
        browser.viewport(1440,960)
        if '--nav-only' in sys.argv:
            browser.viewport(390,844)
            verify_navigation(browser,result)
            result['mode']='navigation-only'
        else:
            browser.call('/url',{'url':SERVER+'/coverage'})
            verify_page(browser,result)
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('coverage-failure.png')
                result['failureText']=browser.evaluate('return document.body.innerText')
                result['renderAtFailure']=browser.evaluate('return window.__coverageQA')
            except Exception as capture_error:result['captureFailure']=str(capture_error)
        raise
    finally:
        (ARTIFACTS/('browser-navigation-report.json' if '--nav-only' in sys.argv else 'browser-coverage-report.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
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
