"""Loopback Firefox ArUco/YOLO acceptance; synthetic marker video, no robot/auth data."""
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
ARTIFACTS = ROOT / 'artifacts' / 'aruco-overlay'
SERVER = 'http://127.0.0.1:19138'
DRIVER = 'http://127.0.0.1:19140'
HTML = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ISOLATED ARUCO / YOLO OVERLAY FIXTURE · NOT ROBOT DATA</title><link rel="stylesheet" href="/app.css">
<style>body{padding:20px}.fixture-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
@media(max-width:600px){body{padding:8px}.fixture-grid{grid-template-columns:1fr}}
.media-viewport{height:300px}.fixture-source{display:none}</style>
<h1>检测叠加独立测试 · 合成画面</h1><div class="fixture-grid" id="gallery"></div>
<script>
window.__errors=[];addEventListener('error',e=>__errors.push(e.message));
addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
const KEYS=['usbFront','usbLeft','usbHub3','usbHub2','color'];
const SESSION='a'.repeat(32),STREAM='b'.repeat(32),HASH='c'.repeat(64);
window.__mode='normal';window.__visible=true;window.__calls=0;window.__active=0;window.__max=0;
window.__boxes=[];window.__markers=[];window.__labels=[];window.__videos={};window.__order=0;window.__frameTimes={};
window.CloudObserver={isExpired:()=>false,unauthorized(){__mode='unauthorized';}};
const nativeStroke=CanvasRenderingContext2D.prototype.strokeRect;
CanvasRenderingContext2D.prototype.strokeRect=function(...args){
 if(this.canvas.classList.contains('detection-overlay')) __boxes.push({key:this.canvas.parentElement.dataset.key,args});
 if(__boxes.length>100)__boxes.shift();return nativeStroke.apply(this,args);
};
const nativeMove=CanvasRenderingContext2D.prototype.moveTo,nativeLine=CanvasRenderingContext2D.prototype.lineTo;
const nativeStrokePath=CanvasRenderingContext2D.prototype.stroke,nativeText=CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.moveTo=function(...args){this.__polygon=[args];return nativeMove.apply(this,args);};
CanvasRenderingContext2D.prototype.lineTo=function(...args){this.__polygon?.push(args);return nativeLine.apply(this,args);};
CanvasRenderingContext2D.prototype.stroke=function(...args){
 if(this.canvas.classList.contains('detection-overlay')&&this.__polygon?.length===4)
  __markers.push({key:this.canvas.parentElement.dataset.key,points:this.__polygon});
 if(__markers.length>100)__markers.shift();return nativeStrokePath.apply(this,args);
};
CanvasRenderingContext2D.prototype.fillText=function(...args){
 if(this.canvas.classList.contains('detection-overlay'))__labels.push({key:this.canvas.parentElement.dataset.key,label:args[0]});
 if(__labels.length>100)__labels.shift();return nativeText.apply(this,args);
};
const markerIds={usbFront:10,usbLeft:11,usbHub3:42,usbHub2:0,color:99};
const markerBits={"usbFront":[[0,0,0,0,0,0,0],[0,1,0,0,1,1,0],[0,1,1,0,0,1,0],[0,1,1,0,1,1,0],[0,1,0,0,0,0,0],[0,0,0,0,1,1,0],[0,0,0,0,0,0,0]],"usbLeft":[[0,0,0,0,0,0,0],[0,1,1,0,1,0,0],[0,0,0,1,0,1,0],[0,1,0,1,1,0,0],[0,1,0,1,1,0,0],[0,0,0,0,0,0,0],[0,0,0,0,0,0,0]],"usbHub3":[[0,0,0,0,0,0,0],[0,0,1,1,0,1,0],[0,1,0,0,0,0,0],[0,1,0,0,1,0,0],[0,0,1,1,1,1,0],[0,1,0,1,1,1,0],[0,0,0,0,0,0,0]],"usbHub2":[[0,0,0,0,0,0,0],[0,1,0,1,0,0,0],[0,0,1,0,1,1,0],[0,0,1,1,0,0,0],[0,1,0,1,0,1,0],[0,1,1,1,0,0,0],[0,0,0,0,0,0,0]],"color":[[0,0,0,0,0,0,0],[0,0,0,1,0,0,0],[0,0,1,0,0,1,0],[0,0,0,0,1,1,0],[0,1,0,1,1,1,0],[0,1,1,1,0,1,0],[0,0,0,0,0,0,0]]};
function markerCorners(width,height){const x=.6,y=.25,size=Math.min(width,height)*.3;
 return [[x,y],[x+size/width,y],[x+size/width,y+size/height],[x,y+size/height]];}
