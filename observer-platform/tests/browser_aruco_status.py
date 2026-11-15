"""Firefox acceptance for isolated ArUco 2D status versus legacy docking pose.

Serves real staged HTML/CSS/JS with an in-tab synthetic socket. No production
server, login, ROS publishers, robot images, calibration or pose are accessed.
"""
import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from aiohttp import web
import websockets

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts' / 'aruco-status'
SERVER = 'http://127.0.0.1:19142'
DRIVER = 'http://127.0.0.1:19144'
FRONTEND = ROOT / 'cloud' / 'frontend'
FIXTURE = r'''
window.__errors=[];window.__mode='available';window.__pause=false;
addEventListener('error',e=>__errors.push(e.message));
addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
window.CloudObserver={ready:Promise.resolve(true),isExpired:()=>false,checkSession:async()=>true,update(){},unauthorized(){}};
function snapshot(){
 const rowStatus=__mode==='stale'?'STALE':'AVAILABLE';
 const keys=['usbFront','usbLeft','usbHub3','usbHub2','color'];
 const labels=['前视相机','后视相机','左视相机','右视相机','深度相机彩色'];
 const identities=[[10,11],[42],[0],[99],[11]];
 const cameras=Object.fromEntries(keys.map((key,index)=>[key,{status:__mode==='oneStale'&&index===0?'STALE':rowStatus,label:labels[index],
  dictionary:'DICT_5X5_100',ids:__mode==='empty'?[]:__mode==='invalid'?[100]:identities[index],ageMs:__mode==='stale'?8000:350}]));
 return {meta:{timestamp:Date.now()/1000,sequence:1},
  cloud:{online:true,status:'AVAILABLE',ageS:.2},rk3588:{status:'AVAILABLE',network:[],thermalZones:[]},
  ros2:{status:'AVAILABLE',topics:{}},robot:{source:'ISOLATED_SYNTHETIC_SOCKET',telemetry:{status:'WAITING_UPSTREAM',hz:0,ageS:null},data:{}},
  devices:{cameras:{},sensors:{},visionDocking:{status:'WAITING_UPSTREAM',targetStatus:'WAITING_UPSTREAM',
   detectedIds:null,boardPose:null,detectionState:null,dockingState:'DISABLED',topics:{},
   arucoPreview:{status:rowStatus,topic:'/observer_camera/aruco_detections',dictionary:'DICT_5X5_100',
    ageMs:__mode==='stale'?8000:200,cameras}}},pages:{},errors:{}};
}
class FixtureSocket extends EventTarget{
 static CONNECTING=0;static OPEN=1;static CLOSED=3;
 constructor(){super();this.readyState=0;window.__fixtureSocket=this;setTimeout(()=>{this.readyState=1;this.dispatchEvent(new Event('open'));this.emit();},20);}
 emit(){if(this.readyState===1&&!__pause)this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(snapshot())}));}
 close(){this.readyState=3;this.dispatchEvent(new Event('close'));}
}
window.WebSocket=FixtureSocket;setInterval(()=>window.__fixtureSocket?.emit(),250);
window.__switch=mode=>{window.__mode=mode;window.__fixtureSocket?.emit();};
'''


async def handle(request):
    name = request.match_info['path']
    if not name:
        html = (FRONTEND / 'index.html').read_text()
        html = html.replace('<title>机器人云端观测平台</title>', '<title>ISOLATED ARUCO STATUS FIXTURE · SYNTHETIC</title>')
        html = html.replace('<script src="/cloud.js"></script>', '<script src="/fixture.js"></script>')
        for removed in ('native-player.js', 'detection-overlay.js', 'media.js'):
            html = html.replace('<script src="/' + removed + '"></script>', '')
        return web.Response(text=html, content_type='text/html')
    if name == 'fixture.js':
        return web.Response(text=FIXTURE, content_type='application/javascript')
    if name not in ('app.css', 'cloud.css', 'formatters.js', 'charts.js', 'app.js'):
        raise web.HTTPNotFound()
    return web.FileResponse(FRONTEND / name)


