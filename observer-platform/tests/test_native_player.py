"""Independent H.264 playback helpers; no device, credentials or camera source."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "cloud" / "frontend"


def javascript(body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for native H.264 player regression")
    prefix = r'''
      const fs=require('fs'),vm=require('vm');
      let clock=1000;
      const context={window:{},performance:{now:()=>clock},console,
        setInterval:()=>1,clearInterval:()=>{},setTimeout:()=>2,clearTimeout:()=>{},
        AbortController,DOMException,URL:{revokeObjectURL(){}},fetch(){throw Error('No network in pure tests');}};
      vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
      const Player=context.window.NativeCameraPlayer;
    '''
    result = subprocess.run([node, "-e", prefix + body, str(FRONTEND / "native-player.js")],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_codec_comes_from_bounded_avc_configuration_not_guessed_profile():
    values = javascript(r'''
      const valid=Uint8Array.from([0,0,0,16,0x61,0x76,0x63,0x43,1,0x42,0x40,0x32,0,0,0,0]);
      const high=Uint8Array.from(valid);high[9]=0x64;high[10]=0;high[11]=0x28;
      const badSize=Uint8Array.from(valid);badSize[3]=255;
      const badVersion=Uint8Array.from(valid);badVersion[8]=2;
      console.log(JSON.stringify({valid:Player.avcCodec(valid),high:Player.avcCodec(high),
        empty:Player.avcCodec(new Uint8Array()),partial:Player.avcCodec(valid.slice(0,11)),
        badSize:Player.avcCodec(badSize),badVersion:Player.avcCodec(badVersion)}));
    ''')
    assert values == {"valid": "avc1.424032", "high": "avc1.640028", "empty": None,
                      "partial": None, "badSize": None, "badVersion": None}


def test_buffer_decisions_are_independent_of_jpeg_age_and_do_not_draw_freeze():
    values = javascript(r'''
      const changes=[];
      const video={muted:false,playsInline:false,controls:true,currentTime:10,paused:true,readyState:2,
        videoWidth:1920,videoHeight:1080,requestVideoFrameCallback(){return 4;},
        buffered:{length:1,start(){return 0;},end(){return video.currentTime+video.ahead;}},
        ahead:.39,playCount:0,pauseCount:0,play(){this.paused=false;this.playCount++;return Promise.resolve();},
        pause(){this.paused=true;this.pauseCount++;},removeAttribute(){},load(){}};
      const p=new Player(video,{onState:(status,info)=>changes.push({status,phase:info.phase})});
      p.controller=new AbortController();const epoch=p.epoch;p.tick(epoch);
      const before=video.playCount;video.ahead=.4;p.tick(epoch);const started=video.playCount;
      p.decoded(clock);p.state('AVAILABLE','playing');
      const fresh=p.drawable(clock);video.ahead=.8;p.tick(epoch);const catchup=video.playbackRate;
      video.ahead=4.1;p.tick(epoch);const sought=video.currentTime;
      video.ahead=.04;p.tick(epoch);const paused=video.paused,drawAfterPause=p.drawable(clock);
      video.paused=false;p.state('AVAILABLE','playing');clock+=1001;
      const frozen=p.drawable(clock);p.tick(epoch);
      console.log(JSON.stringify({before,started,fresh,catchup,sought,paused,drawAfterPause,frozen,
        lastState:changes[changes.length-1],dimensions:[video.videoWidth,video.videoHeight]}));
    ''')
    assert values["before"] == 0 and values["started"] == 1
    assert values["fresh"] and values["catchup"] == 1.03
    assert values["sought"] == pytest.approx(13.7)
    assert values["paused"] and not values["drawAfterPause"] and not values["frozen"]
    assert values["lastState"] == {"status": "WAITING_UPSTREAM", "phase": "buffering"}
    assert values["dimensions"] == [1920, 1080]


def test_presented_fps_is_not_upstream_encoder_or_capture_fps():
    values = javascript(r'''
      const video={paused:false,readyState:2,videoWidth:1280,videoHeight:720};
      const p=new Player(video);p.controller=new AbortController();p.state('AVAILABLE','playing');
      [1000,1100,1200,1300].forEach(t=>p.decoded(t));clock=1350;
      const rate=Player.decodedFps(video),version=Player.frameVersion(video);
      clock=2501;const frozen=Player.decodedFps(video);
      p.frameTimes=[2500,2500];p.lastFrameAt=2500;const duplicateClock=Player.decodedFps(video);
      console.log(JSON.stringify({rate,version,frozen,duplicateClock}));
    ''')
    assert values == {"rate": 10, "version": 4, "frozen": None, "duplicateClock": None}


def test_stop_and_stall_purge_the_reader_blob_and_decoded_freshness():
    values = javascript(r'''
      const operations=[];
      context.URL.revokeObjectURL=value=>operations.push(['revoke',value]);
      const video={paused:false,readyState:2,videoWidth:1920,videoHeight:1080,
        pause(){this.paused=true;operations.push(['pause']);},
        removeAttribute(name){operations.push(['remove',name]);},load(){operations.push(['load']);},
        cancelVideoFrameCallback(id){operations.push(['cancelFrame',id]);}};
      const p=new Player(video);p.controller=new AbortController();const controller=p.controller;
      p.reader={cancel(){operations.push(['cancelReader']);return Promise.resolve();}};
      p.buffer={abort(){operations.push(['abortBuffer']);}};p.url='blob:isolated-test';p.frameCallback=8;
      p.state('AVAILABLE','playing');p.decoded(clock);p.stop();
      const stopped={aborted:controller.signal.aborted,drawable:p.drawable(clock),phase:p.phase,frameAt:p.lastFrameAt};
      p.controller=new AbortController();p.path='/native-camera/usbFront/video.mp4';
      p.started=true;p.lastFrameAt=1000;clock=9000;p.tick(p.epoch);
      console.log(JSON.stringify({operations,stopped,retryPhase:p.phase,retryTimer:p.retryTimer}));
    ''')
    assert values["stopped"] == {"aborted": True, "drawable": False, "phase": "stopped", "frameAt": 0}
    assert ["cancelReader"] in values["operations"] and ["abortBuffer"] in values["operations"]
    assert ["revoke", "blob:isolated-test"] in values["operations"]
    assert ["remove", "src"] in values["operations"] and ["load"] in values["operations"]
    assert ["cancelFrame", 8] in values["operations"]
    assert values["retryPhase"] == "reconnecting" and values["retryTimer"] == 2


def test_status_source_freshness_cannot_promote_old_buffered_pixels():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for native source verification regression")
    script = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      let clock=1000;const context={nativeSources:{},nativePlayers:new Map([['front',{lastFrameAt:900}]]),performance:{now:()=>clock}};
      vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function nativeVerificationStatus('),source.indexOf('  function cameraElement(')),context);
      const invalid=[null,'50',NaN,Infinity,-1,true];
      const rejected=invalid.map(age=>{context.verifyNativeSource('bad',{frame_age_ms:age});return context.nativeVerificationStatus('bad');});
      context.verifyNativeSource('front',{frame_age_ms:50});const metadataOnly=context.nativeSourceReady('front');
      context.nativePlayers.get('front').lastFrameAt=1001;const newFrame=context.nativeSourceReady('front');
      clock=2800;context.verifyNativeSource('front',{frame_age_ms:1999});
      const continuous={since:context.nativeSources.front.freshSince,ready:context.nativeSourceReady('front')};
      clock=8800;const disconnected=context.nativeVerificationStatus('front');
      clock=8801;context.verifyNativeSource('front',{frame_age_ms:20});const recoveredMetadata=context.nativeSourceReady('front');
      context.nativePlayers.get('front').lastFrameAt=8802;const recoveredFrame=context.nativeSourceReady('front');
      context.verifyNativeSource('front',{frame_age_ms:2000});const expired=context.nativeVerificationStatus('front');
      console.log(JSON.stringify({rejected,metadataOnly,newFrame,continuous,disconnected,recoveredMetadata,recoveredFrame,expired}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")], capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert values["rejected"] == ["WAITING_UPSTREAM"] * 6
    assert not values["metadataOnly"] and values["newFrame"]
    assert values["continuous"] == {"since": 1000, "ready": True}
    assert values["disconnected"] == "WAITING_UPSTREAM"
    assert not values["recoveredMetadata"] and values["recoveredFrame"]
    assert values["expired"] == "STALE"


def test_native_integration_retains_cards_controls_routes_and_no_new_socket():
    source = (FRONTEND / "media.js").read_text()
    player = (FRONTEND / "native-player.js").read_text()
    html = (FRONTEND / "index.html").read_text()
    assert 'ObserverRecording' not in source
    assert 'old.replaceWith(element)' in source
    assert 'if (nativeKey(key)) return; // JPEG metadata is not native H.264 playback progress.' in source
    assert 'if (nativeKey(key)) {' in source and 'if (nativeMode === null' in source
    assert 'fetch("/native-camera/status"' in source
    assert 'player.start(`/native-camera/${key}/video.mp4`)' in source
    assert 'MediaRecorder' not in source and 'captureStream' not in source
    assert 'nativePlayers.forEach(player => player.stop()); nativePlayers.clear();' in source
    assert 'nativeStatusRequest?.abort()' in source
    assert 'isSourceReady: () => nativeSourceReady(key)' in source
    assert 'isRecordable' not in source and 'isRecordable' not in player
    assert 'usbLeft: "前视相机", usbFront: "后视相机", usbHub2: "左视相机", usbHub3: "右视相机"' in source
    native_metadata = source[source.index('  function nativeMetadata('):source.index('  function applyNativeStatus(')]
    assert "decodedFps(element)" in native_metadata
    assert all(label not in native_metadata for label in ("来源", "Source", "Topic", "videoUrl", "/native-camera/"))
    assert 'new WebSocket(' not in player
    assert 'this.reader?.cancel()' in player and 'this.video.removeAttribute("src")' in player
    assert 'URL.revokeObjectURL(this.url)' in player
    assert html.index('/native-player.js') < html.index('/media.js')


def usage_javascript(body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for camera occupancy display regression")
    prefix = r'''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      let clock=1300;
      const context={nativeUsage:{},nativeUsageItems:{},nativeUsageSupported:true,
        nativePlayers:new Map(),nativeSources:{},nativeDisplayStates:{},performance:{now:()=>clock}};
      vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function verifyNativeUsage('),
        source.indexOf('  function nativeVerificationStatus(')),context);
    '''
    result = subprocess.run([node, "-e", prefix + body, str(FRONTEND / "media.js")],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_camera_usage_lease_counts_sample_age_and_request_transit_without_false_busy():
    values = usage_javascript(r'''
      const checked=context.verifyNativeUsage('front',{owner:'OTHER',connected:true,checkedAgeMs:500},1000);
      const busy=context.nativeUsageNotice('front',null),deadline=context.nativeUsage.front.deadline;
      clock=6499;const before=context.nativeUsageNotice('front',null);
      clock=6500;const expired=context.nativeUsageNotice('front',null);
      const realVideo=context.nativeUsageNotice('front',{codec:'h264'});
      context.nativePlayers.set('front',{});const metadataLoss=context.nativeUsageNotice('front',null);
      clock=9000;context.verifyNativeUsage('late',{owner:'OTHER',connected:true,checkedAgeMs:0},1000);
      const delayed=context.nativeUsageNotice('late',null);
      context.nativeUsageSupported=false;const oldServer=context.nativeUsageNotice('front',null);
      console.log(JSON.stringify({checked,deadline,busy,before,expired,realVideo,metadataLoss,delayed,oldServer}));
    ''')
    assert values["checked"] and values["deadline"] == 6500
    assert values["busy"] == values["before"] == {"label": "未共享", "message": "相机正在使用，尚未共享画面"}
    assert values["expired"] == values["delayed"] == {"label": "状态未知", "message": "占用状态未知 · 等待视频"}
    assert values["realVideo"] is None and values["oldServer"] is None and values["metadataLoss"] is None



def test_other_capture_owner_can_supply_preview_but_cannot_bypass_metadata_or_connection_checks():
    values = usage_javascript(r"""
      context.verifyNativeUsage('front',{owner:'OTHER',connected:true,checkedAgeMs:0},clock);
      const valid=['h264','avc','avc1','avc1.424032'].map(codec=>context.nativeUsageNotice('front',{codec,error:null,video_error:null}));
      const invalid=[null,{},[],{codec:null},{codec:'javascript:TEST_ONLY'},
        {codec:'h264',error:'unavailable'},{codec:'h264',video_error:'unavailable'}]
        .map(item=>context.nativeUsageNotice('front',item));
      context.verifyNativeUsage('front',{owner:'OTHER',connected:false,checkedAgeMs:0},clock);
      const disconnected=context.nativeUsageNotice('front',{codec:'h264'});
      console.log(JSON.stringify({valid,invalid,disconnected}));
    """)
    assert values["valid"] == [None] * 4
    assert values["invalid"] == [{"label": "未共享", "message": "相机正在使用，尚未共享画面"}] * 7
    assert values["disconnected"] == {"label": "未连接", "message": "相机未连接"}


def test_other_capture_preview_starts_passive_stream_only_when_valid_and_connected():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for passive camera preview regression")
    script = r"""
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
      let clock=1300;const requests=[],notices=[],stops=[];
      const context={nativeUsage:{},nativeUsageItems:{},nativeUsageSupported:true,nativeMode:true,
        nativePlayers:new Map(),nativeSources:{},nativeDisplayStates:{},busy:new Map(),
        httpCameras:{usbFront:{}},performance:{now:()=>clock},global:{dispatchEvent(){}},CustomEvent:class {},
        document:{getElementById:()=>({})},cameraViewportVisible:()=>true,
        metadata:()=>{},stateLabel:(key,status,label)=>notices.push(label),
        fetch:path=>requests.push(path)};
      vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function verifyNativeUsage('),
        source.indexOf('  async function nativeStatusTick(')),context);
      context.cameraElement=()=>({});context.nativeMetadata=()=>{};context.nativeState=()=>{};
      context.clear=key=>{context.nativePlayers.get(key)?.stop();delete context.nativeDisplayStates[key];};
      context.global.NativeCameraPlayer=class {
        constructor(){this.status='WAITING_UPSTREAM';this.lastFrameAt=0;}
        start(path){context.fetch(path);}
        stop(){stops.push('stop');}
      };
      const item={id:'camera2',direction:'front',codec:'avc1.424032',error:null,video_error:null,frame_age_ms:0};
      function apply(preview,connected=true) {
        context.applyNativeStatus({enabled:true,maxPlaying:4,cameras:preview?{usbFront:preview}:{},
          cameraUsage:{usbFront:{owner:'OTHER',connected,checkedAgeMs:0}}},clock);
        return requests.length;
      }
      const noPreview=apply(null),invalid=apply({...item,codec:null}),error=apply({...item,video_error:'unavailable'});
      const valid=apply(item),ready=context.nativeVerificationStatus('usbFront');
      clock+=2000;apply({...item,frame_age_ms:2000});const stale=context.nativeVerificationStatus('usbFront');
      const disconnected=apply(item,false),lostPreview=apply(null),reconnected=apply(item);
      console.log(JSON.stringify({noPreview,invalid,error,valid,ready,stale,disconnected,lostPreview,reconnected,
        requests,notices,stops}));
    """
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")],
                            capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert values["noPreview"] == values["invalid"] == values["error"] == 0
    assert values["valid"] == values["disconnected"] == values["lostPreview"] == 1
    assert values["reconnected"] == 2
    assert values["requests"] == ["/native-camera/usbFront/video.mp4"] * 2
    assert values["ready"] == "AVAILABLE" and values["stale"] == "STALE"
    assert "未共享" in values["notices"] and "未连接" in values["notices"]
    assert values["stops"] == ["stop"]


def test_camera_usage_none_unknown_and_disconnected_have_distinct_truthful_text():
    values = usage_javascript(r'''
      const notices={};
      for(const [key,owner,connected] of [['none','NONE',true],['web','WEB',true],
        ['unknown','UNKNOWN',null],['missing','UNKNOWN',false]]) {
        context.verifyNativeUsage(key,{owner,connected,checkedAgeMs:0},clock);
        notices[key]=context.nativeUsageNotice(key,null);
      }
      const realVideo=context.nativeUsageNotice('none',{codec:'h264'});
      console.log(JSON.stringify({notices,realVideo}));
    ''')
    assert values["notices"] == {
        "none": {"label": "未启动", "message": "相机预览未启动"},
        "web": {"label": "等待视频", "message": "等待视频数据"},
        "unknown": {"label": "状态未知", "message": "占用状态未知 · 等待视频"},
        "missing": {"label": "未连接", "message": "相机未连接"},
    }
    assert values["realVideo"] is None


def test_invalid_camera_usage_cannot_create_or_preserve_occupied_claim():
    values = usage_javascript(r'''
      const good={owner:'OTHER',connected:true,checkedAgeMs:0};
      const invalid=[null,[],{}, {...good,owner:'OTHER<script>'},{...good,connected:'true'},
        {...good,checkedAgeMs:null},{...good,checkedAgeMs:'0'},{...good,checkedAgeMs:true},
        {...good,checkedAgeMs:NaN},{...good,checkedAgeMs:Infinity},{...good,checkedAgeMs:-1},
        {...good,checkedAgeMs:6001}];
      const checks=invalid.map(item=>{
        context.verifyNativeUsage('front',good,1000);
        const accepted=context.verifyNativeUsage('front',item,1000);
        return {accepted,label:context.nativeUsageNotice('front',null).label};
      });
      const future=context.verifyNativeUsage('front',good,clock+1);
      const nan=context.verifyNativeUsage('front',good,NaN);
      console.log(JSON.stringify({checks,future,nan}));
    ''')
    assert all(value == {"accepted": False, "label": "状态未知"} for value in values["checks"])
    assert not values["future"] and not values["nan"]


def test_busy_slot_is_cleared_once_and_drops_stale_source_without_global_changes():
    values = usage_javascript(r'''
      const calls=[], titles={}, known={label:'未共享',message:'相机正在使用，尚未共享画面'};
      context.clear=(key,message)=>{calls.push(['clear',key,message]);context.nativePlayers.get(key)?.stop();
        delete context.nativeDisplayStates[key];};
      context.metadata=(key,parts)=>calls.push(['metadata',key,parts]);
      context.stateLabel=(key,status,label)=>calls.push(['label',key,status,label]);
      context.document={getElementById:key=>titles[key]||(titles[key]={})};
      context.nativePlayers.set('front',{stop:()=>calls.push(['stop','front'])});
      context.nativePlayers.set('rear',{stop:()=>calls.push(['stop','rear'])});
      context.nativeSources.front={fresh:true};context.nativeSources.rear={fresh:true};
      context.nativeUsageItems.front={id:'camera2'};
      context.showNativeUsage('front',known);
      context.nativeUsageItems.front={id:'camera2'};context.showNativeUsage('front',known);
      const after={players:[...context.nativePlayers.keys()],sources:Object.keys(context.nativeSources),
        item:context.nativeUsageItems.front,title:titles['media-state-front'].title};
      context.showNativeUsage('front',{label:'状态未知',message:'占用状态未知 · 等待视频'});
      console.log(JSON.stringify({calls,after}));
    ''')
    assert values["after"] == {"players": ["rear"], "sources": ["rear"], "item": None,
                               "title": "相机正在使用，尚未共享画面"}
    assert ["stop", "rear"] not in values["calls"]
    assert values["calls"].count(["stop", "front"]) == 1
    assert ["label", "front", "WAITING_UPSTREAM", "未共享"] in values["calls"]
    assert ["label", "front", "WAITING_UPSTREAM", "状态未知"] in values["calls"]


def test_aborted_video_response_cannot_log_out_a_new_page():
    values = javascript(r"""
      let response,logouts=0,canceled=0;
      context.fetch=()=>new Promise(resolve=>response=resolve);
      const video={pause(){},removeAttribute(){},load(){}};
      const p=new Player(video,{onAuthLost:()=>logouts++});p.controller=new AbortController();
      (async()=>{const pending=p.connect('/native-camera/usbFront/video.mp4',p.epoch);
        p.stop();response({status:401,body:{cancel:async()=>canceled++}});await pending;
        console.log(JSON.stringify({logouts,canceled,stopped:p.controller===null}));
      })().catch(error=>{console.error(error);process.exitCode=1;});
    """)
    assert values == {"logouts": 0, "canceled": 1, "stopped": True}


def test_native_drawability_still_requires_verified_source_after_recording_removal():
    values = javascript(r'''
      let ready=false;
      const video={paused:false,readyState:2,videoWidth:1280,videoHeight:720};
      const p=new Player(video,{isSourceReady:()=>ready});
      p.controller=new AbortController();p.state('AVAILABLE','playing');p.decoded(clock);
      const unverified=p.drawable(clock);ready=true;const verified=p.drawable(clock);
      clock+=1000;const expired=p.drawable(clock);
      console.log(JSON.stringify({unverified,verified,expired}));
    ''')
    assert values == {"unverified": False, "verified": True, "expired": False}
