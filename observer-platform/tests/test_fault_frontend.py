"""Fault page UI contract and isolated interaction tests; no production connection."""
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'cloud/frontend'


def test_fault_page_has_local_assets_and_persistent_download_contract():
    html = (FRONTEND / 'index.html').read_text()
    assert 'data-page="logs"' in html
    assert 'data-page-name="logs"' in html
    assert '/fault-logs.js' in html and '/fault-logs.css' in html
    script = (FRONTEND / 'fault-logs.js').read_text()
    assert '/api/fault-logs/export?' in script
    assert 'textContent' in script and '.innerHTML' not in script
    assert 'Asia/Shanghai' in script and '+08:00' in script
    assert 'setTimeout(refresh, 5000)' in script
    assert 'observer-page-change' in script and 'visibilitychange' in script
    assert 'cloud-auth-lost' in script
    assert 'params.set("grouped", "1")' in script
    assert '/api/fault-logs/summary?grouped=1' in script
    assert '/api/fault-logs/groups/' in script and '/timestamps?' in script
    assert '历史汇总' in script and '来源合计' in script
    assert 'fault-categories' in html and '全部保留记录' in html
    assert 'filter.category' in script and 'item.meaning' in script and 'item.device_label' in script
    assert 'cloud-source' not in script  # A source outage is not a log service failure.


