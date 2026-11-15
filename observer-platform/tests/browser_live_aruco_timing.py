"""Read-only LAN live ArUco timing probe. Never saves robot pixels or credentials.

Starts only an isolated loopback browser and HTTP passthrough to the existing
127.0.0.1:18083 gateway tunnel. Logs aggregate PTS/frame/overlay counters only.
The optional poll override affects the in-memory isolated script response only.
"""
import asyncio
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import urllib.request
from aiohttp import web, ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'cloud' / 'frontend'
ARTIFACTS = ROOT / 'artifacts' / 'live-aruco-timing'
SERVER = 'http://127.0.0.1:19146'
DRIVER = 'http://127.0.0.1:19148'
HTML = '''<!doctype html><meta charset="utf-8"><title>ISOLATED LIVE TIMING · NO RECORDING</title>
<link rel="stylesheet" href="/app.css"><style>body{padding:10px}.media-viewport{width:800px;height:450px}</style>
<div class="media-viewport"><video id="media-usbLeft"></video></div>
<script>
window.__errors=[];addEventListener('error',e=>__errors.push(e.message));addEventListener('unhandledrejection',e=>__errors.push(String(e.reason)));
window.CloudObserver={isExpired:()=>false};window.__metadata=[];window.__samples=[];window.__labels=[];
window.__phases=[];window.__latestRows=[];
const nativeFetch=fetch.bind(window);window.fetch=async(path,opts)=>{const started=performance.now();const response=await nativeFetch(path,opts);
 if(path==='/native-camera/detections'){const data=await response.clone().json();
  __latestRows=ObserverDetections.normalize(data,started).rows.usbLeft;__metadata.push(performance.now());}
 return response;};
const paintText=CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText=function(label,...args){if(this.canvas.classList.contains('detection-overlay')&&label.startsWith('ArUco #'))__labels.push(label);return paintText.call(this,label,...args);};
</script><script src="/native-player.js"></script><script src="/detection-overlay.js"></script><script>
const source=document.getElementById('media-usbLeft');let player;
ObserverDetections.attach({isVisible:()=>true,isDrawable:()=>!!player?.drawable()});
player=new NativeCameraPlayer(source,{isCurrent:()=>true,onState:(status,state)=>__phases.push({status,phase:state.phase}),onFrame:info=>{
 const now=performance.now();const frame={source,...info};ObserverDetections.frame('usbLeft',frame);
 const canvas=source.parentElement.querySelector('.detection-overlay');
 const normalized={...frame,kind:'video',presentedAt:now};
 const selected=ObserverDetections.select(__latestRows,normalized,now);
 const past=__latestRows.filter(r=>r.streamSession===info.streamSession&&r.mediaTime<=info.mediaTime+.000001).sort((a,b)=>b.mediaTime-a.mediaTime)[0];
 __samples.push({at:now,pts:info.mediaTime,visible:!canvas.hidden,selected:!!selected,
  gap:past?info.mediaTime-past.mediaTime:null,ids:selected?.markers?.map(m=>m.id)||[],fps:player.decodedFps(),
  buffer:source.buffered.length?source.buffered.end(source.buffered.length-1)-source.currentTime:null});
}});
window.__player=player;player.start('/native-camera/usbLeft/video.mp4');
window.__stop=()=>{player.stop();ObserverDetections.invalidate();};
</script>'''

async def handle(request):
    name=request.match_info['path']
    if not name:return web.Response(text=HTML,content_type='text/html')
    if name in ('native-player.js','app.css'):return web.FileResponse(FRONTEND/name)
    if name=='detection-overlay.js':
        source=(FRONTEND/name).read_text()
        if request.query.get('poll'):
            raise web.HTTPBadRequest()
        poll=request.app['poll']
        if poll:source=source.replace('POLL_MS = 500','POLL_MS = '+str(poll))
        return web.Response(text=source,content_type='application/javascript')
    if name=='native-camera/detections':
        async with ClientSession(timeout=ClientTimeout(total=3)) as client:
            async with client.get('http://127.0.0.1:18083/detections') as upstream:
                data=await upstream.read()
                await asyncio.sleep(.15)
                return web.Response(body=data,status=upstream.status,content_type='application/json',headers={'Cache-Control':'no-store'})
    if name=='native-camera/usbLeft/video.mp4':
        async with ClientSession(timeout=ClientTimeout(total=None,sock_connect=3,sock_read=10)) as client:
            async with client.get('http://127.0.0.1:18083/camera/camera1/video.mp4') as upstream:
                response=web.StreamResponse(status=upstream.status,headers={'Content-Type':'video/mp4','Cache-Control':'no-store',
                    'X-Video-Session':upstream.headers.get('X-Video-Session','')})
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_chunked(65536):await response.write(chunk)
                except (ConnectionResetError,asyncio.CancelledError):pass
                return response
    raise web.HTTPNotFound()