def serve():
    app = web.Application()
    app.router.add_get('/{path:.*}', handle)
    web.run_app(app, host='127.0.0.1', port=19142, access_log=None)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method='GET', data=None, timeout=15):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def verify():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    profile = Path.home() / 'snap/firefox/common/observer-aruco-status-test'
    profile.mkdir(parents=True, exist_ok=True)
    processes, logs, session = [], [], None
    report = {'scope': 'Synthetic in-tab socket; real staged HTML/CSS/JS; no production credentials, robot or pose', 'layouts': {}}
    try:
        for command, name in (([sys.executable, __file__, '--serve'], 'fixture'),
                              (['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', '19144',
                                '--profile-root', str(profile)], 'driver')):
            log = (ARTIFACTS / (name + '.log')).open('w')
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        end = time.monotonic() + 20
        while time.monotonic() < end:
            try:
                request(DRIVER + '/status')
                break
            except (OSError, ValueError):
                time.sleep(.2)
        created = request(DRIVER + '/session', 'POST', {'capabilities': {'alwaysMatch': {
            'browserName': 'firefox', 'webSocketUrl': True,
            'moz:firefoxOptions': {'binary': '/snap/firefox/current/usr/lib/firefox/firefox', 'args': ['-headless']}}}}, 55)['value']
        session = created['sessionId']

        def call(endpoint, data=None, method='POST'):
            return request(DRIVER + '/session/' + session + endpoint, method, data)['value']

        def evaluate(code):
            return call('/execute/sync', {'script': code, 'args': []})

        def wait(condition, timeout=12):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                value = evaluate('return (' + condition + ')')
                if value:
                    return value
                time.sleep(.15)
            raise AssertionError('Timed out: ' + condition + '; ' + str(evaluate('return {mode:__mode,errors:__errors,body:document.body.innerText}')))

        async def resize(width, height):
            async with websockets.connect(created['capabilities']['webSocketUrl'], proxy=None) as ws:
                async def command(identity, method, params):
                    await ws.send(json.dumps({'id': identity, 'method': method, 'params': params}))
                    while True:
                        response = json.loads(await ws.recv())
                        if response.get('id') == identity:
                            if response.get('type') == 'error':
                                raise AssertionError(response)
                            return response['result']
                tree = await command(1, 'browsingContext.getTree', {})
                await command(2, 'browsingContext.setViewport', {'context': tree['contexts'][0]['context'],
                              'viewport': {'width': width, 'height': height}, 'devicePixelRatio': 1})

        call('/url', {'url': SERVER + '/#robot/docking'})
        wait("!!window.RKObserverDashboard && RKObserverDashboard.getRoute()==='robot/docking'")
        # Selector/schema checks below intentionally exercise the actual new status panel.
        expected_labels = ['前视相机', '后视相机', '左视相机', '右视相机', '深度相机 · 彩色']
        expected_ids = ['ID 10 · 11', 'ID 42', 'ID 0', 'ID 99', 'ID 11']
        read_cards = "[...document.querySelectorAll('#aruco-preview-cameras .status-card')].map(c=>({label:c.querySelector('.status-label').innerText.trim(),value:c.querySelector('.status-value').innerText,status:c.className,width:c.clientWidth,scroll:c.scrollWidth}))"
        wait("document.querySelectorAll('#aruco-preview-cameras .status-card.AVAILABLE').length===5")
        for name, width, height in (('desktop', 1440, 1000), ('mobile', 390, 844)):
            asyncio.run(resize(width, height))
            evaluate("document.getAnimations().forEach(a=>a.finish());document.getElementById('aruco-preview-cameras').closest('article').scrollIntoView({block:'start'})")
            time.sleep(.2)
            rows = evaluate('return ' + read_cards)
            assert [row['label'] for row in rows] == expected_labels, rows
            assert [row['value'] for row in rows] == expected_ids, rows
            assert all(row['scroll'] <= row['width'] + 1 for row in rows), rows
            assert evaluate('return document.documentElement.scrollWidth<=innerWidth+1')
            pose = evaluate("return [...document.querySelectorAll('#vision-docking-summary .status-card')].find(c=>c.querySelector('.status-label').innerText.includes('目标板位姿')).innerText")
            assert '前向' not in pose and '横向 —' in pose and '高度 —' in pose, pose
            assert evaluate("return [...document.querySelectorAll('#vision-docking-summary .status-card')].find(c=>c.querySelector('.status-label').innerText.includes('目标板位姿')).classList.contains('WAITING_UPSTREAM')")
            report['layouts'][name] = {'cards': rows, 'legacyPose': pose, 'viewport': width}
            (ARTIFACTS / (name + '.png')).write_bytes(base64.b64decode(call('/screenshot', method='GET')))
        evaluate("__switch('oneStale')")
        wait("document.querySelector('#aruco-preview-cameras .status-value').innerText==='等待'")
        assert evaluate("return [...document.querySelectorAll('#aruco-preview-cameras .status-value')].slice(1).every(c=>c.innerText.startsWith('ID '))")
        report['oneStaleCameraClearsOnlyItsIds'] = True
        for mode in ('stale', 'invalid'):
            evaluate('__switch(' + json.dumps(mode) + ')')
            wait("[...document.querySelectorAll('#aruco-preview-cameras .status-value')].every(c=>c.innerText==='等待')")
            report[mode + 'IdsHidden'] = True
        evaluate("__switch('empty')")
        wait("[...document.querySelectorAll('#aruco-preview-cameras .status-value')].every(c=>c.innerText==='未发现码')")
        report['FreshEmptySeparateFromWaiting'] = True
        evaluate("__switch('available')")
        wait("document.querySelector('#aruco-preview-cameras .status-value').innerText==='ID 10 · 11'")
        evaluate('window.__pause=true')
        wait("[...document.querySelectorAll('#aruco-preview-cameras .status-value')].every(c=>c.innerText==='等待')", 8)
        report['SocketStallClearsIds'] = True
        errors = evaluate('return __errors')
        assert not errors, errors
        report['javascriptErrors'] = errors
        (ARTIFACTS / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'passed': True, 'report': str(ARTIFACTS / 'report.json')}, ensure_ascii=False))
    finally:
        if session:
            try:
                request(DRIVER + '/session/' + session, 'DELETE')
            except OSError:
                pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == '__main__':
    serve() if '--serve' in sys.argv else verify()
