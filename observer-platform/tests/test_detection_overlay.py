"""Isolated JS replay: frame identity, stale results and bounded canvas overlays."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / 'cloud' / 'frontend'


def javascript(body, filename='detection-overlay.js'):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for detection overlay replay')
    prefix = r'''
      const fs=require('fs'),vm=require('vm');let clock=1000,shown=true,drawable=true,expired=false;
      const handlers={},intervals=[],fetches=[],canvases=[];
      const SESSION='a'.repeat(32),STREAM='b'.repeat(32),HASH='c'.repeat(64);
      const operations=[];
      function drawing(){return {save(){},restore(){},beginPath(){},clip(){},setTransform(){},
        moveTo(...a){operations.push(['move',...a]);},lineTo(...a){operations.push(['line',...a]);},
        closePath(){operations.push(['close']);},stroke(){operations.push(['polygon']);},
        clearRect(...a){operations.push(['clear',...a]);},rect(...a){operations.push(['clip',...a]);},
        strokeRect(...a){operations.push(['box',...a]);},fillRect(...a){operations.push(['labelbox',...a]);},
        fillText(...a){operations.push(['text',...a]);},measureText(t){return {width:t.length*8};}};}
      const viewport={clientWidth:800,clientHeight:450,appendChild(c){canvases.push(c);c.parentElement=this;}};
      const source={id:'media-usbFront',hidden:false,videoWidth:640,videoHeight:480,closest(){return viewport;}};
      const colorSource={id:'media-color',hidden:false,naturalWidth:480,naturalHeight:300,closest(){return viewport;}};
      const window={devicePixelRatio:1,addEventListener:(n,f)=>(handlers[n]??=[]).push(f),
        CloudObserver:{isExpired:()=>expired,unauthorized(){expired=true;}}};
      const document={hidden:false,addEventListener:(n,f)=>(handlers[n]??=[]).push(f),
        getElementById:id=>id===source.id?source:id===colorSource.id?colorSource:null,
        createElement:()=>({width:300,height:150,hidden:true,setAttribute(){},getContext(){return drawing();}})};
      function result(order=1,options={}){return {id:SESSION+':'+order,streamSession:STREAM,mediaTime:10,imageId:null,
        ageMs:100,width:640,height:480,boxes:[{classId:0,confidence:.86,xyxy:[.1,.2,.5,.9]}],...options};}
      function color(order=1,options={}){return result(order,{streamSession:null,mediaTime:null,imageId:HASH,
        width:480,height:300,...options});}
      function payload(rows={usbFront:{status:'AVAILABLE',results:[result()]}}){return {version:1,session:SESSION,
        model:'YOLO26n',cameras:rows};}
      let response=payload();
      const context={window,document,performance:{now:()=>clock},AbortController,console,
        setInterval:f=>{intervals.push(f);return intervals.length;},
        setTimeout:()=>2,clearTimeout(){},fetch:async(path,options)=>{
          fetches.push({path,options});return {ok:true,status:200,json:async()=>response};}};
      vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
      const D=window.ObserverDetections;
      const videoFrame={kind:'video',streamSession:STREAM,mediaTime:10.2,presentedAt:clock,source};
      const imageFrame={kind:'image',imageId:HASH,deadline:3000,source:colorSource};
      const flush=()=>new Promise(resolve=>setImmediate(resolve));
      function attach(){D.attach({isVisible:()=>shown,isDrawable:()=>drawable});}
      function emit(name){(handlers[name]||[]).forEach(f=>f());}
    '''
    completed = subprocess.run([node, '-e', prefix + body, str(FRONTEND / filename)],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_video_chooses_displayed_pts_in_same_session_and_empty_latest_clears():
    values = javascript(r'''
      const rows=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[
        result(1,{mediaTime:9.8}),result(2,{mediaTime:10}),result(3,{mediaTime:10.3}),
        result(4,{mediaTime:10.2,streamSession:'d'.repeat(32)})]}}),clock).rows.usbFront;
      const selected=D.select(rows,videoFrame,clock);
      const future=D.select(rows,{...videoFrame,mediaTime:9.7},clock);
      const late=D.select(rows,{...videoFrame,mediaTime:11},clock);
      const other=D.select(rows,{...videoFrame,streamSession:'e'.repeat(32)},clock);
      const empty=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1),result(2,{mediaTime:10.1,boxes:[]})]}}),clock);
      console.log(JSON.stringify({selected:selected.id,future,late,other,empty:D.select(empty.rows.usbFront,videoFrame,clock).boxes}));
    ''')
    assert values == {'selected': 'a' * 32 + ':2', 'future': None, 'late': None, 'other': None, 'empty': []}


def test_color_only_matches_current_decoded_jpeg_and_original_deadline():
    values = javascript(r'''
      const rows=D.normalize(payload({color:{status:'AVAILABLE',results:[color(),color(2,{imageId:'d'.repeat(64)})]}}),clock).rows.color;
      console.log(JSON.stringify({matched:D.select(rows,imageFrame,clock).id,
        different:D.select(rows,{...imageFrame,imageId:'e'.repeat(64)},clock),
        expired:D.select(rows,imageFrame,3000),missing:D.select(rows,{...imageFrame,imageId:null},clock)}));
    ''')
    assert values == {'matched': 'a' * 32 + ':1', 'different': None, 'expired': None, 'missing': None}


def test_lease_includes_request_transit_and_repeated_id_cannot_renew_or_change_boxes():
    values = javascript(r'''
      const leases=new Map(),p=payload();
      const first=D.normalize(p,1000,leases).rows.usbFront[0];
      const repeated=D.normalize(p,3000,leases).rows.usbFront[0];
      p.cameras.usbFront.results[0].boxes=[];
      const changed=D.normalize(p,4000,leases).rows.usbFront;
      const delayed=D.normalize(payload(),0).rows.usbFront;
      console.log(JSON.stringify({deadline:first.deadline,repeated:repeated.deadline,changed,
        transitExpired:D.select(delayed,{...videoFrame,presentedAt:5000},5000),
        retired:D.normalize(payload(),9000,new Map(),new Map([['usbFront',1]])).rows.usbFront}));
    ''')
    assert values == {'deadline': 5900, 'repeated': 5900, 'changed': [], 'transitExpired': None, 'retired': []}


def test_frozen_video_and_stale_source_cannot_show_boxes():
    values = javascript(r'''
      const rows=D.normalize(payload(),clock).rows.usbFront;
      const invalid=[null,NaN,Infinity,-1,5000,true,'10'];
      console.log(JSON.stringify({fresh:!!D.select(rows,videoFrame,1999),frozen:D.select(rows,videoFrame,2000),
        old:D.select(rows,{...videoFrame,presentedAt:6000},6000),
        invalid:invalid.map(ageMs=>D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{ageMs})]}}),clock).rows.usbFront.length)}));
    ''')
    assert values == {'fresh': True, 'frozen': None, 'old': None, 'invalid': [0] * 7}


def test_untrusted_boxes_identities_and_unknown_camera_never_draw():
    values = javascript(r'''
      const bad=[{classId:80,confidence:.8,xyxy:[0,0,1,1]},
        {classId:0,confidence:NaN,xyxy:[0,0,1,1]},{classId:0,confidence:1.1,xyxy:[0,0,1,1]},
        {classId:0,confidence:.8,xyxy:[0,0,-1,1]},{classId:0,confidence:.8,xyxy:[0,0,0,1]},
        {classId:'<img onerror=x>',confidence:.8,xyxy:[0,0,1,1]}];
      const lengths=bad.map(box=>D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{boxes:[box]})]}}),clock).rows.usbFront.length);
      const crowded=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{boxes:Array(13).fill(result().boxes[0])})]}}),clock);
      const unknown=D.normalize(payload({depth:{status:'AVAILABLE',results:[result()]}}),clock);
      const cross=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{id:'d'.repeat(32)+':1'})]}}),clock);
      console.log(JSON.stringify({lengths,crowded:crowded.rows.usbFront,unknownKeys:Object.keys(unknown.rows),
        cross:cross.rows.usbFront,labels:D.labels.length,first:D.labels[0],last:D.labels[79]}));
    ''')
    assert values['lengths'] == [0] * 6
    assert values['crowded'] == values['cross'] == []
    assert values['unknownKeys'] == ['usbFront', 'usbLeft', 'usbHub3', 'usbHub2', 'color']
    assert values['labels'] == 80 and values['first'] == '人' and values['last'] == '牙刷'


def test_contain_letterboxing_and_same_painter_for_recording_geometry():
    values = javascript(r'''
      const wide=D.contain(800,450,640,480),tall=D.contain(400,600,480,300);
      D.paintBoxes(drawing(),wide,[result().boxes[0]]);
      const display=operations.filter(row=>row[0]==='box')[0];operations.length=0;
      D.paintBoxes(drawing(),{x:10,y:20,width:480,height:300},[result().boxes[0]]);
      console.log(JSON.stringify({wide,tall,invalid:D.contain(0,20,640,480),display,
        recorded:operations.find(row=>row[0]==='box'),label:operations.find(row=>row[0]==='text')[1]}));
    ''')
    assert values['wide'] == {'x': 100, 'y': 0, 'width': 600, 'height': 450}
    assert values['tall'] == {'x': 0, 'y': 175, 'width': 400, 'height': 250}
    assert values['invalid'] is None
    assert values['display'][1:] == pytest.approx([160, 90, 240, 315])
    assert values['recorded'][1:] == pytest.approx([58, 80, 192, 210])
    assert values['label'] == '人 86%'


def test_runtime_shares_boxes_with_recording_and_clears_on_stall_and_empty_result():
    values = javascript(r'''
      (async()=>{
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        const painted=!canvases[0].hidden;operations.length=0;
        D.paintForSource(source,drawing(),{x:0,y:0,width:640,height:480});
        const recording=operations.filter(row=>row[0]==='box').length;
        drawable=false;clock=1100;intervals[0]();const stalled=canvases[0].hidden;
        drawable=true;clock=1500;response=payload({usbFront:{status:'AVAILABLE',results:[result(2,{boxes:[]})]}});
        D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        const empty=canvases[0].hidden;
        console.log(JSON.stringify({painted,recording,stalled,empty,requests:fetches.length}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''')
    assert values == {'painted': True, 'recording': 1, 'stalled': True, 'empty': True, 'requests': 2}


@pytest.mark.parametrize('late_status', [200, 403])
def test_single_inflight_request_is_aborted_and_late_response_cannot_resurrect_hidden_page(late_status):
    values = javascript(r'''
      (async()=>{
        let resolve;context.fetch=(path,options)=>{fetches.push({path,options});return new Promise(r=>resolve=r);};
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();clock=1600;intervals[0]();clock=2200;intervals[0]();
        const requests=fetches.length;shown=false;emit('observer-page-change');
        const aborted=fetches[0].options.signal.aborted;
        resolve({ok:LATE_STATUS===200,status:LATE_STATUS,json:async()=>payload()});await flush();
        intervals[0]();console.log(JSON.stringify({requests,aborted,hidden:canvases.every(c=>c.hidden),after:fetches.length,expired}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    '''.replace('LATE_STATUS', str(late_status)))
    assert values == {'requests': 1, 'aborted': True, 'hidden': True, 'after': 1, 'expired': False}


def test_timeout_retries_back_off_from_completion_and_success_restores_regular_cadence():
    values = javascript(r'''
      (async()=>{
        let timeout,active=0,maximum=0,mode='timeout';
        const times=[];
        context.setTimeout=(fn,delay)=>{timeout=fn;return 2;};
        context.fetch=(path,options)=>{
          fetches.push({path,options});times.push(clock);active++;maximum=Math.max(maximum,active);
          if(mode==='success'){
            active--;return Promise.resolve({ok:true,status:200,json:async()=>payload()});
          }
          return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{
            active--;reject(new DOMException('Timed out','AbortError'));
          },{once:true}));
        };
        attach();
        for(const [start,finished,due] of [[1000,2500,3500],[3500,5000,7000],
            [7000,8500,12500],[12500,14000,18000]]){
          clock=start;intervals[0]();const requests=fetches.length;
          clock=finished;timeout();await flush();intervals[0]();
          if(fetches.length!==requests)throw Error('Immediate retry after timeout');
          clock=due-1;intervals[0]();
          if(fetches.length!==requests)throw Error('Retry before backoff deadline');
        }
        clock=18000;mode='success';intervals[0]();await flush();
        clock=18499;intervals[0]();const beforeRegular=fetches.length;
        clock=18500;intervals[0]();await flush();
        // A recovered connection starts a fresh one-second failure delay.
        clock=19000;mode='timeout';intervals[0]();clock=20500;timeout();await flush();
        clock=21499;intervals[0]();const beforeReset=fetches.length;
        clock=21500;intervals[0]();
        console.log(JSON.stringify({times,maximum,beforeRegular,beforeReset,hidden:canvases.every(c=>c.hidden)}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''')
    assert values == {'times': [1000, 3500, 7000, 12500, 18000, 18500, 19000, 21500],
                      'maximum': 1, 'beforeRegular': 5, 'beforeReset': 7, 'hidden': True}


@pytest.mark.parametrize('failure', ['http', 'json', 'contract'])
def test_unavailable_detection_responses_back_off_and_page_change_resets_delay(failure):
    values = javascript(r'''
      (async()=>{
        context.fetch=async(path,options)=>{
          fetches.push({path,options});
          return {ok:FAILURE!=='http',status:FAILURE==='http'?503:200,
            json:async()=>{if(FAILURE==='json')throw Error('Truncated JSON');
              return FAILURE==='contract'?{}:payload();}};
        };
        attach();intervals[0]();await flush();
        clock=1999;intervals[0]();const before=fetches.length;
        // Leaving and returning may retry immediately; an old backoff is not a lease.
        shown=false;emit('observer-page-change');shown=true;
        intervals[0]();await flush();const after=fetches.length;
        clock=2998;intervals[0]();const beforeReset=fetches.length;
        clock=2999;intervals[0]();await flush();
        console.log(JSON.stringify({before,after,beforeReset,total:fetches.length,
          hidden:canvases.every(c=>c.hidden)}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    '''.replace('FAILURE', json.dumps(failure)))
    assert values == {'before': 1, 'after': 2, 'beforeReset': 2, 'total': 3, 'hidden': True}


@pytest.mark.parametrize('event', ['cloud-auth-lost', 'pagehide', 'visibilitychange'])
def test_lifecycle_removes_overlays_and_frames(event):
    values = javascript(r'''
      (async()=>{
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        const before=!canvases[0].hidden;emit(EVENT);operations.length=0;
        D.paintForSource(source,drawing(),{x:0,y:0,width:640,height:480});
        console.log(JSON.stringify({before,hidden:canvases.every(c=>c.hidden),recorded:operations.some(r=>r[0]==='box')}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    '''.replace('EVENT', json.dumps(event)))
    assert values == {'before': True, 'hidden': True, 'recorded': False}


def test_pending_permission_auth_failure_clears_without_delivering_results():
    values = javascript(r'''
      (async()=>{
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        clock=1500;context.fetch=async()=>({status:403,ok:false,json:async()=>{throw Error('must not read');}});
        intervals[0]();await flush();const hidden=canvases.every(c=>c.hidden);clock=2000;intervals[0]();
        console.log(JSON.stringify({expired,hidden,requests:fetches.length}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''')
    assert values == {'expired': True, 'hidden': True, 'requests': 1}


def test_integration_binds_detections_to_decoded_jpeg_and_verified_native_frames():
    media = (FRONTEND / 'media.js').read_text()
    native = (FRONTEND / 'native-player.js').read_text()
    html = (FRONTEND / 'index.html').read_text()
    assert html.index('/detection-overlay.js') < html.index('/media.js')
    assert 'X-Video-Session' in native
    assert 'mediaTime: video.currentTime, streamSession: this.streamSession' in native
    assert 'mediaTime: info.mediaTime, streamSession: this.streamSession' in native
    assert 'X-Frame-Image-SHA256' in media
    assert 'if (digest !== previous.imageId) previous.imageId = null' in media
    assert media.index('await candidate.decode()') < media.index('imageId: cameraFrames[key].imageId, deadline')
    # Exercise the actual native presentation callback. Browser recording was
    # retired; detections still require a verified, currently drawable frame.
    values = javascript(r'''
      const media=fs.readFileSync(process.argv[2],'utf8');
      const callback=media.match(/onFrame: info => \{([\s\S]*?)\n          \},\n          onAuthLost:/);
      if(!callback)throw Error('Native presentation callback not found');
      const delivered=[],states=[];let callbackReady=false,callbackDrawable=false;
      const player={status:'AVAILABLE',phase:'playing',message:'',drawable:()=>callbackDrawable};
      const element={id:'media-usbFront'},info={mediaTime:10.2,streamSession:STREAM};
      const run=new Function('info','nativeState','key','player','nativeSourceReady','global','element',callback[1]);
      const args=[info,(...v)=>states.push(v),'usbFront',player,()=>callbackReady,
        {ObserverDetections:{frame:(...v)=>delivered.push(v)}},element];
      run(...args);callbackReady=true;run(...args);callbackDrawable=true;run(...args);
      console.log(JSON.stringify({states:states.length,delivered,sourceIdentity:delivered[0][1].source===element}));
    '''.replace("process.argv[2]", json.dumps(str(FRONTEND / 'media.js'))))
    assert values == {'states': 3, 'delivered': [['usbFront', {
        'mediaTime': 10.2, 'streamSession': 'b' * 32,
        'source': {'id': 'media-usbFront'}}]], 'sourceIdentity': True}


def test_native_player_uses_response_session_and_presented_media_time_in_both_browser_paths():
    values = javascript(r'''
      (async()=>{
        const Player=window.NativeCameraPlayer,frames=[];
        let callback=null;
        const video={muted:false,playsInline:false,controls:false,paused:false,readyState:2,
          videoWidth:640,videoHeight:480,currentTime:12.25,
          requestVideoFrameCallback(f){callback=f;return 1;}};
        const p=new Player(video,{onFrame:info=>frames.push(info)});p.controller=new AbortController();
        context.fetch=async()=>({status:200,ok:true,headers:{get:name=>name==='X-Video-Session'?STREAM:null},
          body:{getReader:()=>({read:async()=>({done:true})})}});
        try{await p.connect('/native-camera/usbFront/video.mp4',p.epoch);}catch(_){}
        p.observeFrame(p.epoch);callback(clock,{mediaTime:12.125,presentedFrames:7});
        delete video.requestVideoFrameCallback;video.getVideoPlaybackQuality=()=>({totalVideoFrames:8});
        clock+=100;p.tick(p.epoch);
        console.log(JSON.stringify({session:p.streamSession,frames:frames.map(f=>({mediaTime:f.mediaTime,
          streamSession:f.streamSession,width:f.width,height:f.height}))}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''', 'native-player.js')
    assert values == {'session': 'b' * 32, 'frames': [
        {'mediaTime': 12.125, 'streamSession': 'b' * 32, 'width': 640, 'height': 480},
        {'mediaTime': 12.25, 'streamSession': 'b' * 32, 'width': 640, 'height': 480}]}


def test_aruco_markers_are_optional_and_keep_dictionary_ids_corners_only():
    values = javascript(r'''
      const marker={dictionary:'DICT_5X5_100',id:10,corners:[[.1,.2],[.5,.25],[.45,.7],[.15,.65]],confidence:1,label:'untrusted'};
      const raw=result(1,{boxes:[],markers:[marker,{...marker,id:99,corners:[...marker.corners].reverse()}]});
      const item=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[raw]}}),clock).rows.usbFront[0];
      console.log(JSON.stringify({legacy:D.normalize(payload(),clock).rows.usbFront[0].markers,markers:item.markers,
        selected:D.select([item],videoFrame,clock).id,originalUntouched:marker.confidence}));
    ''')
    assert values['legacy'] == []
    assert values['selected'] == 'a' * 32 + ':1'
    assert values['markers'] == [
        {'dictionary': 'DICT_5X5_100', 'id': 10, 'corners': [[.1, .2], [.5, .25], [.45, .7], [.15, .65]]},
        {'dictionary': 'DICT_5X5_100', 'id': 99, 'corners': [[.15, .65], [.45, .7], [.5, .25], [.1, .2]]}]
    assert values['originalUntouched'] == 1


def test_untrusted_aruco_markers_never_reach_canvas():
    values = javascript(r'''
      const marker={dictionary:'DICT_5X5_100',id:10,corners:[[.1,.2],[.5,.2],[.5,.6],[.1,.6]]};
      const invalid=[null,{},'marker',Array(13).fill(marker),[null],
        [{...marker,dictionary:'<script>'}],[{...marker,id:true}],[{...marker,id:100}],
        [{...marker,id:-1}],[{...marker,id:1.5}],[{...marker,corners:[[0,0],[1,1],[0,1],[1,0]]}],
        [{...marker,corners:[[0,0],[1,0],[.4,.3],[0,1]]}],
        [{...marker,corners:[[0,0],[0,0],[1,1],[0,1]]}],
        [{...marker,corners:[[0,0],[1,0],[1,0],[0,0]]}],
        [{...marker,corners:[[0,0],[1,0],[1,1]]}],
        [{...marker,corners:[[0,0],[1,0],[Infinity,1],[0,1]]}],
        [{...marker,corners:[[0,0],[1,0],[1,1],[-.1,1]]}],
        [{...marker,corners:[[0,0],[1,0],[1,1],[0,'1']]}]];
      const counts=invalid.map(markers=>D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{markers})]}}),clock).rows.usbFront.length);
      const duplicate=D.normalize(payload({usbFront:{status:'AVAILABLE',results:[result(1,{markers:[marker,marker]})]}}),clock).rows.usbFront[0];
      console.log(JSON.stringify({counts,duplicate:duplicate.markers.length}));
    ''')
    assert values['counts'] == [0] * 18
    assert values['duplicate'] == 2


def test_aruco_marker_coordinates_cannot_change_with_same_result_identity():
    values = javascript(r'''
      const marker={dictionary:'DICT_5X5_100',id:10,corners:[[.1,.2],[.5,.2],[.5,.6],[.1,.6]]};
      const leases=new Map(),p=payload({usbFront:{status:'AVAILABLE',results:[result(1,{markers:[marker]})]}});
      const first=D.normalize(p,clock,leases).rows.usbFront[0];
      p.cameras.usbFront.results[0].markers[0].id=11;
      const changed=D.normalize(p,clock+500,leases).rows.usbFront;
      console.log(JSON.stringify({id:first.markers[0].id,changed}));
    ''')
    assert values == {'id': 10, 'changed': []}


def test_aruco_polygon_uses_contain_geometry_and_label_without_fake_confidence():
    values = javascript(r'''
      const marker={dictionary:'DICT_5X5_100',id:10,corners:[[.1,.2],[.5,.25],[.45,.7],[.15,.65]]};
      D.paintMarkers(drawing(),D.contain(800,450,640,480),[marker]);
      console.log(JSON.stringify({vertices:operations.filter(row=>row[0]==='move'||row[0]==='line'),
        polygon:operations.filter(row=>row[0]==='polygon').length,label:operations.find(row=>row[0]==='text')[1]}));
    ''')
    assert values['vertices'] == [['move', 160, 90], ['line', 400, 112.5], ['line', 370, 315], ['line', 190, 292.5]]
    assert values['polygon'] == 1
    assert values['label'] == 'ArUco #10'


def test_runtime_marker_only_result_is_recorded_and_empty_or_stalled_frame_clears():
    values = javascript(r'''
      (async()=>{
        const marker={dictionary:'DICT_5X5_100',id:11,corners:[[.1,.2],[.5,.2],[.5,.6],[.1,.6]]};
        response=payload({usbFront:{status:'AVAILABLE',results:[result(1,{boxes:[],markers:[marker]})]}});
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        const painted=!canvases[0].hidden;operations.length=0;
        D.paintForSource(source,drawing(),{x:0,y:0,width:640,height:480});
        const recorded=operations.filter(row=>row[0]==='polygon').length;
        const label=operations.find(row=>row[0]==='text')[1];
        drawable=false;clock=1100;intervals[0]();const stalled=canvases[0].hidden;
        drawable=true;clock=1500;response=payload({usbFront:{status:'AVAILABLE',results:[result(2,{boxes:[],markers:[]})]}});
        D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        console.log(JSON.stringify({painted,recorded,label,stalled,empty:canvases[0].hidden}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''')
    assert values == {'painted': True, 'recorded': 1, 'label': 'ArUco #11', 'stalled': True, 'empty': True}


def test_color_marker_requires_displayed_jpeg_hash_and_expires_with_frame():
    values = javascript(r'''
      const marker={dictionary:'DICT_5X5_100',id:42,corners:[[.1,.2],[.5,.2],[.5,.6],[.1,.6]]};
      const item=D.normalize(payload({color:{status:'AVAILABLE',results:[color(1,{boxes:[],markers:[marker]})]}}),clock).rows.color;
      console.log(JSON.stringify({matched:D.select(item,imageFrame,clock).markers[0].id,
        other:D.select(item,{...imageFrame,imageId:'e'.repeat(64)},clock),expired:D.select(item,imageFrame,3000)}));
    ''')
    assert values == {'matched': 42, 'other': None, 'expired': None}


def test_detection_poll_uses_selected_robot_and_unknown_robot_does_not_fallback():
    values = javascript(r'''
      (async()=>{
        const requests=[];
        window.location={href:'https://observer.example/?robot=robot-two',origin:'https://observer.example'};
        window.fetch=async(path,options)=>{
          const robot=options?.headers?.get('X-Observer-Robot');requests.push({path,robot});
          return {ok:robot==='robot-two',status:robot==='robot-two'?200:404,
            json:async()=>response,body:{cancel:async()=>{}}};
        };
        context.URL=URL;context.Headers=Headers;context.Request=Request;
        context.fetch=async()=>{throw new Error('default robot fetch must never run');};
        const fleetScript=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'fleet.js'),'utf8');
        vm.runInContext(fleetScript,context);
        attach();D.frame('usbFront',{...videoFrame});intervals[0]();await flush();
        const selectedPainted=!canvases[0].hidden;
        window.location.href='https://observer.example/?robot=unknown';
        vm.runInContext(fleetScript,context);
        clock=1600;D.frame('usbFront',{...videoFrame,presentedAt:clock});intervals[0]();await flush();
        console.log(JSON.stringify({requests,selectedPainted,unknownHidden:canvases[0].hidden}));
      })().catch(e=>{console.error(e);process.exitCode=1;});
    ''')
    assert values == {
        'requests': [
            {'path': '/native-camera/detections', 'robot': 'robot-two'},
            {'path': '/native-camera/detections', 'robot': 'unknown'},
        ],
        'selectedPainted': True,
        'unknownHidden': True,
    }