def serve(poll):
    app=web.Application();app['poll']=poll;app.router.add_get('/{path:.*}',handle)
    web.run_app(app,host='127.0.0.1',port=19146,access_log=None)

OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(url,method='GET',data=None,timeout=15):
    body=None if data is None else json.dumps(data).encode()
    req=urllib.request.Request(url,body,{'Content-Type':'application/json'},method=method)
    with OPENER.open(req,timeout=timeout) as response:return json.load(response)

def summarize(samples,metadata,errors,phases):
    cutoff=samples[0]['at']+3000 if samples else 0
    rows=[r for r in samples if r['at']>=cutoff]
    def percentile(values,q):
        values=sorted(v for v in values if v is not None)
        return values[min(len(values)-1,round((len(values)-1)*q))] if values else None
    return {'decodedFrames':len(samples),'steadyFrames':len(rows),
        'steadyDurationS':(rows[-1]['at']-rows[0]['at'])/1000 if len(rows)>1 else 0,
        'decodedFpsMedian':percentile([r['fps'] for r in rows],.5),
        'overlayVisibleFrames':sum(r['visible'] for r in rows),
        'overlayVisibleRatio':sum(r['visible'] for r in rows)/len(rows) if rows else 0,
        'selectedRatio':sum(r['selected'] for r in rows)/len(rows) if rows else 0,
        'markerIds':sorted({i for r in rows if r['visible'] for i in r['ids']}),
        'ptsGapMedianS':percentile([r['gap'] for r in rows],.5),'ptsGapP95S':percentile([r['gap'] for r in rows],.95),
        'bufferMedianS':percentile([r['buffer'] for r in rows],.5),'metadataResponses':len(metadata),
        'metadataIntervalMedianMs':statistics.median([b-a for a,b in zip(metadata,metadata[1:])]) if len(metadata)>1 else None,
        'errors':errors,'phaseCounts':{name:sum(p['phase']==name for p in phases) for name in sorted({p['phase'] for p in phases})}}


def verify(poll=0,duration=15):
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-live-aruco-timing';profile.mkdir(parents=True,exist_ok=True)
    processes=[];logs=[];session=None
    try:
        for command,name in (([sys.executable,__file__,'--serve',str(poll)],'fixture'),
            (['/snap/bin/geckodriver','--host','127.0.0.1','--port','19148','--profile-root',str(profile)],'driver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        created=request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox',
            'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless']}}}},55)['value']
        session=created['sessionId']
        def call(endpoint,data=None,method='POST'):return request(DRIVER+'/session/'+session+endpoint,method,data)['value']
        def evaluate(code):return call('/execute/sync',{'script':code,'args':[]})
        call('/url',{'url':SERVER})
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if evaluate('return __samples.length>10'):break
            time.sleep(.25)
        else:raise AssertionError(evaluate('return {errors:__errors,phases:__phases,ready:document.querySelector("video").readyState}'))
        time.sleep(duration)
        value=evaluate('return {samples:__samples,metadata:__metadata,errors:__errors,phases:__phases}')
        evaluate('__stop()')
        report=summarize(**value)
        report.update(scope='Read-only LAN rear camera; real NativeCameraPlayer + ObserverDetections; pixels never saved',
            simulatedMetadataDelayMs=150,pollOverrideMs=poll or None)
        path=ARTIFACTS/('report-'+str(poll or 500)+'.json');path.write_text(json.dumps(report,indent=2))
        print(json.dumps(report));assert report['steadyFrames']>50 and not report['errors'],report
    finally:
        if session:
            try:request(DRIVER+'/session/'+session,'DELETE')
            except OSError:pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        for log in logs:log.close()

if __name__=='__main__':
    if '--serve' in sys.argv:serve(int(sys.argv[-1]))
    else:verify(int(sys.argv[1]) if len(sys.argv)>1 else 0,int(sys.argv[2]) if len(sys.argv)>2 else 15)