def test_fault_level_filter_only_offers_retained_severities():
    class LevelOptions(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_level = False
            self.options = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'select':
                self.in_level = values.get('id') == 'fault-level'
            if tag == 'option' and self.in_level:
                self.options.append(values.get('value'))

        def handle_endtag(self, tag):
            if tag == 'select':
                self.in_level = False

    parser = LevelOptions()
    parser.feed((FRONTEND / 'index.html').read_text())
    assert parser.options == ['', 'ERROR', 'FATAL', 'WARN']


def test_fault_page_filters_downloads_and_request_lifecycle():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for isolated fault page interaction')
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const listeners={},timers=new Map();let timerId=0;let activeRequests=0,maxActive=0;const requests=[],downloads=[];
function listen(target,name,callback){(target[name]??=[]).push(callback);}
function dispatch(target,name,event={}){for(const callback of target[name]||[])callback(event);}
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.events={};this.hidden=false;this.open=false;this.value='';this.disabled=false;this._text='';this.attributes={};const classes=new Set();this.classList={toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)};}
  set innerHTML(_){throw Error('unsafe HTML sink');}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  setAttribute(name,value){this.attributes[name]=value;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[];this._text='';for(const node of nodes)this.append(...(node.tagName==='#fragment'?node.children:[node]));}
  addEventListener(name,callback){listen(this.events,name,callback);}
  fire(name,event={}){dispatch(this.events,name,event);}
  querySelectorAll(selector){const result=[];function walk(node){for(const child of node.children){if(selector==='details[open]'&&child.tagName==='details'&&child.open)result.push(child);walk(child);}}walk(this);return result;}
  remove(){}
  click(){downloads.push({href:this.href,download:this.download});}
}
const ids={};for(const id of ['page-logs','fault-retention','fault-summary-total','fault-summary-error','fault-summary-warning','fault-summary-latest','fault-monitor-status','fault-log-list','fault-result-count','fault-page-label','fault-prev','fault-next','fault-updated','fault-filter-error','fault-range','fault-level','fault-query','fault-start','fault-end','fault-export-status','fault-filter-form','fault-reset','fault-custom-range','fault-refresh','fault-categories'])ids[id]=new Element();
ids['fault-filter-form'].reset=()=>{ids['fault-range'].value='24h';ids['fault-level'].value='';ids['fault-query'].value='';ids['fault-start'].value='';ids['fault-end'].value='';};
ids['fault-range'].value='24h';ids['fault-prev'].disabled=true;ids['fault-next'].disabled=true;
const exports=[new Element('button'),new Element('button')];exports[0].dataset.faultExport='csv';exports[1].dataset.faultExport='json';
ids['page-logs'].querySelectorAll=selector=>selector==='[data-fault-export]'?exports:[];
const documentListeners={};
const document={hidden:false,body:new Element('body'),getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('#fragment'),addEventListener:(name,cb)=>listen(documentListeners,name,cb)};
const window={RKObserverDashboard:{getRoute:()=> 'overview'},addEventListener:(name,cb)=>listen(listeners,name,cb),location:{replace:value=>{window.redirect=value;}}};
const mockFetch=(url,options)=>new Promise((resolve,reject)=>{
  activeRequests++;maxActive=Math.max(maxActive,activeRequests);let settled=false;
  const request={url,options,finish(value){if(settled)return;settled=true;activeRequests--;resolve(value);},fail(){if(settled)return;settled=true;activeRequests--;reject(Error('aborted'));}};
  options.signal.addEventListener('abort',()=>request.fail());requests.push(request);
});
const context={window,document,fetch:mockFetch,AbortController,URLSearchParams,Intl,Date,Number,JSON,Object,Array,String,Set,Promise,console,
 setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:id=>timers.delete(id),URL:{createObjectURL:()=> 'blob:test-log',revokeObjectURL:()=>{}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=value=>({status:200,ok:true,json:async()=>value});
function finishBatch(index,items,total=items.length){requests[index].finish(response({items,total,limit:50,offset:0}));requests[index+1].finish(response({total:67,error:3,warning:5,last_timestamp:1760000000,retention_days:30,max_records:20000,monitoring:{online:false},categories:[{id:'lidar_3d',label:'三维激光雷达',total:31,error:3,warning:28},{id:'imu',label:'惯性传感器（IMU）',total:5,error:2,warning:3},{id:'__proto__',label:'untrusted',total:99}]}));}
function poll(){const entry=[...timers].find(([,v])=>v.delay===5000);assert(entry,'poll scheduled');timers.delete(entry[0]);entry[1].callback();}
const event={id:'fault-1',timestamp:1760000000.123,last_timestamp:1760000001.123,received_at:1760000002.123,level:'ERROR',code:'LIDAR_TIME_ROLLBACK',component:'MID360',source_id:'ipc',message:'<img src=x onerror=alert(1)> 时间回退',details:{raw:'<script>unsafe</script>'},count:2,category:'lidar_3d',category_label:'三维激光雷达',device_label:'MID360 三维激光雷达',title:'雷达时间戳回退',meaning:'收到的雷达时间早于前一帧，数据顺序倒退。',impact:'可能造成定位不稳定。',suggestion:'核对驱动和雷达时间。'};
(async()=>{
 assert.equal(requests.length,0,'inactive page must not poll');
 assert.equal(window.ObserverFaultLogs.formatTime(0),'1970-01-01 08:00:00.000');
 assert.equal(window.ObserverFaultLogs.parseBeijing('1970-01-01T08:00:00'),0);
 assert.equal(window.ObserverFaultLogs.parseBeijing(''),null);
 assert(Number.isNaN(window.ObserverFaultLogs.parseBeijing('invalid')));
 assert(Number.isNaN(window.ObserverFaultLogs.parseBeijing('2026-02-31T10:00:00')));
 dispatch(listeners,'observer-page-change',{detail:{page:'logs'}});assert.equal(requests.length,2);assert(requests[0].url.includes('grouped=1'));assert(requests[1].url.endsWith('summary?grouped=1'));finishBatch(0,[event],67);await settle();
 assert(ids['fault-log-list'].textContent.includes(event.message),'raw text remains literal in disclosure');
 assert(ids['fault-log-list'].textContent.includes(event.title)&&ids['fault-log-list'].textContent.includes(event.meaning),'Chinese title and meaning displayed');
 assert.equal(ids['fault-log-list'].children[0].children[3].children[0].textContent,event.title);
 assert.equal(ids['fault-log-list'].children[0].children[3].children[1].textContent,event.meaning);
 assert.equal(ids['fault-categories'].children.length,12,'only known category buttons are rendered');
 assert(ids['fault-categories'].children.find(e=>e.dataset.faultCategory==='lidar_3d').textContent.includes('31'));
 assert.equal(ids['fault-summary-error'].textContent,'3');
 assert(ids['fault-monitor-status'].textContent.includes('采集端离线'));
 assert(!ids['fault-next'].disabled);
 const details=ids['fault-log-list'].children[0].children[3].children.find(node=>node.className==='fault-details');details.open=true;
 poll();assert.equal(requests.length,4);finishBatch(2,[{...event,level:'WARN',count:3}],67);await settle();
 assert(ids['fault-log-list'].textContent.includes('关键告警'),'retained warning is labelled a key alert');
 assert.equal(ids['fault-log-list'].querySelectorAll('details[open]').length,1,'expanded evidence survives refresh');
 poll();assert.equal(requests.length,6);ids['fault-query'].value='雷达';ids['fault-level'].value='WARN';ids['fault-filter-form'].fire('submit',{preventDefault(){}});await settle();
 assert(requests[4].options.signal.aborted&&requests[5].options.signal.aborted,'superseded filter requests cancelled');
 assert.equal(requests.length,8);assert(requests[6].url.includes('q=%E9%9B%B7%E8%BE%BE'));assert(requests[6].url.includes('level=WARN'));finishBatch(6,[event],67);await settle();
 const imu=ids['fault-categories'].children.find(node=>node.dataset.faultCategory==='imu');imu.fire('click');assert.equal(requests.length,10);
 assert(requests[8].url.includes('category=imu')&&requests[8].url.includes('offset=0')&&requests[8].url.includes('level=WARN'),'category combines with existing filters');
 assert.equal(ids['fault-categories'].children.find(node=>node.dataset.faultCategory==='imu').attributes['aria-pressed'],'true');
 finishBatch(8,[{...event,category:'imu',category_label:'惯性传感器（IMU）',device_label:'惯性传感器',title:'惯性数据时间断档'}],67);await settle();
 ids['fault-next'].fire('click');assert.equal(requests.length,12);assert(requests[10].url.includes('offset=50')&&requests[10].url.includes('category=imu'));finishBatch(10,[event],67);await settle();
 assert(ids['fault-page-label'].textContent.includes('第 2 / 2 页'));
 exports[0].fire('click');assert.equal(requests.length,13);assert(requests[12].url.startsWith('/api/fault-logs/export?'));assert(!requests[12].url.includes('offset='));assert(!requests[12].url.includes('limit='));assert(!requests[12].url.includes('grouped='),'export keeps raw records');assert(requests[12].url.includes('format=csv'));assert(requests[12].url.includes('level=WARN')&&requests[12].url.includes('category=imu'),'list and export share severity and sensor category');
 requests[12].finish({status:200,ok:true,blob:async()=>({size:100})});await settle();assert.equal(downloads.length,1);assert(downloads[0].download.endsWith('.csv'));assert(downloads[0].download.includes('惯性传感器'));
 poll();dispatch(listeners,'observer-page-change',{detail:{page:'overview'}});await settle();assert(requests.at(-1).options.signal.aborted);assert(![...timers.values()].some(timer=>timer.delay===5000),'leaving page cancels polling');
 const previous=requests.length;document.hidden=true;dispatch(listeners,'observer-page-change',{detail:{page:'logs'}});assert.equal(requests.length,previous,'hidden page must not poll');document.hidden=false;dispatch(documentListeners,'visibilitychange');assert.equal(requests.length,previous+2);finishBatch(previous,[],0);await settle();
 assert(ids['fault-log-list'].textContent.includes('暂无关键故障记录'));assert(ids['fault-next'].disabled);
 ids['fault-reset'].fire('click');const resetIndex=requests.length-2;assert(!requests[resetIndex].url.includes('category='));assert(requests[resetIndex].url.includes('offset=0'));
 finishBatch(resetIndex,[],0);await settle();assert.equal(ids['fault-categories'].children[0].attributes['aria-pressed'],'true');

 poll();const failed=requests.length-2;requests[failed].fail();await settle();assert(requests[failed+1].options.signal.aborted,'failed batch cancels sibling request');assert.equal(activeRequests,0);assert(ids['fault-updated'].textContent.includes('连接中断'));
 dispatch(listeners,'cloud-auth-lost');assert(![...timers.values()].some(timer=>timer.delay===5000));
 assert.equal(maxActive,2,'only one list/summary batch is in flight');
 console.log(JSON.stringify({passed:true,requests:requests.length,maxActive,downloads:downloads.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, '-e', script, str(FRONTEND / 'fault-logs.js')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['passed'] is True


def test_grouped_fault_history_is_lazy_paginated_and_scoped_without_losing_focus():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for isolated grouped fault interaction')
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const listeners={},docEvents={},requests=[],timers=new Map();let timerId=0;
function listen(target,name,fn){(target[name]??=[]).push(fn);}
function fire(target,name,event={}){for(const fn of target[name]||[])fn(event);}
let document;
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.events={};this.attributes={};this._text='';this.open=false;this.hidden=false;this.value='';this.disabled=false;this.classList={toggle(){},remove(){}};}
 set innerHTML(_){throw Error('unsafe HTML sink');}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[];this._text='';for(const node of nodes)this.append(...(node.tagName==='#fragment'?node.children:[node]));}
 setAttribute(name,value){this.attributes[name]=value;}
 addEventListener(name,fn){listen(this.events,name,fn);}
 fire(name,event={}){fire(this.events,name,event);}
 querySelectorAll(){return [];}
 contains(child){return this===child||this.children.some(node=>node.contains(child));}
 focus(){document.activeElement=this;}
}
const ids={};for(const id of ['page-logs','fault-retention','fault-summary-total','fault-summary-error','fault-summary-warning','fault-summary-latest','fault-monitor-status','fault-log-list','fault-result-count','fault-page-label','fault-prev','fault-next','fault-updated','fault-filter-error','fault-range','fault-level','fault-query','fault-start','fault-end','fault-export-status','fault-filter-form','fault-reset','fault-custom-range','fault-refresh','fault-categories'])ids[id]=new Element();
ids['fault-range'].value='24h';
document={hidden:false,activeElement:null,body:new Element('body'),getElementById:id=>ids[id],createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('#fragment'),addEventListener:(name,fn)=>listen(docEvents,name,fn)};
const window={RKObserverDashboard:{getRoute:()=> 'logs'},addEventListener:(name,fn)=>listen(listeners,name,fn),location:{replace(){}},RobotFleet:{fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))}};
const context={window,document,fetch:()=>{throw Error('unscoped network request');},AbortController,URLSearchParams,Intl,Date,Number,JSON,Object,Array,String,Set,Map,Promise,console,setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:id=>timers.delete(id)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=value=>({status:200,ok:true,json:async()=>value});
const done=(index,value)=>requests[index].resolve(response(value));
const group='a'.repeat(64),second='b'.repeat(64),stamp=1760000000;
const sample={id:group,group_id:group,timestamp:stamp,last_timestamp:stamp+51,received_at:stamp+52,count:55,level:'ERROR',code:'CAMERA_ERROR',component:'camera',category:'camera',device_label:'前相机',title:'相机异常',meaning:'设备报告异常',message:'<img src=x>',details:{raw:'<script>safe text</script>',legacy_count_is_lower_bound:true}};
const summary={total:1,error:1,warning:0,last_timestamp:stamp+51,categories:[]};
function finishList(index,items=[sample]){done(index,{items,total:items.length});done(index+1,summary);}
function poll(){const entry=[...timers].find(([,value])=>value.delay===5000);assert(entry,'poll scheduled');timers.delete(entry[0]);entry[1].callback();}
function history(row=ids['fault-log-list'].children[0]){return row.children[3].children.find(node=>node.className==='fault-history');}
function more(panel){return panel.children.find(node=>node.className==='fault-history-more');}
(async()=>{
 assert.equal(requests.length,2,'only list and summary fetched initially');
 finishList(0);await settle();
 assert(ids['fault-result-count'].textContent.includes('1 类故障'));
 const row=ids['fault-log-list'].children[0],panel=history(row),toggle=panel.children[0];
 assert.equal(requests.length,2,'timestamp history remains lazy');
 assert.equal(toggle.textContent,'时间记录（至少 55 次）');assert.equal(row.children[4].textContent,'≥55');assert(row.children[4].attributes['aria-label'].includes('至少'));
 panel.open=true;panel.fire('toggle');assert.equal(requests.length,3);
 assert(requests[2].url.startsWith(`/api/fault-logs/groups/${group}/timestamps?`));
 assert(requests[2].url.includes('limit=50')&&requests[2].url.includes('offset=0')&&requests[2].url.includes('start='));
 const events=Array.from({length:50},(_,i)=>({id:'event-'+i,timestamp:stamp+i,last_timestamp:stamp+i,count:1,kind:'event'}));
 done(2,{items:events,total:52,occurrence_count:55});await settle();
 const list=panel.children.find(node=>node.className==='fault-timestamps');assert.equal(list.children.length,50);assert(!more(panel).hidden);
 more(panel).fire('click');assert(requests[3].url.includes('offset=50'));
 done(3,{items:[{id:'event-50',timestamp:stamp+50,last_timestamp:stamp+50,count:2,kind:'reported_summary'},{id:'history-old',timestamp:stamp-100,last_timestamp:stamp-20,count:3,kind:'historical_summary',details:{legacy_count_is_lower_bound:true}}],total:52,occurrence_count:55});await settle();
 assert.equal(list.children.length,52);assert(more(panel).hidden);assert(panel.textContent.includes('来源合计 2 次'));assert(panel.textContent.includes('历史汇总 · 至少 3 次'));assert(panel.textContent.includes('至 '),'legacy summaries remain ranges, without invented timestamps');
 const raw=row.children[3].children.find(node=>node.className==='fault-details');raw.open=true;toggle.focus();
 poll();finishList(4);await settle();
 assert.strictEqual(ids['fault-log-list'].children[0],row,'same group keeps the row DOM');assert.strictEqual(history(),panel);assert(panel.open&&raw.open);assert.strictEqual(document.activeElement,toggle,'poll preserves keyboard focus');assert.equal(requests.length,6,'unchanged poll reuses loaded timestamp history');
 poll();finishList(6,[{...sample,count:56,last_timestamp:stamp+52}]);await settle();assert.equal(requests.length,9,'new occurrence refreshes expanded timestamps');assert(requests[8].url.includes('offset=0'));
 done(8,{items:[{id:'event-new',timestamp:stamp+52,count:1,kind:'event'},...events.slice(0,49)],total:53});await settle();assert.strictEqual(document.activeElement,toggle);assert.equal(toggle.textContent,'时间记录（至少 56 次）');
 more(panel).fire('click');assert.equal(requests.length,10);assert(requests[9].url.includes('offset=50'));
 requests[9].reject(Error('network'));await settle();assert.equal(more(panel).textContent,'重试');assert(!more(panel).hidden);assert.equal(list.children.length,50,'failure retains previous timestamps');
 more(panel).fire('click');assert.equal(requests.length,11);
 ids['fault-query'].value='新故障';ids['fault-filter-form'].fire('submit',{preventDefault(){}});assert(requests[10].options.signal.aborted,'changing filters cancels timestamp requests');assert.equal(requests.length,13);
 done(10,{items:[{id:'stale',timestamp:stamp,count:1,kind:'event'}],total:52});finishList(11,[{...sample,group_id:second,id:second,count:1}]);await settle();
 const next=history();assert(!next.open,'new filter starts with a closed disclosure');assert(!ids['fault-log-list'].textContent.includes('stale'));
 next.open=true;next.fire('toggle');assert.equal(requests.length,14);assert(requests[13].url.includes('q=%E6%96%B0%E6%95%85%E9%9A%9C'));
 fire(listeners,'observer-page-change',{detail:{page:'overview'}});assert(requests[13].options.signal.aborted,'leaving page cancels history');done(13,{items:[{id:'late',timestamp:stamp,count:1,kind:'event'}],total:1});await settle();assert.equal(next.children.find(node=>node.className==='fault-timestamps').children.length,0,'late result after leaving page is ignored');
 fire(listeners,'observer-page-change',{detail:{page:'logs'}});finishList(14,[{...sample,group_id:second,id:second,count:1}]);await settle();assert.equal(requests.length,17,'open disclosure resumes its cancelled request after returning');
 document.hidden=true;fire(docEvents,'visibilitychange');assert(requests[16].options.signal.aborted,'hidden page cancels history');done(16,{items:[],total:0});await settle();
 document.hidden=false;fire(docEvents,'visibilitychange');finishList(17,[{...sample,group_id:second,id:second,count:1}]);await settle();assert.equal(requests.length,20);
 poll();finishList(20,[{...sample,group_id:second,id:second,count:2,last_timestamp:stamp+53}]);await settle();assert.equal(requests.length,22);assert(!requests[19].options.signal.aborted,'poll does not repeatedly cancel a slow history response');
 fire(listeners,'cloud-auth-lost');assert(requests[19].options.signal.aborted,'authentication loss cancels history');done(19,{items:[],total:0});await settle();assert(![...timers.values()].some(timer=>timer.delay===5000));
 console.log(JSON.stringify({passed:true,requests:requests.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, '-e', script, str(FRONTEND / 'fault-logs.js')], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['passed'] is True
