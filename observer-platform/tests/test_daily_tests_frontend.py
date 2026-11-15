"""Daily test UI behavior with isolated requests; no robot or production access."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
FRONTEND=ROOT/'cloud/frontend'


def test_daily_test_page_registers_independent_assets_and_route():
    html=(FRONTEND/'index.html').read_text()
    script=(FRONTEND/'daily-tests.js').read_text()
    assert 'data-page="tests"' in html and 'data-page-name="tests"' in html
    assert '/daily-tests.js' in html and '/daily-tests.css' in html
    assert '"logs", "tests"' in (FRONTEND/'app.js').read_text()
    for name in ('start','pause','resume','finish'):
        assert f'id="tests-{name}"' in html
    assert 'Asia/Shanghai' in script and '.innerHTML' not in script
    assert 'observer-page-change' in script and 'visibilitychange' in script and 'cloud-auth-lost' in script
    assert 'setTimeout(refresh, 5000)' in script
    assert '/cmd_vel' not in script and 'WebSocket' not in script
    assert 'id="tests-tester-name"' in html and 'required aria-describedby="tests-message"' in html
    assert 'localStorage' not in script and 'sessionStorage' not in script


def test_server_state_controls_unknown_distance_multitab_conflicts_and_request_lifecycle():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for isolated UI interactions')
    test=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const listeners={},documentListeners={},timers=new Map(),requests=[],downloads=[];let timerId=0,focused=null;
function listen(target,name,callback){(target[name]??=[]).push(callback);}
function fire(target,name,event={}){for(const callback of target[name]||[])callback(event);}
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.dataset={};this.attributes={};this.value='';this.hidden=false;this.disabled=false;this._text='';const classes=new Set();this.classList={toggle:(name,on)=>on?classes.add(name):classes.delete(name),add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name)};}
 set innerHTML(_){throw Error('unsafe HTML sink');}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(v=>v.textContent).join('');}
 setAttribute(name,value){this.attributes[name]=value;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[];this._text='';for(const n of nodes)this.append(...(n.tagName==='#fragment'?n.children:[n]));}
 addEventListener(name,callback){listen(this.events,name,callback);}
 click(){if(this.tagName==='a')downloads.push({name:this.download,href:this.href});else fire(this.events,'click');}
 focus(){if(!this.disabled)focused=this;}
 remove(){}
}
const html=fs.readFileSync(process.argv[2],'utf8'),ids={};
for(const match of html.matchAll(/id="(tests-[^"]+|page-tests)"/g))ids[match[1]]=new Element();
const exports=[new Element('button'),new Element('button')];exports[0].dataset.testsExport='csv';exports[1].dataset.testsExport='json';
ids['page-tests'].querySelectorAll=selector=>selector==='[data-tests-export]'?exports:[];
const document={hidden:false,body:new Element('body'),getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>new Element(tag),createDocumentFragment:()=>new Element('#fragment'),addEventListener:(name,cb)=>listen(documentListeners,name,cb)};
const window={RKObserverDashboard:{getRoute:()=> 'overview'},crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},addEventListener:(name,cb)=>listen(listeners,name,cb),location:{replace:path=>window.redirect=path},CloudObserver:{ready:Promise.resolve(true),unauthorized:()=>window.authLost=true}};
const fetch=(url,options)=>new Promise((resolve,reject)=>{let done=false;const req={url,options,finish(value){if(done)return;done=true;resolve(value);},fail(){if(done)return;done=true;reject(Error('network unavailable'));}};options.signal.addEventListener('abort',()=>req.fail());requests.push(req);});
const context={window,document,fetch,AbortController,Intl,Date,Number,JSON,Object,Array,String,Math,Promise,console,setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:id=>timers.delete(id),URL:{createObjectURL:()=> 'blob:daily-tests',revokeObjectURL:()=>{}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=(body,status=200)=>({status,ok:status>=200&&status<300,json:async()=>body,blob:async()=>({})});
const day={date:'2026-10-25',duration_s:600,distance_m:null,sessions:1,valid_seconds:0,quality:'waiting'};
function data(revision,active=null,source={online:false,odom_status:'WAITING_UPSTREAM',applied_revision:0,last_seen:null},override={}){return {server_time:1792919400,timezone:'Asia/Shanghai',viewer:{username:'alice',can_view_all:false},revision,active,summary:day,days:[day],runs:active?[active]:[],source,...override};}
const active={id:'test-run',created_by:'alice',name:'<img src=x onerror=alert(1)>',tester_name:'<b>张三</b>',state:'running',started_at:1792919000,ended_at:null,duration_s:400,distance_m:null,valid_seconds:0,quality:'waiting'};
function poll(){const entry=[...timers].find(([,v])=>v.delay===5000);assert(entry,'poll expected');timers.delete(entry[0]);entry[1].callback();}
(async()=>{
 await settle();assert.equal(requests.length,0,'inactive page does not poll');
 assert.equal(window.ObserverDailyTests.duration(3599.9),'00:59:59');assert.equal(window.ObserverDailyTests.duration(null),'—');assert.equal(window.ObserverDailyTests.distance(null),'—');assert.equal(window.ObserverDailyTests.distance(0),'0.0');assert.equal(window.ObserverDailyTests.time(0),'1970-01-01 08:00:00');
 fire(listeners,'observer-page-change',{detail:{page:'tests'}});await settle();assert.equal(requests[0].url,'/api/tests?days=30');ids['tests-tester-name'].value='填写中的姓名';const other={...active,id:'other-run',created_by:'<img src=x onerror=alert(1)>',name:'其他账号正在测试'};
 requests[0].finish(response(data(0,null,{online:false,odom_status:'WAITING_UPSTREAM',applied_revision:99,syncing:false},{viewer:{username:'observer',can_view_all:true},own_summary:{...day,duration_s:120,sessions:1},totals:{duration_s:7200,sessions:8},users:[{username:other.created_by,summary:{duration_s:480},total:{duration_s:7080,sessions:7},active_state:'running',active_duration_s:65},{username:'observer',summary:{duration_s:120},total:{duration_s:120,sessions:1},active_state:'idle',active_duration_s:null}],runs:[other]})));await settle();assert.equal(ids['tests-tester-name'].value,'填写中的姓名','polling never replaces an in-progress name');
 assert(!ids['tests-admin-overview'].hidden);assert.equal(ids['tests-all-duration'].textContent,'02:00:00');assert.equal(ids['tests-today-duration'].textContent,'00:10:00');assert.equal(ids['tests-summary-scope'].textContent,'全部账号记录');
 assert.equal(ids['tests-today-duration-label'].textContent,'今日全部账号时长');assert.equal(ids['tests-current-account'].textContent,'账号：observer');assert(ids['tests-own-summary'].textContent.includes('00:02:00'));
 assert.equal(ids['tests-users-body'].children.length,2);const userRow=ids['tests-users-body'].children[0];assert.equal(userRow.children[0].textContent,other.created_by);assert.equal(userRow.children[1].textContent,'00:08:00');assert.equal(userRow.children[2].textContent,'01:58:00');assert.equal(userRow.children[3].textContent,'7');assert(userRow.children[4].textContent.includes('进行中')&&userRow.children[4].textContent.includes('00:01:05'));
 assert.equal(ids['tests-users-body'].children[1].children[4].textContent,'尚未开始');assert(ids['tests-runs-list'].textContent.includes('账号：'+other.created_by));assert(!ids['tests-start'].hidden&&ids['tests-pause'].hidden,'another account running must never become my controllable active test');assert(ids['tests-sync-status'].hidden,'explicit source sync overrides unrelated global revision');
 assert.equal(ids['tests-today-distance'].textContent,'—');assert.equal(ids['tests-source-status'].textContent,'采集端离线');assert(!ids['tests-start'].disabled);
 assert(!ids['tests-registration-fields'].hidden&&!ids['tests-tester-name-wrap'].hidden&&ids['tests-run-tester'].hidden);
 for(const [value,message] of [['','请填写测试人员姓名'],['   ','请填写测试人员姓名'],['张'.repeat(61),'最多 60'],['张\u0000三','控制字符'],['张\u007f三','控制字符'],['\t张三','控制字符'],['张\u200b三','控制字符'],['张\u2028三','控制字符'],['张\ud800三','控制字符']]){
  ids['tests-tester-name'].value=value;ids['tests-start'].click();await settle();assert.equal(requests.length,1,'invalid registration never posts');assert(ids['tests-message'].textContent.includes(message));assert.strictEqual(focused,ids['tests-tester-name']);assert.equal(ids['tests-tester-name'].attributes['aria-invalid'],'true');
 }
 ids['tests-tester-name'].value='  '+active.tester_name+'  ';fire(ids['tests-tester-name'].events,'input');assert.equal(ids['tests-tester-name'].attributes['aria-invalid'],'false');
 ids['tests-name'].value=active.name;ids['tests-start'].click();await settle();const start=requests[1];assert.equal(start.options.method,'POST');assert.equal(start.options.headers['Content-Type'],'application/json');
 assert.equal(JSON.parse(start.options.body).action,'start');assert.equal(JSON.parse(start.options.body).revision,0);assert.equal(JSON.parse(start.options.body).name,active.name);assert.equal(JSON.parse(start.options.body).tester_name,active.tester_name);
 assert(ids['tests-start'].disabled&&!ids['tests-start'].hidden,'pending action must not optimistically change state');
 start.finish(response(data(1,active,{online:true,odom_status:'AVAILABLE',applied_revision:0},{days:[{...day,tester_names:[active.tester_name,'李四','']},{...day,date:'2026-10-24',sessions:0,tester_names:[]}]})));await settle();
 assert(ids['tests-admin-overview'].hidden&&ids['tests-users-body'].children.length===0,'ordinary account cannot retain previous administrator rows');assert.equal(ids['tests-all-duration'].textContent,'—');assert.equal(ids['tests-summary-scope'].textContent,'我的记录');assert.equal(ids['tests-current-account'].textContent,'账号：alice');assert(ids['tests-runs-list'].textContent.includes('账号：alice'));
 assert.equal(ids['tests-run-name'].textContent,active.name);assert.equal(ids['tests-run-tester'].textContent,'测试人员：'+active.tester_name);assert(!ids['tests-run-tester'].hidden&&ids['tests-tester-name-wrap'].hidden&&ids['tests-registration-fields'].hidden);
 assert(ids['tests-runs-list'].textContent.includes('测试人员：'+active.tester_name));assert.equal(ids['tests-days-body'].children[0].children[5].textContent,active.tester_name+'、李四、未登记');assert.equal(ids['tests-days-body'].children[1].children[5].textContent,'—');
 ids['tests-tester-name'].value='';assert.equal(ids['tests-source-status'].textContent,'同步中');assert(!ids['tests-sync-status'].hidden);assert(ids['tests-start'].hidden&&!ids['tests-pause'].hidden&&!ids['tests-pause'].disabled);
 ids['tests-pause'].click();await settle();assert(!ids['tests-pause'].hidden&&ids['tests-resume'].hidden);assert.equal(JSON.parse(requests[2].options.body).revision,1);assert.deepEqual(Object.keys(JSON.parse(requests[2].options.body)).sort(),['action','command_id','revision'],'control requests cannot name another account or run');assert(!('tester_name' in JSON.parse(requests[2].options.body)));requests[2].finish(response({error:'conflict'},409));await settle();assert.equal(requests[3].url,'/api/tests?days=30');
 const paused={...active,state:'paused',tester_name:''};requests[3].finish(response(data(2,paused,{online:true,odom_status:'AVAILABLE',applied_revision:2})));await settle();assert(ids['tests-pause'].hidden&&!ids['tests-resume'].hidden);assert(ids['tests-sync-status'].hidden);assert.equal(ids['tests-run-tester'].textContent,'测试人员：未登记');assert(ids['tests-runs-list'].textContent.includes('测试人员：未登记'));assert(ids['tests-days-body'].textContent.includes('未登记'));
 ids['tests-resume'].click();await settle();assert(!('tester_name' in JSON.parse(requests[4].options.body)));requests[4].finish(response(data(3,{...active,distance_m:0,quality:'partial',valid_seconds:1},{online:true,odom_status:'AVAILABLE',applied_revision:3},{summary:{...day,distance_m:0,quality:'partial'}})));await settle();assert.equal(ids['tests-today-distance'].textContent,'0.0');assert.equal(ids['tests-distance-quality'].textContent,'部分记录');
 ids['tests-finish'].click();await settle();assert(!('tester_name' in JSON.parse(requests[5].options.body)));requests[5].finish(response(data(4,null,{online:true,odom_status:'AVAILABLE',applied_revision:4})));await settle();assert(!ids['tests-start'].hidden&&ids['tests-finish'].hidden);
 poll();await settle();requests[6].fail();await settle();assert(ids['tests-start'].disabled);assert(ids['tests-updated'].textContent.includes('连接中断'));assert.equal(ids['tests-today-distance'].textContent,'—');
 ids['tests-refresh'].click();await settle();requests[7].finish(response(data(4,null,{online:true,odom_status:'AVAILABLE',applied_revision:4})));await settle();assert(!ids['tests-start'].disabled);
 focused=null;ids['tests-tester-name'].value='王五';ids['tests-start'].click();await settle();assert.equal(JSON.parse(requests[8].options.body).tester_name,'王五');requests[8].finish(response({error:'测试人员姓名无效'},400));await settle();assert.equal(ids['tests-message'].textContent,'测试人员姓名无效');assert.equal(requests.length,9,'a rejected registration is not treated as unknown network success');assert(!ids['tests-start'].disabled&&!ids['tests-registration-fields'].hidden);assert.equal(ids['tests-tester-name'].value,'王五');assert.equal(ids['tests-tester-name'].attributes['aria-invalid'],'true');assert.strictEqual(focused,ids['tests-tester-name']);
 exports[0].click();await settle();assert.equal(requests[9].url,'/api/tests/export?days=30&format=csv');requests[9].finish(response({}));await settle();assert(downloads[0].name.endsWith('.csv'));
 exports[1].click();await settle();requests[10].finish(response({}));await settle();assert(downloads[1].name.endsWith('.json'));
 fire(listeners,'observer-page-change',{detail:{page:'overview'}});assert(![...timers.values()].some(v=>v.delay===5000));
 fire(listeners,'observer-page-change',{detail:{page:'tests'}});await settle();assert(requests[11]);document.hidden=true;fire(documentListeners,'visibilitychange');await settle();assert(requests[11].options.signal.aborted);assert(![...timers.values()].some(v=>v.delay===5000));
 document.hidden=false;fire(documentListeners,'visibilitychange');await settle();requests[12].finish(response({},401));await settle();assert(window.authLost);assert(![...timers.values()].some(v=>v.delay===5000));
 console.log('daily test UI lifecycle and authoritative state passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',test,str(FRONTEND/'daily-tests.js'),str(FRONTEND/'index.html')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+'\n'+result.stderr


def test_explicit_source_sync_is_independent_from_personal_revision():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for daily test synchronization behavior')
    code=r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),elements={};
const context={state:{serviceLost:false},runNames:{running:'进行中',paused:'已暂停'},
 byId(id){return elements[id]??=( {dataset:{},hidden:false,textContent:''} );},
 text(id,value){context.byId(id).textContent=String(value);}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('  function sourceStatus('),source.indexOf('  function badge(')),context);
const base={revision:2,active:{state:'paused'},source:{online:true,odom_status:'AVAILABLE',applied_revision:99,syncing:false}};
context.sourceStatus(base);assert.equal(elements['tests-source-status'].textContent,'里程已就绪');assert(elements['tests-sync-status'].hidden);assert.equal(elements['tests-run-state'].textContent,'已暂停');
context.sourceStatus({...base,source:{...base.source,applied_revision:2,syncing:true}});assert.equal(elements['tests-source-status'].textContent,'同步中');assert(!elements['tests-sync-status'].hidden);
context.sourceStatus({...base,active:null,source:{...base.source,syncing:true}});assert(elements['tests-sync-status'].hidden,'idle personal test must not display another account synchronization badge');assert.equal(elements['tests-source-status'].textContent,'同步中');
const legacy={...base,source:{online:true,odom_status:'AVAILABLE',applied_revision:1}};context.sourceStatus(legacy);assert.equal(elements['tests-source-status'].textContent,'同步中');
context.sourceStatus({...base,source:{...base.source,syncing:null}});assert.equal(elements['tests-source-status'].textContent,'同步中');
console.log('personal/global revisions and explicit synchronization passed');'''
    result=subprocess.run([node,'-e',code,str(FRONTEND/'daily-tests.js')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+'\n'+result.stderr


def test_editable_drafts_survive_read_and_save_failures_without_duplicate_posts():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for isolated draft interactions')
    code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),source=fs.readFileSync(process.argv[1],'utf8');
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=(body,status=200)=>({status,ok:status>=200&&status<300,json:async()=>body});
const snapshot={server_time:1792919400,revision:7,active:null,viewer:{username:'alice',can_view_all:false},summary:{date:'2026-10-25'},days:[],runs:[],source:{online:false},test_issues:{today:'2026-10-25',issues:[],pending_review:[],can_start:true}};
function boot(){
 const listeners={},documentListeners={},timers=new Map(),requests=[],ids={};let timerId=0,focused=null;
 const listen=(target,name,cb)=>(target[name]??=[]).push(cb);
 const fire=(target,name,event={})=>{for(const cb of target[name]||[])cb(event);};
 class Element{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.dataset={};this.attributes={};this.value='';this.hidden=false;this._disabled=false;this._text='';const classes=new Set();this.classList={toggle:(n,on)=>on?classes.add(n):classes.delete(n),add:n=>classes.add(n),remove:n=>classes.delete(n)};}
  set disabled(value){this._disabled=Boolean(value);if(this._disabled&&focused===this)focused=null;}
  get disabled(){return this._disabled;}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  set innerHTML(_){throw Error('unsafe HTML sink');}
  setAttribute(name,value){this.attributes[name]=value;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[];this._text='';for(const n of nodes)this.append(...(n.tagName==='#fragment'?n.children:[n]));}
  addEventListener(name,cb){listen(this.events,name,cb);}
  click(){if(!this.disabled)fire(this.events,'click');}
  focus(){if(!this.disabled)focused=this;}
 }
 for(const match of html.matchAll(/id="(tests-[^"]+|page-tests)"/g))ids[match[1]]=new Element();
 ids['page-tests'].querySelectorAll=()=>[];
 const document={hidden:false,get activeElement(){return focused;},getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),createElementNS:(_,tag)=>new Element(tag),createDocumentFragment:()=>new Element('#fragment'),addEventListener:(n,cb)=>listen(documentListeners,n,cb)};
 const window={RKObserverDashboard:{getRoute:()=> 'tests'},crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},addEventListener:(n,cb)=>listen(listeners,n,cb),location:{replace:path=>window.redirect=path},CloudObserver:{ready:Promise.resolve(true),checkSession:async()=>true,unauthorized:()=>window.authLost=true}};
 const fetch=(url,options)=>new Promise((resolve,reject)=>{let done=false;const req={url,options,finish(body,status=200){if(done)return;done=true;resolve(response(body,status));},fail(){if(done)return;done=true;reject(Error('network unavailable'));}};options.signal.addEventListener('abort',()=>req.fail());requests.push(req);});
 const context={window,document,fetch,AbortController,Intl,Date,Number,JSON,Object,Array,String,Math,Promise,console,setTimeout:(cb,delay)=>{timers.set(++timerId,{cb,delay});return timerId;},clearTimeout:id=>timers.delete(id)};
 vm.createContext(context);vm.runInContext(source,context);
 const runTimer=delay=>{const entry=[...timers].find(([,timer])=>timer.delay===delay);assert(entry,`expected ${delay}ms timer`);timers.delete(entry[0]);entry[1].cb();};
 return {ids,requests,window,document,timers,runTimer,submit(){fire(ids['tests-issue-form'].events,'submit',{preventDefault(){}});},posts(){return requests.filter(req=>req.options.method==='POST');}};
}
function assertDraftEditable(ui){
 for(const id of ['tests-tester-name','tests-name','tests-issue-description','tests-issue-category'])assert(!ui.ids[id].disabled,`${id} must remain editable`);
}
(async()=>{
 const ui=boot();await settle();const {ids,requests}=ui;
 assert.equal(requests[0].url,'/api/tests?days=30');assertDraftEditable(ui);assert(ids['tests-start'].disabled,'starting still requires a server revision');
 ids['tests-tester-name'].value='填写中的张三';ids['tests-name'].value='地库路线';ids['tests-issue-description'].value='雷达画面卡顿';ids['tests-issue-category'].value='雷达';
 const name=ids['tests-tester-name'];name.selectionStart=3;name.selectionEnd=3;name.composing=true;name.focus();
 requests[0].fail();await settle();assertDraftEditable(ui);assert(ids['tests-start'].disabled);assert.strictEqual(ui.document.activeElement,name);assert.equal(name.value,'填写中的张三');assert.equal(name.selectionStart,3);assert(name.composing,'a failed read must not interrupt composition');
 assert.equal(requests.at(-1).url,'/api/tests/issues?days=365','notes remain independently readable when statistics fail');requests.at(-1).finish({today:'2026-10-25',issues:[{description:'已有问题',issue_day:'2026-10-25'}],pending_review:[],can_start:true});await settle();assert(ids['tests-issues-current'].textContent.includes('已有问题'));assert(ids['tests-start'].disabled,'a successful notes read cannot enable controls without a statistics revision');
 ids['tests-start'].click();await settle();assert.equal(ui.posts().length,0,'no control command without a valid snapshot');

 ui.submit();await settle();let post=ui.posts().at(-1);assert.equal(post.url,'/api/tests/issues','issue recording is independent from the statistics endpoint');
 assert(ids['tests-issue-description'].disabled&&ids['tests-issue-add'].disabled,'freeze only the in-flight issue form');
 ui.submit();await settle();assert.equal(ui.posts().length,1,'a second submit cannot duplicate the pending POST');
 post.finish({error:'暂时无法保存'},503);await settle();assert.equal(ids['tests-issue-description'].value,'雷达画面卡顿');assert(ids['tests-message'].textContent.includes('暂时无法保存'));assertDraftEditable(ui);assert.equal(ui.posts().length,1);

 ui.submit();await settle();post=ui.posts().at(-1);ui.runTimer(15000);await settle();assert(post.options.signal.aborted);assert.equal(ids['tests-issue-description'].value,'雷达画面卡顿');assert(ids['tests-message'].textContent.includes('提交结果待确认'));assert.equal(ui.posts().length,2,'a timeout must never automatically retry a possibly committed POST');assertDraftEditable(ui);

 ui.submit();await settle();post=ui.posts().at(-1);post.finish({id:'saved'},201);await settle();let read=requests.at(-1);assert.equal(read.url,'/api/tests/issues?days=365');read.finish({today:'2026-10-25',issues:[],pending_review:[],can_start:true});await settle();assert.equal(ids['tests-issue-description'].value,'','only confirmed POST success clears the submitted draft');assert.equal(ui.posts().length,3);

 ids['tests-issue-description'].value='先提交的问题';ui.submit();await settle();post=ui.posts().at(-1);ids['tests-issue-description'].value='随后完成输入的新问题';post.finish({id:'saved2'},201);await settle();requests.at(-1).finish({today:'2026-10-25',issues:[],pending_review:[],can_start:true});await settle();assert.equal(ids['tests-issue-description'].value,'随后完成输入的新问题','late responses must not clear a newer IME or local edit');

 name.focus();ids['tests-refresh'].click();await settle();requests.at(-1).finish(snapshot);await settle();assertDraftEditable(ui);assert(!ids['tests-start'].disabled,'controls recover after a successful authoritative read');assert.strictEqual(ui.document.activeElement,name);assert.equal(name.value,'填写中的张三');assert.equal(ids['tests-name'].value,'地库路线');assert.equal(name.selectionStart,3);assert(name.composing);
 ui.runTimer(5000);await settle();requests.at(-1).fail();await settle();assertDraftEditable(ui);assert(ids['tests-start'].disabled);assert.strictEqual(ui.document.activeElement,name,'a later polling failure must not blur an input');assert.equal(ids['tests-issue-description'].value,'随后完成输入的新问题');
 ids['tests-refresh'].click();await settle();requests.at(-1).finish({...snapshot,test_issues:{...snapshot.test_issues,can_start:false,pending_review:[{id:'old',description:'昨日问题'}]}});await settle();assert(ids['tests-start'].disabled,'editing does not bypass the previous-day issue gate');assertDraftEditable(ui);
 ids['tests-refresh'].click();await settle();requests.at(-1).finish(snapshot);await settle();ids['tests-start'].click();await settle();post=ui.posts().at(-1);assert.equal(post.url,'/api/tests/control');assert.equal(JSON.parse(post.options.body).revision,7);assert.equal(JSON.parse(post.options.body).tester_name,'填写中的张三');assert(ids['tests-start'].disabled);post.finish({error:'登记无效'},400);await settle();assertDraftEditable(ui);assert.equal(name.value,'填写中的张三');

 const denied=boot();await settle();denied.requests[0].fail();await settle();denied.requests.at(-1).finish({today:'2026-10-25',issues:[],pending_review:[],can_start:true});await settle();denied.ids['tests-issue-description'].value='不能丢失的权限错误草稿';denied.ids['tests-issue-category'].value='通用';denied.submit();await settle();denied.posts()[0].finish({error:'forbidden'},403);await settle();assert.equal(denied.ids['tests-issue-description'].value,'不能丢失的权限错误草稿');assert(denied.ids['tests-issue-description'].disabled&&denied.ids['tests-tester-name'].disabled,'expired permissions still disable submissions and inputs');assert.equal(denied.window.redirect,'/pending');assert.equal(denied.posts().length,1);
 console.log('editable drafts, IME focus, failed/unknown saves, successful saves and authorization passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',code,str(FRONTEND/'daily-tests.js'),str(FRONTEND/'index.html')],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