const originalFetch=fetch.bind(window);
window.fetch=async function(path,options){
 if(path!='/native-camera/detections')return originalFetch(path,options);
 __calls++;__active++;__max=Math.max(__max,__active);
 try{await new Promise(resolve=>setTimeout(resolve,40));
  const cameras={};
  for(const key of KEYS){const color=key==='color';
   const width=color?480:640,height=color?300:key==='usbHub3'||key==='usbHub2'?360:480;
   cameras[key]={status:'AVAILABLE',results:[{id:SESSION+':'+(++__order),
    streamSession:color?null:__mode==='wrongSession'?'d'.repeat(32):STREAM,
    mediaTime:color?null:Math.max(0,(__videos[key]?.currentTime||0)-.05),
    imageId:color?(__mode==='wrongHash'?'d'.repeat(64):HASH):null,
    ageMs:__mode==='stale'?5001:100,width,height,
    boxes:__mode==='empty'||__mode==='markersOnly'?[]:[{classId:0,confidence:.86,xyxy:[.1,.2,.5,.9]}],
    markers:__mode==='empty'?[]:[{dictionary:'DICT_5X5_100',id:markerIds[key],corners:markerCorners(width,height)}]}]};
  }return {ok:true,status:200,json:async()=>({version:1,session:SESSION,model:'YOLO26n',cameras})};
 }finally{__active--;}
};
</script><script src="/detection-overlay.js"></script><script>
const gallery=document.getElementById('gallery'),sources={},labels=['前视相机','后视相机','左视相机','右视相机','深度相机 · 彩色'];
for(const [index,key]of KEYS.entries()){
 const article=document.createElement('article');article.className='panel media-card';
 article.innerHTML=`<header class="panel-head"><h2>${labels[index]}</h2></header><div class="media-viewport" data-key="${key}"><${key==='color'?'img':'video'} id="media-${key}"></${key==='color'?'img':'video'}></div>`;
 gallery.appendChild(article);sources[key]=document.getElementById('media-'+key);
}
ObserverDetections.attach({isVisible:()=>__visible,isDrawable:key=>__mode!=='freeze'&&(__frameTimes[key]||0)>performance.now()-1000});
let phase=0;
for(const key of KEYS){
 const source=sources[key],canvas=document.createElement('canvas');
 canvas.width=key==='color'?480:640;canvas.height=key==='color'?300:key==='usbHub3'||key==='usbHub2'?360:480;
 canvas.className='fixture-source';document.body.appendChild(canvas);const ctx=canvas.getContext('2d');
 const draw=()=>{ctx.fillStyle='#143344';ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle='#24637c';for(let x=0;x<canvas.width;x+=40)ctx.fillRect(x,0,1,canvas.height);
  for(let y=0;y<canvas.height;y+=40)ctx.fillRect(0,y,canvas.width,1);
  ctx.fillStyle='#dceef1';ctx.font='20px sans-serif';ctx.fillText('SYNTHETIC '+key,30,35);
  ctx.fillStyle='#43859a';ctx.fillRect(canvas.width*.1,canvas.height*.2,canvas.width*.4,canvas.height*.7);
  const size=Math.min(canvas.width,canvas.height)*.3,x=canvas.width*.6,y=canvas.height*.25;
  ctx.fillStyle='#fff';ctx.fillRect(x-8,y-8,size+16,size+16);
  const bits=markerBits[key];for(let row=0;row<7;row++)for(let column=0;column<7;column++){
   ctx.fillStyle=bits[row][column]?'#fff':'#000';
   ctx.fillRect(x+column*size/7,y+row*size/7,size/7+.2,size/7+.2);}
  phase++;};
 draw();
 if(key==='color'){
  source.src=canvas.toDataURL('image/jpeg');source.onload=()=>{
   __frameTimes[key]=performance.now();ObserverDetections.frame(key,{source,imageId:HASH,deadline:performance.now()+1800});};
  setInterval(()=>{if(__mode==='freeze')return;__frameTimes[key]=performance.now();
   ObserverDetections.frame(key,{source,imageId:HASH,deadline:performance.now()+1800});},300);
 }else{
  __videos[key]=source;source.muted=true;source.playsInline=true;source.srcObject=canvas.captureStream(10);source.play();
  setInterval(()=>{if(__mode!=='freeze')draw();},100);
  const present=(now,info)=>{if(__mode!=='freeze'){
   __frameTimes[key]=performance.now();ObserverDetections.frame(key,{source,streamSession:STREAM,mediaTime:info.mediaTime});}
   source.requestVideoFrameCallback(present);};source.requestVideoFrameCallback(present);
 }
}
window.__rows=()=>KEYS.map(key=>{const s=sources[key],c=s.parentElement.querySelector('.detection-overlay');
 return {key,hidden:c.hidden,width:c.width,height:c.height,sourceWidth:s.videoWidth||s.naturalWidth,
 sourceHeight:s.videoHeight||s.naturalHeight,viewportWidth:c.parentElement.clientWidth,
 viewportHeight:c.parentElement.clientHeight,box:__boxes.filter(b=>b.key===key).at(-1)?.args,
 marker:__markers.filter(m=>m.key===key).at(-1)?.points,label:__labels.filter(m=>m.key===key&&m.label.startsWith('ArUco')).at(-1)?.label};});
