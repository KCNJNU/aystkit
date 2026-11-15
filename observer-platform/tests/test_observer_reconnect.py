"""Exercise the deployed observation socket lifecycle with a fake clock and socket.

No real browser network, robot, camera, service, or production session is touched.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
let now = 0, nextTimer = 1, checks = 0;
const timers = new Map(), sockets = [], windowEvents = new Map(), documentEvents = new Map();
const renders = [], media = [], recorded = [];
let checkSession = () => Promise.resolve(true);
const listen = (map, name, fn) => map.set(name, [...(map.get(name) || []), fn]);
const dispatch = (map, name) => (map.get(name) || []).forEach(fn => fn());
const setTimer = (fn, delay, interval) => { const id = nextTimer++; timers.set(id, { fn, at: now + delay, interval }); return id; };
const settle = async () => { for (let i=0; i<8; i++) await Promise.resolve(); };
async function advance(ms) {
  const end = now + ms;
  while (true) {
    const pending = [...timers].filter(([, t]) => t.at <= end).sort((a,b) => a[1].at-b[1].at || a[0]-b[0])[0];
    if (!pending) break;
    const [id,timer] = pending; now = timer.at;
    if (timer.interval) timer.at += timer.interval; else timers.delete(id);
    timer.fn(); await settle();
  }
  now = end; await settle();
}
class Socket {
  static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
  constructor(url) { this.url=url; this.readyState=0; this.listeners=new Map(); this.closed=0; sockets.push(this); }
  addEventListener(name, fn) { listen(this.listeners,name,fn); }
  emit(name, data) {
    if (name==='open') this.readyState=1;
    if (name==='close') this.readyState=3;
    (this.listeners.get(name)||[]).forEach(fn => fn({ data }));
  }
  // Deliberately omit a close event: real half-open sockets may never deliver it.
  close() { this.closed++; this.readyState=2; }
}
const document = { hidden:false, addEventListener:(name,fn)=>listen(documentEvents,name,fn) };
const global = {
  location:{ protocol:'https:', host:'fixture.invalid' },
  addEventListener:(name,fn)=>listen(windowEvents,name,fn),
  CloudObserver:{ checkSession:()=>{ checks++; return checkSession(); }, update:(snapshot,fresh)=>renders.push({ snapshot, fresh }) },
  ObserverMedia:{ update:snapshot=>media.push(snapshot) }
};
const context = {
  global, document, WebSocket:Socket, performance:{ now:()=>now }, console, Promise,
  setTimeout:(fn,ms)=>setTimer(fn,ms,0), clearTimeout:id=>timers.delete(id),
  setInterval:(fn,ms)=>setTimer(fn,ms,ms), clearInterval:id=>timers.delete(id),
  readNumberPreference:(_name,value)=>value, readStringPreference:(_name,value)=>value,
  F:{ isFiniteNumber:value=>typeof value==='number' && Number.isFinite(value), normalizeStatus:value=>value,
      get:(value,path)=>path.split('.').reduce((v,key)=>v?.[key],value) },
  recordEvent:(...args)=>recorded.push(args), recordStateChanges:()=>{}, recordHistory:()=>{},
  renderHeader:()=>{}, renderOverview:()=>{}, renderSystem:()=>{}, renderRobot:()=>{}, renderSensors:()=>{},
  renderSTM32:()=>{}, renderROS:()=>{}, renderArucoPreview:()=>{}, setText:()=>{},
  requestAnimationFrame:()=>{}, Charts:{ drawAll:()=>{} }, historyStore:{}, pollOverviewMetrics:()=>{}
};
vm.createContext(context);
function section(start,end) {
  const a=source.indexOf(start),b=source.indexOf(end,a+start.length);
  assert(a>=0 && b>a, 'Production section must exist');
  vm.runInContext(source.slice(a,b),context);
}
section('  const state = {','  function readStringPreference(');
vm.runInContext('global.state = state;', context);
const state = global.state;
section('  function disconnectedTooLong(', '  function setText(');
section('  function renderSnapshot(', '  function applyViewMode(');
section('  function observationsWanted(', '  function receiveNativeMetrics(');
section('  document.addEventListener("visibilitychange"', '  global.addEventListener("cloud-auth-lost"');
const authStart=source.lastIndexOf('  global.addEventListener("cloud-auth-lost"');
vm.runInContext(source.slice(authStart,source.indexOf('  applyViewMode(',authStart)),context);
vm.runInContext('setInterval(updateClock, 1000);',context);
const invoke = name => context[name]();
const send = (socket=sockets.at(-1), online=true) => socket.emit('message',JSON.stringify({meta:{timestamp:100+now/1000},cloud:{online,status:online?'AVAILABLE':'OFFLINE'}}));
const timeoutCount = () => [...timers.values()].filter(t=>!t.interval).length;
const pending = () => { let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };

(async () => {
  if (scenario==='connecting') {
    invoke('connectWebSocket'); const old=sockets[0];
    await advance(9999); assert.equal(sockets.length,1); assert.equal(old.closed,0);
    await advance(1); assert.equal(old.closed,1); assert.equal(state.socket,null);
    await advance(2000); assert.equal(sockets.length,2); assert.equal(checks,1);
    await advance(9999); assert.equal(sockets[1].closed,0,'new attempt gets its own full deadline');
    await advance(1); assert.equal(sockets[1].closed,1);
  } else if (scenario==='no_first_message') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open');
    await advance(10000); assert.equal(old.closed,1);
    assert.equal(state.lastMessageAt,null); assert.equal(media.length,0);
    await advance(2000); assert.equal(sockets.length,2);
  } else if (scenario==='invalid_messages') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open');
    for (const value of ['broken','null','[]','{}','{"meta":true}','{"meta":{}}','{"meta":{"timestamp":"0"}}','{"meta":[]}']) {
      await advance(1000); old.emit('message',value);
    }
    assert.equal(state.lastMessageAt,null); assert.equal(media.length,0);
    await advance(2000); assert.equal(old.closed,1,'invalid packets cannot feed watchdog');
  } else if (scenario==='stale_semantics') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open'); send(old);
    await advance(3501); assert.equal(invoke('disconnectedTooLong'),true); assert.equal(old.closed,0);
    await advance(6499); assert.equal(old.closed,1); assert.equal(state.lastMessageAt,0);
    await advance(2000); const replacement=sockets[1]; replacement.emit('open');
    assert.equal(state.lastMessageAt,0); assert.equal(renders.at(-1).fresh,false);
    assert.equal(media.length,1,'cached redraw/open cannot publish a fresh media sample');
    await advance(1000); send(replacement,false);
    assert.equal(state.lastMessageAt,now); assert.equal(renders.at(-1).fresh,true);
    assert.equal(renders.at(-1).snapshot.cloud.online,false,'fresh connection does not claim robot is online');
  } else if (scenario==='healthy_stream') {
    invoke('connectWebSocket'); const socket=sockets[0]; socket.emit('open');
    for (let i=0;i<30;i++) { send(socket); await advance(1000); }
    assert.equal(sockets.length,1); assert.equal(socket.closed,0); assert.equal(checks,0);
    assert.equal(timeoutCount(),0,'clock needs no added perpetual retry timers');
  } else if (scenario==='old_socket_events') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open'); send(old);
    await advance(12000); const replacement=sockets[1]; replacement.emit('open'); send(replacement,false);
    const freshAt=state.lastMessageAt,count=media.length;
    old.emit('message',JSON.stringify({meta:{timestamp:999},cloud:{online:true}}));
    old.emit('close'); old.emit('error'); old.emit('open');
    assert.equal(state.socket,replacement); assert.equal(state.lastMessageAt,freshAt);
    assert.equal(state.connected,true); assert.equal(media.length,count); assert.equal(timeoutCount(),0);
  } else if (scenario==='online_dedup') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open'); send(old);
    await advance(4000); const gate=pending(); checkSession=()=>gate.promise;
    for(let i=0;i<20;i++) dispatch(windowEvents,'online');
    assert.equal(old.closed,1); assert.equal(checks,1); assert.equal(sockets.length,1);
    gate.resolve(true); await settle(); assert.equal(sockets.length,2); assert.equal(timeoutCount(),0);
    for(let i=0;i<20;i++) dispatch(windowEvents,'online');
    assert.equal(sockets.length,2); assert.equal(checks,1);
    sockets[1].emit('open'); send(sockets[1]); dispatch(windowEvents,'online');
    assert.equal(sockets.length,2,'online must not churn healthy connections');
  } else if (scenario==='hidden_lifecycle') {
    invoke('connectWebSocket'); const socket=sockets[0]; socket.emit('open'); send(socket);
    document.hidden=true; dispatch(documentEvents,'visibilitychange'); dispatch(windowEvents,'online');
    await advance(30000); assert.equal(sockets.length,1); assert.equal(checks,0); assert.equal(timeoutCount(),0);
    document.hidden=false; dispatch(documentEvents,'visibilitychange'); await settle();
    assert.equal(sockets.length,2); assert.equal(state.lastMessageAt,0);
    dispatch(windowEvents,'pagehide'); dispatch(windowEvents,'online'); await advance(20000);
    assert.equal(sockets.length,2); assert.equal(timeoutCount(),0);
    dispatch(windowEvents,'pageshow'); await settle(); assert.equal(sockets.length,3);
  } else if (scenario==='cancel_pending_session') {
    const gate=pending(); checkSession=()=>gate.promise;
    dispatch(windowEvents,'online'); assert.equal(checks,1); assert.equal(timeoutCount(),1);
    document.hidden=true; dispatch(documentEvents,'visibilitychange'); await settle();
    assert.equal(timeoutCount(),0); gate.resolve(true); await settle(); assert.equal(sockets.length,0);
    document.hidden=false; checkSession=()=>Promise.resolve(true);
    dispatch(documentEvents,'visibilitychange'); await settle(); assert.equal(sockets.length,1);
  } else if (scenario==='hung_session') {
    checkSession=()=>new Promise(()=>{});
    dispatch(windowEvents,'online'); await advance(5499); assert.equal(sockets.length,0);
    await advance(1); assert.equal(sockets.length,1,'unknown session check must not lock recovery forever');
    assert.equal(timeoutCount(),0); assert.equal(state.observationResume,null);
  } else if (scenario==='initial_session_hung') {
    global.CloudObserver.ready=new Promise(()=>{});
    const start=source.indexOf('  if (global.CloudObserver) void resumeObservations(');
    assert(start>=0);
    vm.runInContext(source.slice(start,source.indexOf('  global.RKObserverDashboard',start)),context);
    dispatch(windowEvents,'online'); assert.equal(checks,0,'initial session promise is reused');
    await advance(5500); assert.equal(sockets.length,1); assert.equal(timeoutCount(),0);
  } else if (scenario==='late_session_result') {
    const old=pending(); checkSession=()=>old.promise; dispatch(windowEvents,'online');
    dispatch(windowEvents,'pagehide'); await settle();
    const current=pending(); checkSession=()=>current.promise; dispatch(windowEvents,'pageshow');
    old.resolve(true); await settle(); assert.equal(sockets.length,0,'prior lifecycle result must not resume');
    current.resolve(true); await settle(); assert.equal(sockets.length,1); assert.equal(timeoutCount(),0);
  } else if (scenario==='auth_expired') {
    invoke('connectWebSocket'); sockets[0].emit('open'); send();
    sockets[0].emit('error'); assert.equal(timeoutCount(),1);
    dispatch(windowEvents,'cloud-auth-lost'); assert.equal(timeoutCount(),0);
    dispatch(windowEvents,'online'); dispatch(windowEvents,'pageshow'); await advance(30000);
    assert.equal(sockets.length,1); assert.equal(checks,0); assert.equal(state.socket,null);
  } else if (scenario==='auth_during_check') {
    const gate=pending(); checkSession=()=>gate.promise; dispatch(windowEvents,'online');
    dispatch(windowEvents,'cloud-auth-lost'); gate.resolve(true); await settle(); await advance(20000);
    assert.equal(sockets.length,0); assert.equal(timeoutCount(),0);
  } else if (scenario==='denied_or_failed_session') {
    checkSession=()=>Promise.resolve(false); dispatch(windowEvents,'online'); await settle();
    assert.equal(sockets.length,0); assert.equal(timeoutCount(),0);
    checkSession=()=>Promise.reject(new Error('offline')); dispatch(windowEvents,'online'); await settle();
    assert.equal(sockets.length,1,'transport failure is not an auth success or rejection; server still enforces auth');
  } else if (scenario==='repeated_close_and_error') {
    invoke('connectWebSocket'); const old=sockets[0]; old.emit('open');
    old.emit('error'); old.emit('close'); old.emit('error');
    assert.equal(timeoutCount(),1); await advance(2000); assert.equal(sockets.length,2); assert.equal(checks,1);
    assert.equal(timeoutCount(),0);
  } else { throw Error('Unknown scenario: '+scenario); }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


@pytest.mark.parametrize('scenario', [
    'connecting', 'no_first_message', 'invalid_messages', 'stale_semantics',
    'healthy_stream', 'old_socket_events', 'online_dedup', 'hidden_lifecycle',
    'cancel_pending_session', 'hung_session', 'initial_session_hung', 'late_session_result',
    'auth_expired', 'auth_during_check', 'denied_or_failed_session',
    'repeated_close_and_error',
])
def test_observation_socket_recovers_without_refreshing_stale_data(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for isolated socket lifecycle regression')
    subprocess.run([node, '-e', HARNESS, str(ROOT / 'cloud/frontend/app.js'), scenario],
                   check=True, capture_output=True, text=True, timeout=10)
