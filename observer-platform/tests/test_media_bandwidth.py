"""Viewer-only bandwidth controls; generated data and no network."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "cloud/frontend"


def test_camera_visibility_checks_viewport_and_fullscreen_before_pulling_pixels():
    if not shutil.which("node"):
        pytest.skip("Node is needed for viewport visibility")
    script = r'''const fs=require('fs'),vm=require('vm');
    const source=fs.readFileSync(process.argv[1],'utf8');
    let rect={left:10,right:650,top:20,bottom:380,width:640,height:360};
    const viewport={getBoundingClientRect:()=>rect};
    const element={closest:()=>viewport};
    const context={window:{innerWidth:800,innerHeight:600},document:{}};
    vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function cameraViewportVisible('),
      source.indexOf('function activeMediaSubpage(')),context);
    const visible=()=>context.cameraViewportVisible(element);
    const first=visible();rect={...rect,top:650,bottom:1010};const below=visible();
    rect={...rect,top:-360,bottom:0};const above=visible();
    rect={...rect,top:590,bottom:950};const partial=visible();
    rect={...rect,left:800,right:1440};const beside=visible();
    rect={...rect,width:0};const hidden=visible();
    context.document.fullscreenElement={contains:item=>item===element};const fullscreen=visible();
    context.document.fullscreenElement={contains:()=>false};const otherFullscreen=visible();
    delete context.document.fullscreenElement;
    console.log(JSON.stringify({first,below,above,partial,beside,hidden,fullscreen,otherFullscreen,
      legacy:context.cameraViewportVisible({closest:()=>({})})}));'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js')],
                          capture_output=True,text=True,check=True)
    assert json.loads(result.stdout) == dict(first=True,below=False,above=False,partial=True,
        beside=False,hidden=False,fullscreen=True,otherFullscreen=False,legacy=True)


def test_fixed_header_and_mobile_navigation_do_not_keep_covered_cameras_running():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewport occlusion')
    script=r'''const fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
    let rect={width:320,height:270,left:14,right:334,top:-200,bottom:70};
    const element={closest:()=>({getBoundingClientRect:()=>rect})};
    const context={window:{innerWidth:390,innerHeight:844},document:{querySelector:selector=>({
      getBoundingClientRect:()=>selector==='.topbar'?{width:390,bottom:108}:{width:390,top:726}})}};
    vm.createContext(context);vm.runInContext(s.slice(s.indexOf('function cameraViewportVisible('),
      s.indexOf('function activeMediaSubpage(')),context);
    const header=context.cameraViewportVisible(element);
    rect={...rect,top:750,bottom:1020};const navigation=context.cameraViewportVisible(element);
    rect={...rect,top:200,bottom:470};const content=context.cameraViewportVisible(element);
    console.log(JSON.stringify({header,navigation,content}));'''
    r=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js')],capture_output=True,text=True,check=True)
    assert json.loads(r.stdout) == {'header':False,'navigation':False,'content':True}


def test_leaving_camera_viewport_cancels_only_its_transfer_and_clears_freshness():
    if not shutil.which("node"):
        pytest.skip("Node is needed for viewport lifecycle")
    script=r'''const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
    const events=[];const front=new AbortController(),rear=new AbortController();
    const context={busy:new Map([['front',front],['rear',rear]]),
      nativePlayers:new Map([['front',{}],['rear',{}]]),nativeSources:{front:{fresh:true},rear:{fresh:true}},
      urls:{front:'blob:front',rear:'blob:rear'},cameraPollTimers:{front:12,rear:13},
      clearTimeout:id=>events.push(['timer',id]),clear:(key,message)=>events.push(['clear',key,message])};
    vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function suspendOffscreenCamera('),
      source.indexOf('  function clear(')),context);
    context.suspendOffscreenCamera('front');context.suspendOffscreenCamera('front');
    console.log(JSON.stringify({frontAborted:front.signal.aborted,rearAborted:rear.signal.aborted,
      players:[...context.nativePlayers.keys()],sources:Object.keys(context.nativeSources),
      timers:context.cameraPollTimers,events}));'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js')],
                          capture_output=True,text=True,check=True)
    value=json.loads(result.stdout)
    assert value['frontAborted'] and not value['rearAborted']
    assert value['players'] == value['sources'] == ['rear']
    assert value['timers'] == {'rear':13}
    assert ['timer',12] in value['events']


def test_map_conditionals_keep_exact_decoded_identity_and_cannot_renew_a_frame():
    if not shutil.which("node"):
        pytest.skip("Node is needed for the map lease helper")
    script = r'''const fs=require('fs'),vm=require('vm');const source=fs.readFileSync(process.argv[1],'utf8');
    const context={};vm.createContext(context);
    vm.runInContext(source.slice(source.indexOf('  function strongFrameEtag('),source.indexOf('  function cameraPollInterval(')),context);
    const lease=context.mapImageLease,tag='"decoded-png"';
    const first=lease('localCostmap','session:1',tag,'1','5',1000,null);
    const repeat=lease('localCostmap','session:1',tag,'0','5',3000,first);
    const changed=lease('localCostmap','session:2',tag,'1','5',3000,first);
    console.log(JSON.stringify({first,repeat,changed,staticEvent:lease('map','map:1',tag,'100000','event',1000,null)?.deadline===Infinity,
      invalid:[lease('localCostmap','s:1',tag,'0','event',1000,null),lease('map','s:1',tag,'bad','event',1000,null),
      lease('localCostmap','s:1',tag,'5','5',1000,null),lease('map','s:1','W/'+tag,'0','event',1000,null),
      lease('lidar','s:1',tag,'0','5',1000,null)]}));'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js')],capture_output=True,text=True,check=True)
    value=json.loads(result.stdout)
    assert value['first']['deadline'] == value['repeat']['deadline'] == 5000
    assert value['changed']['deadline'] == 7000
    assert value['staticEvent'] is True
    assert value['invalid'] == [None] * 5


def test_only_active_visual_group_is_requested_and_maps_advertise_conditionals():
    source=(FRONTEND/'media.js').read_text()
    assert 'const path = `/visuals.json?group=${subpage}${delta ? "&delta=1" : ""}`;' in source
    assert 'await fetch(path,' in source
    assert 'fetch("/visuals.json"' not in source
    assert 'const validator = camera ? cameraEtags[key] : mapImage ? mapImageEtags[key] : null;' in source
    assert 'mapImageLeases[key] = lease;' in source
    assert 'delete mapImageEtags[key]; delete mapImageLeases[key];' in source


def test_camera_polling_preserves_fixed_quality_and_uses_decoded_validators():
    if not shutil.which("node"):
        pytest.skip("Node is needed for viewer polling")
    script = r'''const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
    const context={httpCameras:{usbFront:'front'},cameraEtags:{},urls:{},cameraStates:{color:{status:'AVAILABLE'}}};
    vm.createContext(context);
    vm.runInContext(source.slice(source.indexOf('  function cameraPollInterval('),source.indexOf('  function freshCamera(')),context);
    const undecoded=context.cameraPollInterval('usbFront');context.cameraEtags.usbFront='"tag"';
    const noPixels=context.cameraPollInterval('usbFront');context.urls.usbFront='blob:decoded';
    const decoded=context.cameraPollInterval('usbFront'),color=context.cameraPollInterval('color');
    context.cameraStates.color.status='STALE';
    console.log(JSON.stringify({undecoded,noPixels,decoded,color,stale:context.cameraPollInterval('color')}));'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js')],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout) == {'undecoded':1000,'noPixels':1000,'decoded':250,'color':250,'stale':1000}
    # The unshipped adaptive-quality experiment is not a deployed requirement.
    source=(FRONTEND/'media.js').read_text()
    assert 'function adaptiveProfile(' not in source


@pytest.mark.parametrize('stage', ['download', 'decode', 'conditional'])
def test_costmap_cannot_be_painted_after_expiry_or_renewed_by_304(stage):
    if not shutil.which('node'):
        pytest.skip('Node is needed for the image lifecycle regression')
    script = r'''const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
    const stage=process.argv[2],key='localCostmap',tag='"map-png"',events=[];let clock=1000,decoded=0;
    const headers={'X-Frame-Sequence':'session:1','ETag':tag,'X-Frame-Age-S':'4','X-Frame-TTL-S':'5'};
    const context={busy:new Map(),keyVisible:()=>true,nativeKey:()=>false,nativeMode:false,httpCameras:{},
      cameras:{},maps:{localCostmap:'costmap'},cameraEtags:{},cameraFrames:{},cameraSessions:{},cameraStale:{},
      cameraPollTimers:{},nextCameraPoll:{},lastRequest:{},last:{},sequence:{},frameRevision:{},generation:0,
      urls:stage==='conditional'?{[key]:'blob:existing'}:{},mapImageEtags:stage==='conditional'?{[key]:tag}:{},
      mapImageLeases:stage==='conditional'?{[key]:{identity:'session:1',tag,deadline:2000}}:{},
      mapImageExpiryTimer:null,performance:{now:()=>clock},requestCurrent:()=>true,AbortController,
      setTimeout:()=>1,clearTimeout(){},global:{CloudObserver:{unauthorized(){throw Error('unexpected auth');}}},
      stateLabel:(key,state)=>events.push(state),
      clear(key){events.push('cleared');delete context.urls[key];delete context.mapImageEtags[key];delete context.mapImageLeases[key];},
      URL:{createObjectURL:()=> 'blob:candidate',revokeObjectURL:()=>events.push('revoked')},
      Image:class {async decode(){decoded++;clock=2500;}},
      async fetch(path,options){
        if(stage==='conditional'&&options.headers['If-None-Match']!==tag)throw Error('missing conditional');
        return {status:stage==='conditional'?304:200,ok:true,headers:{get:key=>headers[key]??null},
          async blob(){if(stage==='download')clock=2500;return {};}};
      }};
    vm.createContext(context);
    vm.runInContext(source.slice(source.indexOf('  function strongFrameEtag('),source.indexOf('  function cameraPollInterval(')),context);
    vm.runInContext(source.slice(source.indexOf('  async function image('),source.indexOf('  function metricStep(')),context);
    (async()=>{await context.image(key,'/visual/localCostmap.png');
      const deadline=context.mapImageLeases[key]?.deadline;
      if(stage==='conditional'){clock=2001;context.expireMapImages();}
      console.log(JSON.stringify({decoded,events,deadline,remaining:!!context.urls[key],busy:context.busy.size}));
    })().catch(error=>{console.error(error);process.exitCode=1;});'''
    result=subprocess.run([shutil.which('node'),'-e',script,str(FRONTEND/'media.js'),stage],
                          capture_output=True,text=True,check=True)
    value=json.loads(result.stdout)
    assert value['remaining'] is False and value['busy'] == 0
    assert value['events'][-2:] == ['cleared', 'STALE']
    assert value['decoded'] == (1 if stage == 'decode' else 0)
    if stage == 'conditional':
        assert value['deadline'] == 2000