window.__record=()=>{const c=document.createElement('canvas');c.width=640;c.height=480;
 const ctx=c.getContext('2d'),source=sources.usbFront;ctx.drawImage(source,0,0,640,480);
 ObserverDetections.paintForSource(source,ctx,{x:0,y:0,width:640,height:480});
 const p=ctx.getImageData(64,100,1,1).data;
 const marker=ctx.getImageData(384,150,1,1).data;return {box:[...p],marker:[...marker]};};
window.__hide=()=>{__visible=false;dispatchEvent(new Event('observer-page-change'));};
</script>'''


async def handle(request):
    name = request.match_info['path']
    if not name:
        return web.Response(text=HTML, content_type='text/html')
    if name not in ('app.css', 'detection-overlay.js'):
        raise web.HTTPNotFound()
    return web.FileResponse(ROOT / 'cloud' / 'frontend' / name)


def serve():
    app = web.Application()
    app.router.add_get('/{path:.*}', handle)
    web.run_app(app, host='127.0.0.1', port=19138, access_log=None)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method='GET', data=None, timeout=15):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json'}, method=method)
    with OPENER.open(req, timeout=timeout) as response:
        return json.load(response)


def verify():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    profile = Path.home() / 'snap/firefox/common/observer-aruco-overlay-test'
    profile.mkdir(parents=True, exist_ok=True)
    processes, logs, session = [], [], None
    report = {'scope': 'Synthetic OpenCV DICT_5X5_100 markers in canvas video and JPEG; no production server, login or robot', 'layouts': {}}
    try:
        for command, name in (([sys.executable, __file__, '--serve'], 'fixture'),
                              (['/snap/bin/geckodriver', '--host', '127.0.0.1', '--port', '19140',
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
            raise AssertionError('Timed out: ' + condition + '; ' + str(evaluate('return {mode:__mode,calls:__calls,active:__active,rows:__rows(),errors:__errors}')))

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

        call('/url', {'url': SERVER})
        for name, width, height in (('desktop', 1440, 1000), ('mobile', 390, 844)):
            asyncio.run(resize(width, height))
            wait('__rows().every(r=>!r.hidden&&r.sourceWidth>0&&Math.abs(r.width-r.viewportWidth)<1&&Math.abs(r.height-r.viewportHeight)<1)')
            rows = evaluate('return __rows()')
            for row in rows:
                scale = min(row['viewportWidth'] / row['sourceWidth'], row['viewportHeight'] / row['sourceHeight'])
                actual_width, actual_height = row['sourceWidth'] * scale, row['sourceHeight'] * scale
                expected = [(row['viewportWidth'] - actual_width) / 2 + .1 * actual_width,
                            (row['viewportHeight'] - actual_height) / 2 + .2 * actual_height,
                            .4 * actual_width, .7 * actual_height]
                assert all(abs(a - b) < 1 for a, b in zip(row['box'], expected)), (row, expected)
                marker_size = min(row['sourceWidth'], row['sourceHeight']) * .3
                expected_marker = [
                    [(row['viewportWidth']-actual_width)/2 + .6*actual_width,
                     (row['viewportHeight']-actual_height)/2 + .25*actual_height],
                    [(row['viewportWidth']-actual_width)/2 + (.6+marker_size/row['sourceWidth'])*actual_width,
                     (row['viewportHeight']-actual_height)/2 + .25*actual_height],
                    [(row['viewportWidth']-actual_width)/2 + (.6+marker_size/row['sourceWidth'])*actual_width,
                     (row['viewportHeight']-actual_height)/2 + (.25+marker_size/row['sourceHeight'])*actual_height],
                    [(row['viewportWidth']-actual_width)/2 + .6*actual_width,
                     (row['viewportHeight']-actual_height)/2 + (.25+marker_size/row['sourceHeight'])*actual_height]]
                assert len(row['marker']) == 4
                assert all(abs(a-b)<1 for actual,expected in zip(row['marker'],expected_marker) for a,b in zip(actual,expected)), (row,expected_marker)
                expected_ids = {'usbFront':10, 'usbLeft':11, 'usbHub3':42, 'usbHub2':0, 'color':99}
                assert row['label'] == 'ArUco #' + str(expected_ids[row['key']]), row
            assert evaluate('return document.documentElement.scrollWidth<=innerWidth+1')
            report['layouts'][name] = rows
            (ARTIFACTS / (name + '.png')).write_bytes(base64.b64decode(call('/screenshot', method='GET')))
        pixel = evaluate('return __record()')
        assert pixel['box'][1] > 180 and pixel['box'][0] < 160 and pixel['box'][2] > 120, pixel
        assert pixel['marker'][0] > 230 and pixel['marker'][1] > 170 and 70 < pixel['marker'][2] < 160, pixel
        report['recordedOverlayPixel'] = pixel
        evaluate("window.__mode='markersOnly'")
        wait('__rows().every(r=>!r.hidden)')
        time.sleep(.7)
        assert evaluate("return __rows().every(r=>r.label.startsWith('ArUco #'))")
        marker_only_pixel = evaluate('return __record()')
        assert marker_only_pixel['marker'][0] > 230, marker_only_pixel
        report['markerOnlyOverlayAndRecording'] = True
        evaluate("window.__mode='normal'")
        for mode in ('empty', 'wrongSession', 'wrongHash', 'stale', 'freeze'):
            evaluate('window.__mode=' + json.dumps(mode))
            if mode == 'wrongSession':
                condition = "__rows().filter(r=>r.key!=='color').every(r=>r.hidden)"
            elif mode == 'wrongHash':
                condition = "__rows().find(r=>r.key==='color').hidden"
            else:
                condition = '__rows().every(r=>r.hidden)'
            wait(condition)
            report[mode + 'Clears'] = True
            evaluate("window.__mode='normal'")
            wait('__rows().every(r=>!r.hidden)')
        evaluate('__hide()')
        wait('__rows().every(r=>r.hidden)')
        time.sleep(.2)
        calls = evaluate('return __calls')
        time.sleep(.7)
        assert evaluate('return __calls') == calls
        assert evaluate('return __max') == 1
        errors = evaluate('return __errors')
        assert not errors, errors
        report.update(hiddenPageStopsRequests=True, singleInFlight=True, javascriptErrors=errors)
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
