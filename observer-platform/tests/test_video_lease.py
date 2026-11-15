"""Browser-origin ownership and lifecycle regressions without cloud credentials."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "cloud" / "frontend"


HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
let clock=1000;const channels=[],held=new Map();
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const manager={async request(name,options,callback){
  if(held.has(name))return callback(null);
  const token={};held.set(name,token);
  try{return await callback({name});}finally{if(held.get(name)===token)held.delete(name);}
}};
function page(id,useLocks=true){
  const events=new EventTarget(),intervals=[];
  const win={navigator:useLocks?{locks:manager}:{},crypto:{randomUUID:()=>id},
    queueMicrotask,addEventListener:events.addEventListener.bind(events),
    dispatchEvent:events.dispatchEvent.bind(events)};
  win.BroadcastChannel=class{
    constructor(name){this.name=name;this.closed=false;channels.push(this);}
    postMessage(data){for(const peer of channels)if(peer!==this&&!peer.closed&&peer.name===this.name)
      queueMicrotask(()=>{if(!peer.closed)peer.onmessage?.({data});});}
    close(){this.closed=true;}
  };
  const context={window:win,Event,Promise,Math,Date:{now:()=>clock},
    setTimeout,clearTimeout,setInterval:callback=>{intervals.push(callback);return intervals.length;}};
  vm.createContext(context);vm.runInContext(source,context);
  const downloads=[];
  win.addEventListener('observer-video-lease-change',()=>{
    if(win.ObserverVideoLease.state().active){
      if(!downloads.length||downloads.at(-1).signal.aborted)downloads.push(new AbortController());
    }else for(const controller of downloads)controller.abort();
  });
  return {win,lease:win.ObserverVideoLease,downloads,beat:()=>intervals.forEach(fn=>fn()),
    crash:()=>{for(const channel of channels)if(channel.onmessage&&channel.name=== 'observer-camera-playback-v1'){
      // Assigned below to keep other windows' channels alive.
      if(channel.owner===id)channel.close();
    }},lastChannel:channels.at(-1)};
}
'''


def run_javascript(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for browser lease regressions")
    result = subprocess.run([node, "-e", HARNESS + "\n(async()=>{\n" + body +
        "\n})().catch(error=>{console.error(error);process.exitCode=1;});", str(FRONTEND / "video-lease.js")],
        capture_output=True, text=True, check=True, timeout=12)
    return json.loads(result.stdout)


def test_web_locks_prevent_duplicate_fetches_and_allow_controlled_takeover():
    value = run_javascript(r'''
      const first=page('first'),second=page('second'),third=page('third');
      first.lease.setWanted(true);await wait(10);
      second.lease.setWanted(true);third.lease.setWanted(true);await wait(15);
      assert(first.lease.state().active);assert(!second.lease.state().active);assert(!third.lease.state().active);
      assert.strictEqual(second.lease.state().phase,'blocked');assert.strictEqual(second.downloads.length,0);
      second.lease.takeover();await wait(160);
      assert(second.lease.state().active);assert(!first.lease.state().active);assert(!third.lease.state().active);
      assert(first.downloads[0].signal.aborted);
      const afterTakeover={first:first.lease.state().active,second:second.lease.state().active,
        third:third.lease.state().active,firstAborted:first.downloads[0].signal.aborted};
      second.lease.setWanted(false);await wait(160);
      assert(second.downloads[0].signal.aborted);
      assert.strictEqual([first,second,third].filter(p=>p.lease.state().active).length,1);
      for(const p of [first,second,third])p.lease.setWanted(false);
      await wait(10);assert.strictEqual(held.size,0);
      console.log(JSON.stringify(afterTakeover));
    ''')
    assert value == {"first": False, "second": True, "third": False, "firstAborted": True}


def test_pagehide_releases_ownership_synchronously_without_starting_new_fetches():
    value = run_javascript(r'''
      const first=page('one'),second=page('two');
      first.lease.setWanted(true);await wait(10);second.lease.setWanted(true);await wait(10);
      const controller=first.downloads[0];first.win.dispatchEvent(new Event('pagehide'));
      assert(controller.signal.aborted);assert(!first.lease.state().wanted);assert(!first.lease.state().active);
      await wait(160);assert(second.lease.state().active);
      second.lease.setWanted(false);await wait(10);
      console.log(JSON.stringify({aborted:controller.signal.aborted,oldFetches:first.downloads.length,
        successorFetches:second.downloads.length}));
    ''')
    assert value == {"aborted": True, "oldFetches": 1, "successorFetches": 1}


def test_broadcast_fallback_elects_one_owner_and_honors_takeover():
    value = run_javascript(r'''
      const first=page('z',false),second=page('a',false);
      first.lease.setWanted(true);second.lease.setWanted(true);await wait(280);
      assert(!first.lease.state().active);assert(second.lease.state().active);
      first.lease.takeover();await wait(400);
      assert(first.lease.state().active);assert(!second.lease.state().active);assert(second.downloads[0].signal.aborted);
      first.lease.setWanted(false);second.lease.setWanted(false);await wait(10);
      console.log(JSON.stringify({method:first.lease.state().method,firstFetches:first.downloads.length,
        oldAborted:second.downloads[0].signal.aborted}));
    ''')
    assert value == {"method": "broadcast", "firstFetches": 1, "oldAborted": True}


def test_broadcast_fallback_recovers_expired_owner_without_credentials_or_storage():
    value = run_javascript(r'''
      const first=page('a',false),second=page('z',false);
      first.lease.setWanted(true);await wait(260);second.lease.setWanted(true);await wait(260);
      assert(first.lease.state().active);assert(!second.lease.state().active);
      first.lastChannel.close();clock+=6001;second.beat();await wait(260);
      assert(second.lease.state().active);
      first.lease.setWanted(false);second.lease.setWanted(false);
      console.log(JSON.stringify({recovered:second.downloads.length===1,method:second.lease.state().method}));
    ''')
    assert value == {"recovered": True, "method": "broadcast"}


def test_invalid_cross_tab_messages_do_not_release_current_owner():
    value = run_javascript(r'''
      const first=page('owner'),second=page('peer');first.lease.setWanted(true);await wait(10);
      for(const data of [null,{}, {kind:'takeover',id:'peer'}, {kind:'takeover',id:'peer',wanted:true,active:'yes',yielded:false},
        {kind:'takeover',id:'x'.repeat(129),wanted:true,active:true,yielded:false}])second.lastChannel.postMessage(data);
      await wait(10);const active=first.lease.state().active;assert(active);first.lease.setWanted(false);
      console.log(JSON.stringify({active}));
    ''')
    assert value == {"active": True}


def test_hidden_view_invalidation_aborts_all_transport_and_discards_pending_timers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for media invalidation regressions")
    script = r'''
    const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
    const controllers=Array.from({length:12},()=>new AbortController()),cleared=[],timers=[],overview=[];
    const context={global:{ObserverDetections:{invalidate:()=>controllers[11].abort()},addEventListener(){}},
      generation:0,nativeStatusRequest:controllers[0],nextNativeStatus:22,
      visualExpiryTimer:12,visualWakeTimer:13,mapImageExpiryTimer:14,visualLeases:new Map([['overview',{deadline:99}]]),
      visualValidators:new Map([['clouds','W/"cached-cloud-frame"']]),clearOverview:text=>overview.push(text),
      nativePlayers:new Map(controllers.slice(1,5).map((c,i)=>[i,{stop:()=>c.abort()}])),
      nativeSources:{one:{}},nativeDisplayStates:{one:{}},nativeUsage:{one:{}},nativeUsageItems:{one:{}},
      busy:new Map(controllers.slice(5,10).map((c,i)=>[i,c])),visualRequest:{controller:controllers[10]},
      lastRequest:{one:1},nextCameraPoll:{one:1},cameraPollTimers:{one:9},
      cameras:{front:'front',color:'color'},visuals:{map:'map'},clear:(key,text)=>cleared.push([key,text]),
      clearTimeout:timer=>timers.push(timer)};
    vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function invalidateView('),
      source.indexOf('  function stateLabel(')),context);context.invalidateView('已离开画面');
    assert(controllers.every(c=>c.signal.aborted));assert.strictEqual(context.nativePlayers.size,0);
    assert.strictEqual(context.busy.size,0);assert.strictEqual(context.visualRequest,null);
    assert.deepStrictEqual(Object.keys(context.cameraPollTimers),[]);assert.deepStrictEqual(timers,[14,12,13,9]);
    assert.strictEqual(context.mapImageExpiryTimer,null);
    assert.strictEqual(context.visualLeases.size,0);assert.strictEqual(context.visualExpiryTimer,null);assert.strictEqual(context.visualWakeTimer,null);
    assert.strictEqual(context.visualValidators.size,0);
    assert.deepStrictEqual(overview,['已离开画面']);
    console.log(JSON.stringify({aborted:controllers.filter(c=>c.signal.aborted).length,cleared:cleared.length}));
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"aborted": 12, "cleared": 3}


def test_late_status_401_after_leaving_camera_page_cannot_log_out_other_views():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for media cancellation regressions")
    script = r'''
    const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
    let resolveResponse,logouts=0,canceled=0;
    const context={generation:1,AbortController,performance:{now:()=>1000},nativeStatusRequest:null,nextNativeStatus:0,
      cameraPlaybackAllowed:()=>true,activeMediaSubpage:()=> 'cameras',
      fetch:()=>new Promise(resolve=>resolveResponse=resolve),setTimeout,clearTimeout,
      global:{CloudObserver:{unauthorized:()=>logouts++}}};
    context.scopedFetch=context.fetch;
    vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  async function nativeStatusTick('),
      source.indexOf('  function cameraTick(')),context);
    (async()=>{const pending=context.nativeStatusTick();context.generation++;
      context.nativeStatusRequest.abort();resolveResponse({status:401,body:{cancel:async()=>canceled++}});
      await pending;assert.strictEqual(logouts,0);assert.strictEqual(canceled,1);
      console.log(JSON.stringify({logouts,canceled}));})().catch(error=>{console.error(error);process.exitCode=1;});
    '''
    result = subprocess.run([node, "-e", script, str(FRONTEND / "media.js")], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"logouts": 0, "canceled": 1}


@pytest.mark.parametrize("use_locks", [True, False])
def test_takeover_owner_crash_expires_preference_and_restores_waiting_page(use_locks):
    value = run_javascript("const useLocks = " + str(use_locks).lower() + r''';
      const first=page('first',useLocks),second=page('second',useLocks);
      first.lease.setWanted(true);await wait(280);
      second.lease.setWanted(true);await wait(280);
      second.lease.takeover();await wait(400);
      assert(second.lease.state().active);assert(!first.lease.state().active);
      assert(first.downloads[0].signal.aborted);
      // A crashed document sends neither pagehide nor released; the browser
      // automatically releases its Web Lock, and its channel stops heartbeats.
      second.lastChannel.close();if(useLocks)held.clear();
      clock+=5999;first.beat();await wait(280);
      assert(!first.lease.state().active);
      clock+=2;first.beat();await wait(280);
      assert(first.lease.state().active);
      const recovered={active:first.lease.state().active,fetches:first.downloads.length,
        method:first.lease.state().method};
      first.lease.setWanted(false);second.lease.setWanted(false);await wait(10);
      console.log(JSON.stringify(recovered));
    ''')
    assert value == {"active": True, "fetches": 2,
                     "method": "web-locks" if use_locks else "broadcast"}


def test_expired_takeover_heartbeat_cannot_steal_live_web_lock():
    value = run_javascript(r'''
      const first=page('first'),second=page('second');
      first.lease.setWanted(true);await wait(10);
      second.lease.setWanted(true);await wait(10);
      second.lease.takeover();await wait(160);
      assert(second.lease.state().active);assert(!first.lease.state().active);
      // A frozen or delayed owner might retain its real lock. Only its lease
      // preference may expire; the waiting page must never duplicate downloads.
      second.lastChannel.close();clock+=6001;first.beat();await wait(280);
      assert(!first.lease.state().active);assert.strictEqual(held.size,1);
      const before={active:first.lease.state().active,fetches:first.downloads.length};
      first.lease.setWanted(false);second.lease.setWanted(false);await wait(10);
      assert.strictEqual(held.size,0);console.log(JSON.stringify(before));
    ''')
    assert value == {"active": False, "fetches": 1}


@pytest.mark.parametrize("use_locks", [True, False])
def test_simultaneous_takeovers_recover_exactly_one_owner_on_heartbeat(use_locks):
    value = run_javascript("const useLocks = " + str(use_locks).lower() + r''';
      const first=page('first',useLocks),second=page('second',useLocks);
      first.lease.setWanted(true);await wait(280);
      second.lease.setWanted(true);await wait(280);
      assert(first.lease.state().active);assert(!second.lease.state().active);
      // Both requests broadcast before either page handles its peer's request.
      // Both then yield; neither may keep waiting on an owner that also yielded.
      first.lease.takeover();second.lease.takeover();await wait(20);
      assert(!first.lease.state().active);assert(!second.lease.state().active);
      first.beat();second.beat();await wait(300);
      const owners=[first,second].filter(p=>p.lease.state().active).length;
      assert.strictEqual(owners,1);
      // Further heartbeats preserve a single winner instead of ping-ponging.
      first.beat();second.beat();await wait(300);
      assert.strictEqual([first,second].filter(p=>p.lease.state().active).length,1);
      const result={owners,method:first.lease.state().method};
      first.lease.setWanted(false);second.lease.setWanted(false);await wait(10);
      console.log(JSON.stringify(result));
    ''')
    assert value == {"owners": 1, "method": "web-locks" if use_locks else "broadcast"}
