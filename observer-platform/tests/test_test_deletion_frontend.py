"""Exercise administrator timer deletion, confirmation, busy state and refresh."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]

def test_test_deletion_permissions_confirmation_errors_and_refreshed_totals():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const html=fs.readFileSync(process.argv[2],'utf8');
const drain=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.events={};this.dataset={};this.value='';this.disabled=false;this.hidden=false;this._text='';this.classList={toggle(){},add(){},remove(){}};}
 set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
 append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items.flatMap(x=>x.tagName==='fragment'?x.children:[x]);}
 setAttribute(k,v){this[k]=v;}addEventListener(k,fn){this.events[k]=fn;}
 querySelectorAll(){return this.children.flatMap(descendants).filter(x=>x.dataset.deleteTestId);}
}
const descendants=x=>[x,...x.children.flatMap(descendants)];
async function setup(admin=true){
 const ids={};for(const m of html.matchAll(/id="(tests-[^"]+|page-tests)"/g))ids[m[1]]=new Element();
 const state={requests:[],redirects:[],confirm:true,prompts:[],mode:'ok',run:{id:'00000000-0000-4000-8000-000000000001',created_by:'deleted-user',tester_name:'测试员',name:'<b>遗留测试</b>',state:'running',started_at:1794000000,ended_at:null,duration_s:60,quality:'waiting'}};
 ids['page-tests'].querySelectorAll=selector=>selector==='[data-tests-export]'?[]:Object.values(ids).flatMap(descendants).filter(x=>x.dataset.deleteTestId);
 const data=()=>({viewer:{username:admin?'observer':'alice',can_view_all:admin},summary:{duration_s:state.run?60:0,sessions:state.run?1:0},days:[],runs:state.run?[state.run]:[],source:{online:false},users:state.run?[{username:'deleted-user',active_state:'running',active_run_id:state.run.id,active_duration_s:60,total:{duration_s:60,sessions:1}}]:[],totals:{duration_s:state.run?60:0,sessions:state.run?1:0},active:null,revision:0,test_issues:{issues:[],pending_review:[],today:'2026-11-08'}});
 const response=(status,body)=>({status,ok:status>=200&&status<300,json:async()=>body});
 const fetch=async(url,opt={})=>{
  state.requests.push({url,opt});
  if(url==='/api/tests?days=30')return response(200,data());
  if(url.startsWith('/api/admin/tests/')){
   assert.equal(opt.method,'DELETE');assert.equal(opt.body,'{}');assert.equal(opt.credentials,'same-origin');assert.equal(opt.headers['Content-Type'],'application/json');
   if(state.mode==='throw')throw Error('network');
   if(state.mode==='expired')return response(401,{});
   if(state.mode==='missing')return response(404,{});
   if(state.mode==='conflict')return response(409,{});
   if(state.mode==='mismatch')return response(200,{deleted:true,id:'another-run'});
   const complete=()=>{const id=state.run.id;state.run=null;return response(200,{deleted:true,id});};
   if(state.mode==='pending')return new Promise(resolve=>state.release=()=>resolve(complete()));
   return complete();
  }
  throw Error('unexpected request');
 };
 const document={hidden:false,getElementById:id=>ids[id],createElement:tag=>new Element(tag),createElementNS:(ns,tag)=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),addEventListener(){}};
 const window={RKObserverDashboard:{getRoute:()=> 'tests'},addEventListener(){},confirm:text=>{state.prompts.push(text);return state.confirm;},location:{replace:path=>state.redirects.push(path)},CloudObserver:{ready:Promise.resolve(true),unauthorized:()=>state.expired=true}};
 vm.runInNewContext(source,{window,document,fetch,AbortController,Intl,Date,Number,JSON,Object,Array,String,Math,Promise,console,setTimeout:()=>1,clearTimeout(){}});
 await drain();state.ids=ids;state.buttons=()=>ids['page-tests'].querySelectorAll('[data-delete-test-id]');state.click=()=>state.buttons()[0].events.click();return state;
}
(async()=>{
 const viewer=await setup(false);assert.equal(viewer.buttons().length,0);
 const s=await setup();assert.equal(s.buttons().length,2,'summary and session delete entry');
 assert(s.ids['tests-runs-list'].textContent.includes('<b>遗留测试</b>'),'user-provided text remains text');
 s.confirm=false;s.click();await drain();assert.equal(s.requests.filter(x=>x.opt.method==='DELETE').length,0);assert(s.prompts[0].includes('计时将停止'));
 s.confirm=true;s.mode='pending';s.click();await drain();assert(s.buttons().every(x=>x.disabled));assert(s.ids['tests-start'].disabled);
 s.click();await drain();assert.equal(s.requests.filter(x=>x.opt.method==='DELETE').length,1);
 s.release();await drain();assert.equal(s.buttons().length,0);assert.equal(s.ids['tests-all-duration'].textContent,'00:00:00');assert.equal(s.ids['tests-today-sessions'].textContent,'0');assert.equal(s.ids['tests-message'].textContent,'测试已删除');
 for(const mode of ['throw','missing','conflict','mismatch']){
  const q=await setup();q.mode=mode;q.click();await drain();assert.equal(q.buttons().length,2);assert(q.buttons().every(x=>!x.disabled));assert(q.ids['tests-message'].textContent.length>0);
 }
 const expired=await setup();expired.mode='expired';expired.click();await drain();assert(expired.expired);assert(expired.buttons().every(x=>x.disabled));
 console.log('admin test deletion interaction passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    subprocess.run([node,'-e',script,str(ROOT/'cloud/frontend/daily-tests.js'),str(ROOT/'cloud/frontend/index.html')],check=True,capture_output=True,text=True)
