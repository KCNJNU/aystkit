"""Independent real Firefox acceptance for parameterized plastic mounting volumes.

Synthetic loopback accounts only; real WebGL, pointer actions and dedicated Worker.
No production, physical device or camera access.
"""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from aiohttp import web
import browser_coverage_recommend as rec

refine,shared,qa,polish=rec.refine,rec.shared,rec.qa,rec.polish
ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=Path(os.environ.get('PLASTIC_SUPPORT_ARTIFACTS','/tmp/plastic-supports-20261024/artifacts'))
SERVER,DRIVER='http://127.0.0.1:19248','http://127.0.0.1:19249'
STORAGE,BASELINE=rec.STORAGE,rec.BASELINE
CAMERAS=rec.CAMERAS
SUPPORTS=('front','camera1','camera3','camera4','mid360','bluesea','stl19p')
for module in (rec,refine,rec.fixed,shared,shared.qa,shared.drag,shared.frame,shared.polish,shared.photo):
    module.ROOT,module.ARTIFACTS,module.SERVER,module.DRIVER=ROOT,ARTIFACTS,SERVER,DRIVER

class Browser(shared.Browser):
    def sensor(self,identifier):
        self.clickid('sensor-'+identifier)
        self.wait("document.querySelector('[data-testid=sensor-"+identifier+"-editor]')")
        time.sleep(.15)
        assert self.evaluate("return document.querySelectorAll('.support-panel').length")==1, 'Duplicate stale support panels after sensor switch'
        assert self.evaluate("return document.querySelectorAll('.sensor-card.is-open').length")==1
    def support(self,identifier):
        self.sensor('camera2' if identifier=='front' else identifier)
        self.clickid('select-support')
        self.wait("document.querySelector('.three-host').dataset.selectedSupportId==="+json.dumps(identifier))
        self.evaluate("document.querySelector('.support-structure').open=true")
        time.sleep(.2)

def serve():
    sys.path.insert(0,str(ROOT))
    from cloud.backend.app import create_app
    from cloud.backend.security import Config,hash_password
    with tempfile.TemporaryDirectory(prefix='plastic-support-data-') as directory:
        cfg=Config(robot_id='plastic-support-fixture',device_credentials=[{'token':secrets.token_urlsafe(36),'robot_id':'plastic-support-fixture','source_id':'fixture','role':'primary'}],viewer_username='coverage-fixture',viewer_password_hash=hash_password(os.environ['COVERAGE_QA_PASSWORD']),session_secret=secrets.token_urlsafe(48),public_origin=SERVER,data_dir=directory,allow_insecure_local=True,frontend_dir=str(ROOT/'cloud/frontend'))
        web.run_app(create_app(cfg),host='127.0.0.1',port=19248,access_log=None)

def preload_source():
    source=rec.preload_source().replace('window.__coverageQA=','window.__supportQA={boxBuffers:[]};window.__coverageQA=')
    warning="const originalWarn=console.warn.bind(console);console.warn=(...args)=>{if(/same key|duplicate.*key/i.test(args.map(String).join(' ')))__coverageQA.errors.push(args.map(String).join(' '));originalWarn(...args)};"
    source=source.replace('const originalError=console.error.bind(console);',warning+'const originalError=console.error.bind(console);')
    tracking=r'''
   for(const operation of ['bufferData','bufferSubData']){
     const original=context[operation];context[operation]=function(...args){
       const a=args[operation==='bufferData'?1:2];
       if(a instanceof Float32Array&&a.length===72&&[...a].every(Number.isFinite)){
         const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];
         for(let i=0;i<a.length;i++){const k=i%3;lo[k]=Math.min(lo[k],a[i]);hi[k]=Math.max(hi[k],a[i])}
         const size=lo.map((v,i)=>hi[i]-v),key=size.map(n=>n.toFixed(6)).join(',');
         if(!__supportQA.boxBuffers.some(b=>b.key===key))__supportQA.boxBuffers.push({key,min:lo,max:hi,size});
       }return original.apply(this,args);
     }
   }
'''
    return source.replace('context.__coverageTracked=true;','context.__coverageTracked=true;'+tracking)

def prepare_reference():
    path=ARTIFACTS/'support-reference.ts'
    path.write_text("import fs from 'node:fs';\nimport {computeCoverage} from '"+str(ROOT/'coverage-config/src/geometry.ts')+"';\nimport {sanitizeConfig} from '"+str(ROOT/'coverage-config/src/model.ts')+"';\nimport {baselineUnderCurrentConditions} from '"+str(ROOT/'coverage-config/src/comparison.ts')+"';\nconst input=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));const config=sanitizeConfig(input.config||input);const camera=computeCoverage({...config,sensors:config.sensors.map(s=>({...s,enabled:s.enabled&&(s.kind==='camera'||s.kind==='depth')}))});const overall=computeCoverage(config);const reduce=s=>Object.fromEntries(Object.entries(s).filter(([k])=>k!=='cells'));console.log(JSON.stringify({config,overall:reduce(overall),camera:reduce(camera),baseline:input.baseline?reduce(computeCoverage(baselineUnderCurrentConditions(sanitizeConfig(input.baseline),config))):null}));\n")
    subprocess.run(['node_modules/esbuild/bin/esbuild',str(path),'--bundle','--platform=node','--format=esm','--outfile='+str(ARTIFACTS/'support-reference.mjs')],cwd=ROOT/'coverage-config',check=True,capture_output=True)

def reference(config,baseline=None):
    p=ARTIFACTS/'support-input.json';p.write_text(json.dumps({'config':config,'baseline':baseline}))
    r=subprocess.run(['node',str(ARTIFACTS/'support-reference.mjs'),str(p)],cwd=ROOT/'coverage-config',check=True,capture_output=True,text=True)
    return json.loads(r.stdout)

def near(a,b,tolerance=1e-7):
    assert math.isfinite(a) and abs(a-b)<=tolerance,(a,b,tolerance)

def sensors(config):return {s['id']:s for s in config['sensors']}
def support_id(sensor):return 'front' if sensor in ('camera2','p100r') else sensor

def box_geometry(config,key):
    c=config['chassis'];layout=config['supports'][key]
    hx,hy=c['frameLength']/2,c['frameWidth']/2;bottom=c['height'];top=bottom+c['frameHeight']
    axis,level,side=layout['rail'].split('-')
    if axis=='x':
        z=top if level=='top' else bottom if level=='bottom' else bottom+c['frameMidHeights'][side]
        a={'x':-hx,'y':hy if side=='left' else -hy,'z':z};b={**a,'x':hx}
    elif axis=='y':
        z=top if level=='top' else bottom if level=='bottom' else bottom+c['frameMidHeights'][side]
        a={'x':hx if side=='front' else -hx,'y':-hy,'z':z};b={**a,'y':hy}
    else:
        a={'x':hx if level=='front' else -hx,'y':hy if side=='left' else -hy,'z':bottom};b={**a,'z':top}
    if key=='front':a['y']+=layout['sizeY']/2;b['y']-=layout['sizeY']/2
    anchor={k:a[k]+(b[k]-a[k])*layout['t'] for k in 'xyz'}
    center={k:anchor[k]+layout['offset'+k.upper()] for k in 'xyz'}
    size={k:layout['size'+k.upper()] for k in 'xyz'}
    return {'id':key,'anchor':anchor,'center':center,'size':size,'min':{k:center[k]-size[k]/2 for k in 'xyz'},'max':{k:center[k]+size[k]/2 for k in 'xyz'},'rail':layout['rail'],'style':layout['style'],'thickness':layout['thickness'],'axis':axis}

def assert_config(config):
    assert config['layoutRevision']==6 and len(config['sensors'])==8
    assert set(config['supports'])==set(SUPPORTS)
    assert config['supports']['front']['rail']=='y-mid-front'
    for s in config['sensors']:
        b=box_geometry(config,support_id(s['id']))
        assert set(s['inSupport'])==set('xyz')
        for axis in 'xyz':
            u=s['inSupport'][axis];assert math.isfinite(u) and 0<=u<=1
            near(s[axis],b['center'][axis]+(u-.5)*b['size'][axis])
            assert b['min'][axis]-1e-9<=s[axis]<=b['max'][axis]+1e-9

def state(browser):return shared.board_state(browser)
def baseline(browser):return rec.baseline(browser)
def imported(browser,config):
    browser.evaluate("document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.import_config({'version':1,'config':config})
    browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')")
    time.sleep(.32);actual=browser.config();assert_config(actual)
    assert actual==reference(config)['config']
    return actual

def assert_scene(browser,config):
    data=state(browser)
    assert data['supportBoxCount']==7 and data['plateMountCount']==7 and data['sensorPointCount']==8
    assert data['aluminumMemberCount']==16 and data['frameRailCount']==18 and data['sensorDisplay']=='points'
    assert data['sensorMovement']=='cuboid-xyz' and data['renderMode']=='on-demand'
    assert data['sensorSupportIds']=={s['id']:support_id(s['id']) for s in config['sensors']}
    geometry={b['id']:b for b in data['supportBoxGeometry']};assert set(geometry)==set(SUPPORTS)
    for key in SUPPORTS:
        expected=box_geometry(config,key);actual=geometry[key]
        for field in ('anchor','center','min','max','size'):
            for axis in 'xyz':near(actual[field][axis],expected[field][axis])
        for field in ('rail','style','thickness'):assert actual[field]==expected[field]
    return data

def assert_metrics(browser,config,baseline_config=None):
    ref=reference(config,baseline_config);s=ref['overall'];current=browser.metrics()
    expected=[s['percent'],s['blindArea'],s['overlap']/s['total']*100 if s['total'] else 0,s['area']]
    for a,b,t in zip(current,expected,(.051,.0051,.051,.0051)):near(a,b,t)
    data=assert_scene(browser,config)
    assert data['surfaceClassCounts']=={'blind':s['blind'],'covered':s['covered']-s['overlap'],'overlap':s['overlap']}
    assert data['surfaceCellCount']==s['total'];near(data['surfaceHeight'],config['analysis']['height'])
    if baseline_config:
        b=ref['baseline'];metrics={'coverage':b['percent'],'blind':b['blindArea'],'overlap':b['overlap']/b['total']*100 if b['total'] else 0}
        for key,value in metrics.items():
            shown=browser.evaluate("return document.querySelector('[data-testid="+key+"-baseline]').textContent")
            import re
            number=float(re.search(r'[-+]?\d+(?:\.\d+)?',shown).group())
            near(number,value,.051 if key!='blind' else .0051)
    return {'percent':s['percent'],'blindArea':s['blindArea'],'total':s['total'],'covered':s['covered'],'overlap':s['overlap']}

def assert_buffer_dimensions(browser,size):
    buffers=browser.evaluate('return __supportQA.boxBuffers')
    assert any(all(abs(a-b)<1e-6 for a,b in zip(v['size'],size)) for v in buffers),{'expected':size,'actual':buffers}

def changed_only(before,after,ids):
    assert before['supports']==after['supports'] and before['chassis']==after['chassis'] and before['analysis']==after['analysis']
    for identifier,old in sensors(before).items():
        new=sensors(after)[identifier]
        if identifier not in ids:assert old==new,identifier
        else:
            for key in set(old)-{'x','y','z','inSupport'}:assert old[key]==new[key],(identifier,key)
    assert_config(after)

def verify_numeric_and_shared(browser,initial,result):
    config=imported(browser,initial);browser.sensor('camera3')
    inputs=browser.input_state('camera3');assert all(not v['disabled'] for v in inputs.values())
    steps=[]
    for axis,value in zip('xyz',(.14,.83,.23)):
        before=browser.config();browser.range('sensor-camera3-local-'+axis,value);after=browser.config()
        changed_only(before,after,{'camera3'});near(sensors(after)['camera3']['inSupport'][axis],value)
        for other in set('xyz')-{axis}:near(sensors(before)['camera3'][other],sensors(after)['camera3'][other])
        steps.append({axis:sensors(after)['camera3'][axis]})
    for axis in 'xyz':
        b=box_geometry(browser.config(),'camera3')
        browser.set_input('sensor-camera3-'+axis,b['max'][axis]+10)
        near(sensors(browser.config())['camera3'][axis],b['max'][axis])
        browser.set_input('sensor-camera3-'+axis,b['min'][axis]-10)
        near(sensors(browser.config())['camera3'][axis],b['min'][axis])
    browser.sensor('camera2');before=browser.config()
    for axis,value in zip('xyz',(.79,.18,.73)):browser.range('sensor-camera2-local-'+axis,value)
    after=browser.config();changed_only(before,after,{'camera2'})
    assert sensors(after)['p100r']==sensors(before)['p100r']
    browser.support('front');before=browser.config();locals_before={k:sensors(before)[k]['inSupport'] for k in ('camera2','p100r')}
    browser.range('support-position',.77);after=browser.config();assert_config(after)
    assert before['supports']['front']['t']!=after['supports']['front']['t']
    for k in ('camera2','p100r'):
        assert sensors(after)[k]['inSupport']==locals_before[k]
        near(sensors(after)[k]['x'],sensors(before)[k]['x']);near(sensors(after)[k]['z'],sensors(before)[k]['z'])
        assert sensors(after)[k]['y']!=sensors(before)[k]['y']
    assert browser.evaluate("return document.querySelector('[data-testid=support-rail]').disabled")
    assert set(state(browser)['gizmoProjections'])=={'Y'}
    result['numericXYZClipsToFacesAndSharedPointsAreIndependent']={'independentAxes':steps,'frontBoxMovesBothButPointMovesOnlyOne':True}
    return after

def verify_support_parameters(browser,initial,result):
    imported(browser,initial);browser.support('camera3')
    browser.evaluate('__supportQA.boxBuffers=[]')
    for axis,value in zip('xyz',(24,18,22)):browser.set_input('support-size-'+axis,value)
    for axis,value in zip('xyz',(-7,11,16)):browser.set_input('support-offset-'+axis,value)
    browser.set_input('support-thickness',13)
    browser.evaluate("const s=document.querySelector('[data-testid=support-style]');s.value='pedestal';s.dispatchEvent(new Event('change',{bubbles:true}))")
    time.sleep(.25);config=browser.config();box=config['supports']['camera3']
    for axis,size,offset in zip('xyz',(.24,.18,.22),(-.07,.11,.16)):
        near(box['size'+axis.upper()],size);near(box['offset'+axis.upper()],offset)
    near(box['thickness'],.013);assert box['style']=='pedestal'
    assert_scene(browser,config);assert_buffer_dimensions(browser,[.24,.18,.22])
    # The shared physical sheet has actual BoxGeometry(thickness, width, height).
    browser.support('front');browser.evaluate('__supportQA.boxBuffers=[]')
    browser.set_input('support-thickness',21);time.sleep(.2)
    config=browser.config();front=config['supports']['front']
    assert_buffer_dimensions(browser,[front['thickness'],front['sizeY'],front['sizeZ']])
    browser.clickid('view-mounting');browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0;document.querySelector('button[aria-label=关闭提示]')?.click()")
    browser.screenshot('supports-edited-shared-box.png')
    result['editedSupportGeometryMatchesActualUploadedWebGLBoxBuffers']={'camera3':box,'frontSheetThickness':front['thickness'],'bufferUploadEvidence':browser.evaluate('return __supportQA.boxBuffers')}
    assert_metrics(browser,config)
    return config

def drag_axis(browser,axis,distance=160,release=True):
    data=state(browser);handle=data['gizmoProjections'][axis.upper()]
    origin=[data['selectedScreenX'],data['selectedScreenY']]
    dx,dy=handle['x']-origin[0],handle['y']-origin[1];length=math.hypot(dx,dy);assert length>2,(axis,data)
    start=[handle['x'],handle['y']];end=[start[0]+dx/length*distance,start[1]+dy/length*distance]
    browser.pointer(start,end,release=release,duration=450)
    time.sleep(.25)

def verify_pointer_drags(browser,initial,result):
    fixture=copy.deepcopy(initial);fixture['supports']['camera3'].update(sizeX=.6,sizeY=.6,sizeZ=.6,offsetY=.4,offsetZ=.35)
    fixture['sensors']= [{**s,'inSupport':{'x':.5,'y':.5,'z':.5}} if s['id']=='camera3' else s for s in fixture['sensors']]
    outcomes={}
    for axis in 'xyz':
        before=imported(browser,fixture);browser.sensor('camera3');browser.clickid('view-mounting');time.sleep(.2)
        assert set(state(browser)['gizmoProjections'])==set('XYZ')
        drag_axis(browser,axis,210);after=browser.config();changed_only(before,after,{'camera3'})
        old,new=sensors(before)['camera3'],sensors(after)['camera3'];assert new[axis]>old[axis]+.005,(axis,old,new,state(browser))
        b=box_geometry(after,'camera3');near(new[axis],b['max'][axis],.011)
        for other in set('xyz')-{axis}:near(new[other],old[other])
        outcomes[axis]={'before':old[axis],'after':new[axis],'max':b['max'][axis]}
    before=imported(browser,fixture);browser.sensor('camera3');browser.clickid('view-mounting');time.sleep(.2)
    point=next(p for p in state(browser)['sensorProjections'] if p['id']=='camera3')
    browser.pointer([point['x'],point['y']],[point['x']+100,point['y']-80],release=False,duration=400)
    assert state(browser)['dragActive'] is True
    changed=browser.config();assert sensors(changed)['camera3']!=sensors(before)['camera3'];assert_config(changed)
    browser.call('/actions',{'actions':[{'type':'key','id':'escape-key','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release();time.sleep(.3);assert browser.config()==before
    # Import another point pose with exactly the same support geometry during
    # an active real pointer drag. Queued old movement must not replace it.
    before=imported(browser,fixture);browser.sensor('camera3');browser.clickid('view-mounting');time.sleep(.2)
    point=next(p for p in state(browser)['sensorProjections'] if p['id']=='camera3')
    browser.pointer([point['x'],point['y']],[point['x']+45,point['y']-35],release=False,duration=250)
    assert state(browser)['dragActive'] is True
    replacement=copy.deepcopy(before)
    for sensor in replacement['sensors']:
        if sensor['id']=='camera3':sensor['inSupport']={'x':.12,'y':.78,'z':.32}
    expected=reference(replacement)['config']
    browser.import_config({'version':1,'config':replacement})
    time.sleep(.4)
    assert browser.config()==expected, 'An active drag overwrote imported box-local coordinates'
    assert state(browser)['dragActive'] is False
    browser.release();time.sleep(.3)
    assert browser.config()==expected, 'Pointer release overwrote the imported pose'
    disabled=copy.deepcopy(fixture)
    for sensor in disabled['sensors']:
        if sensor['id']=='camera3':sensor['enabled']=False
    before=imported(browser,disabled);browser.sensor('camera3');browser.clickid('view-mounting');time.sleep(.2)
    drag_axis(browser,'x',105);after=browser.config();changed_only(before,after,{'camera3'})
    assert not sensors(after)['camera3']['enabled'] and sensors(after)['camera3']['x']>sensors(before)['camera3']['x']
    # The shared support's single Y handle translates both point positions.
    before=imported(browser,initial);browser.support('front');time.sleep(.2)
    assert set(state(browser)['gizmoProjections'])=={'Y'}
    drag_axis(browser,'y',85);after=browser.config();assert_config(after)
    for identifier in ('camera2','p100r'):
        assert sensors(after)[identifier]['inSupport']==sensors(before)[identifier]['inSupport']
        assert sensors(after)[identifier]['y']>sensors(before)[identifier]['y']
        near(sensors(after)[identifier]['x'],sensors(before)[identifier]['x']);near(sensors(after)[identifier]['z'],sensors(before)[identifier]['z'])
    before=imported(browser,initial);browser.support('front');time.sleep(.2)
    drag_axis(browser,'y',55,release=False)
    assert state(browser)['dragActive'] is True and browser.config()['supports']['front']['t']!=before['supports']['front']['t']
    browser.call('/actions',{'actions':[{'type':'key','id':'support-escape-key','actions':[{'type':'keyDown','value':'\ue00c'},{'type':'keyUp','value':'\ue00c'}]}]})
    browser.release();time.sleep(.3)
    assert browser.config()==before, 'Support cancellation did not restore the exact saved anchor fraction'
    # Pick the actual transparent cuboid body away from points and gizmos.
    body_attempts=[];body_success=False
    for candidate_index in range(8):
        before=imported(browser,initial);browser.support('front');time.sleep(.15)
        data=state(browser);projection=next(p for p in data['supportBoxProjections'] if p['id']=='front')
        rail=data['railProjections'];dx=rail['end']['x']-rail['start']['x'];dy=rail['end']['y']-rail['start']['y'];length=math.hypot(dx,dy)
        avoid=data['sensorProjections']+list(data['gizmoProjections'].values())
        candidates=sorted(projection['pickPoints'],key=lambda p:min(math.hypot(p['x']-a['x'],p['y']-a['y']) for a in avoid),reverse=True)
        start=candidates[candidate_index];end=[start['x']+dx/length*70,start['y']+dy/length*70]
        browser.pointer([start['x'],start['y']],end,duration=400);time.sleep(.2);after=browser.config()
        body_attempts.append({'index':candidate_index,'selected':state(browser)['selectedSupportId'],'t':after['supports']['front']['t']})
        if abs(after['supports']['front']['t']-before['supports']['front']['t'])<.001:continue
        assert_config(after)
        for key in SUPPORTS:
            if key!='front':assert after['supports'][key]==before['supports'][key]
        for identifier in ('camera2','p100r'):
            assert sensors(after)[identifier]['inSupport']==sensors(before)[identifier]['inSupport']
            near(sensors(after)[identifier]['x'],sensors(before)[identifier]['x']);near(sensors(after)[identifier]['z'],sensors(before)[identifier]['z'])
        body_success=True;break
    assert body_success,body_attempts
    result['realPointerGizmosClampXYZEscAndDisabledPoints']={'axes':outcomes,'directPointerEscRestored':True,'sameGeometryImportDuringDragRemainsExact':True,'disabledPointMovesWithinBox':True,'sharedSupportYHandleMovesBoth':True,'sharedSupportEscRestoresExactAnchorFraction':True,'actualSharedCuboidBodyDrag':body_attempts}


def verify_legacy_persistence_baseline(browser,initial,result):
    legacy=copy.deepcopy(initial);legacy['layoutRevision']=5;legacy.pop('supports')
    for sensor in legacy['sensors']:
        sensor.pop('inSupport',None)
        if sensor['id']=='camera3':sensor['mount']={'rail':'x-top-left','t':.31}
        if sensor['id']=='camera4':sensor['mount']={'rail':'x-top-right','t':.69}
    current=imported(browser,legacy)
    assert current['supports']['camera3']['rail']=='x-mid-left' and current['supports']['camera4']['rail']=='x-mid-right'
    near(current['supports']['camera3']['t'],.31);near(current['supports']['camera4']['t'],.69)
    assert imported(browser,current)==current
    browser.clickid('set-baseline');time.sleep(.2);saved=baseline(browser)
    browser.support('front');browser.set_input('support-size-x',26);browser.set_input('support-offset-z',32);browser.range('support-position',.81)
    edited=browser.config();assert edited['supports']['front']!=saved['supports']['front']
    assert baseline(browser)==saved;assert_metrics(browser,edited,saved)
    browser.clickid('restore-baseline');time.sleep(.4);assert browser.config()==saved
    browser.call('/refresh',{});browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0");time.sleep(.35)
    assert browser.config()==saved and baseline(browser)==saved
    before_files=set(browser.download_dir.glob('*.json'));browser.button('导出配置')
    deadline=time.monotonic()+15;exported=None
    while time.monotonic()<deadline:
        files=set(browser.download_dir.glob('*.json'))-before_files
        if files:
            try:download=next(iter(files));exported=json.loads(download.read_text());break
            except (OSError,ValueError):pass
        time.sleep(.15)
    assert exported and exported['config']==saved
    (ARTIFACTS/'real-exported-support-config.json').write_text(json.dumps(exported,ensure_ascii=False,indent=2))
    browser.support('camera1');browser.set_input('support-offset-z',23);assert browser.config()!=saved
    browser.import_config(exported);browser.wait("document.querySelector('.toast[role=status]')?.textContent.includes('已导入')");time.sleep(.35)
    assert browser.config()==saved
    result['legacyMigrationRealDownloadImportRefreshAndBaseline']={'layoutRevision':current['layoutRevision'],'leftMiddleT':current['supports']['camera3']['t'],'rightMiddleT':current['supports']['camera4']['t'],'realExportBytes':download.stat().st_size,'baselineOwnSupportPreservedUnderEditing':True}

def recommend_constraints(before,after):
    assert before['supports']==after['supports'] and before['chassis']==after['chassis'] and before['analysis']==after['analysis']
    moved=[]
    for identifier,old in sensors(before).items():
        new=sensors(after)[identifier]
        if identifier not in CAMERAS or not old['enabled']:assert old==new,(identifier,old,new)
        else:
            for key in set(old)-{'x','y','z','inSupport'}:assert new[key]==old[key],(identifier,key)
        if any(abs(new[a]-old[a])>1e-9 for a in 'xyz'):moved.append(identifier)
    assert_config(after);return moved

def verify_recommendations(browser,result):
    fixtures=json.loads((ARTIFACTS/'optimizer-fixtures.json').read_text());outcomes={}
    for name in ('single','shared'):
        before=imported(browser,fixtures[name]);browser.clickid('set-baseline');time.sleep(.2);saved=baseline(browser)
        outcome=rec.start(browser,'improved');proposed=outcome['config'];moved=recommend_constraints(before,proposed)
        assert set(moved)==set(outcome['movedIds'])
        for phase,cfg in (('before',before),('after',proposed)):
            ref=reference(cfg)
            for name2,source in (('camera',ref['camera']),('overall',ref['overall'])):
                near(outcome[phase][name2+'Percent'],source['percent'])
                near(outcome[phase][name2+'Covered'],source['covered'])
                text=browser.evaluate("return document.querySelector('[data-testid=recommend-"+name2+'-'+phase+"]').textContent")
                near(float(text.rstrip('%')),source['percent'],.0051)
        assert outcome['after']['cameraCovered']>outcome['before']['cameraCovered']
        assert outcome['after']['overallCovered']>=outcome['before']['overallCovered']
        assert browser.config()==before and baseline(browser)==saved
        browser.clickid('recommend-preview');time.sleep(.3)
        assert set(state(browser)['recommendationIds'])==set(moved)
        assert browser.config()==before and baseline(browser)==saved
        browser.clickid('view-mounting');browser.evaluate("document.querySelector('.panel-scroll').scrollTop=0;document.querySelector('button[aria-label=关闭提示]')?.click()")
        browser.screenshot('supports-recommend-'+name+'.png')
        browser.clickid('recommend-apply');time.sleep(.4);assert browser.config()==proposed and baseline(browser)==saved
        assert state(browser)['recommendationIds']==[]
        browser.clickid('recommend-undo');time.sleep(.4);assert browser.config()==before and baseline(browser)==saved
        rec.start(browser,'improved');browser.support('camera1');browser.set_input('support-offset-x',-9)
        assert rec.status(browser) is None and state(browser)['recommendationIds']==[]
        outcomes[name]={'before':outcome['before'],'after':outcome['after'],'movedIds':moved,'supportsFixed':True,'disabledPartnerStationary':True}
    result['insideBoxNativeWorkerPreviewApplyUndoAndInvalidate']=outcomes

def verify_large_box_view_fit(browser,initial,result):
    fixture=copy.deepcopy(initial)
    fixture['supports']['mid360'].update(sizeX=1.5,sizeY=1.5,sizeZ=1.5,offsetX=.5,offsetY=.5,offsetZ=.5)
    configured=imported(browser,fixture)
    checked=[]
    for mode in ('mounting','perspective'):
        browser.viewport(1440,960)
        browser.clickid('view-'+mode);time.sleep(.25)
        canvas=browser.evaluate("const r=document.querySelector('.three-host canvas').getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}")
        data=state(browser)
        for box in data['supportBoxProjections']:
            for point in box['corners']:
                assert canvas['left']-1<=point['x']<=canvas['right']+1 and canvas['top']-1<=point['y']<=canvas['bottom']+1,(mode,box['id'],point,canvas)
        assert browser.config()==configured
        checked.append(mode)
    assert_metrics(browser,configured)
    browser.screenshot('supports-large-space-fit.png')
    result['viewResetFramesAllLegalLargeAndOffsetSupportCorners']={'modes':checked,'mid360':configured['supports']['mid360']}
    imported(browser,initial)


def verify_layouts(browser,initial,result):
    imported(browser,initial);browser.sensor('camera3');browser.clickid('view-mounting');browser.clickid('select-support')
    result['layouts']={}
    for name,width,height in (('desktop-1440',1440,960),('desktop-1920',1920,1080),('tablet-768',768,1024),('mobile-390',390,844)):
        browser.viewport(width,height);browser.evaluate("scrollTo(0,0);document.querySelector('.panel-scroll').scrollTop=0;document.querySelector('button[aria-label=关闭提示]')?.click()")
        time.sleep(.3);layout=browser.layout()
        assert layout['scrollWidth']<=width+2 and not layout['overflow'],(name,layout)
        assert browser.config()==initial;assert_scene(browser,initial)
        browser.screenshot('supports-'+name+'.png');result['layouts'][name]=layout
        if name=='mobile-390':
            browser.evaluate("document.querySelector('.visual-panel').scrollIntoView({block:'start'})");browser.screenshot('supports-mobile-scene.png')
    browser.viewport(1440,960);browser.evaluate('scrollTo(0,0)');browser.clickid('view-top');time.sleep(.2);browser.screenshot('supports-default-top.png')
    browser.clickid('view-perspective');time.sleep(.2);browser.screenshot('supports-default-perspective.png')
    before=int(state(browser)['frameCount']);time.sleep(.35);after=int(state(browser)['frameCount']);assert before==after,(before,after)
    result['renderOnDemandWhileIdle']=True

def verify():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    profile=Path.home()/'snap/firefox/common/observer-plastic-support-qa-20261024';profile.mkdir(parents=True,exist_ok=True)
    processes,logs=[],[];browser=None;downloads=None
    result={'pass':False,'scope':'Independent real Firefox/WebGL/native Worker, synthetic loopback backend, actual pointer and JSON download; no production/hardware'}
    starting={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'coverage-config/src').rglob('*')) if p.is_file()}
    try:
        prepare_reference();downloads=tempfile.TemporaryDirectory(prefix='supports-download-',dir=profile)
        password=secrets.token_urlsafe(24);env={**os.environ,'COVERAGE_QA_PASSWORD':password,'LIBGL_ALWAYS_SOFTWARE':'1','MOZ_WEBRENDER':'1'}
        authority=profile/'Xauthority';authority.touch(mode=0o600,exist_ok=True)
        subprocess.run(['xauth','-f',str(authority),'add',':93','MIT-MAGIC-COOKIE-1',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        xvfb=Path('/home/user/.local/state/robot-cloud/frame-mount-20261024/artifacts/xvfb-runtime/usr/bin/Xvfb');assert xvfb.exists()
        log=(ARTIFACTS/'supports-xvfb.log').open('w');logs.append(log)
        processes.append(subprocess.Popen([str(xvfb),':93','-screen','0','1920x1080x24','-nolisten','tcp','-auth',str(authority)],stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        env.update(DISPLAY=':93',XAUTHORITY=str(authority));time.sleep(.4)
        for command,name in (([sys.executable,__file__,'--serve'],'supports-server'),(['/snap/bin/geckodriver','--host','127.0.0.1','--port','19249','--profile-root',str(profile)],'supports-geckodriver')):
            log=(ARTIFACTS/(name+'.log')).open('w');logs.append(log);processes.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,cwd=ROOT,env=env))
        until=time.monotonic()+25
        while time.monotonic()<until:
            try:qa.request(SERVER+'/healthz');qa.request(DRIVER+'/status');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise AssertionError('Isolated services did not start')
        shared.drag.verify_auth(result)
        created=qa.request(DRIVER+'/session','POST',{'capabilities':{'alwaysMatch':{'browserName':'firefox','webSocketUrl':True,'moz:firefoxOptions':{'binary':'/snap/firefox/current/usr/lib/firefox/firefox','args':['-headless'],'prefs':{'webgl.force-enabled':True,'webgl.disabled':False,'gfx.webrender.software':True,'webgl.enable-software-renderer':True,'browser.download.folderList':2,'browser.download.dir':downloads.name,'browser.download.useDownloadDir':True,'browser.download.alwaysOpenPanel':False,'browser.helperApps.neverAsk.saveToDisk':'application/json,application/octet-stream'}}}}},timeout=60)['value']
        browser=Browser(created);browser.download_dir=Path(downloads.name);browser.all_errors=[];browser.all_shader_errors=[]
        result['browser']={k:created['capabilities'].get(k) for k in ('browserName','browserVersion','platformName')}
        asyncio.run(browser.bidi([('script.addPreloadScript',{'functionDeclaration':preload_source()})]))
        browser.call('/url',{'url':SERVER+'/login'})
        login=browser.call('/execute/async',{'script':"const done=arguments[arguments.length-1];fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:arguments[0],password:arguments[1]})}).then(async r=>done({status:r.status,body:await r.json()})).catch(e=>done({error:String(e)}));",'args':['coverage-fixture',password]});assert login['status']==200
        browser.evaluate('localStorage.removeItem('+json.dumps(STORAGE)+');localStorage.removeItem('+json.dumps(BASELINE)+');sessionStorage.removeItem("__sharedFixtureSeed")')
        browser.viewport(1440,960);browser.call('/url',{'url':SERVER+'/coverage'});browser.wait("document.querySelectorAll('.sensor-select').length===8&&__coverageQA.drawCalls>0")
        time.sleep(.35);initial=browser.config();assert_config(initial);assert_scene(browser,initial)
        assert initial['supports']['camera3']['rail']=='x-mid-left' and initial['supports']['camera4']['rail']=='x-mid-right'
        mid=box_geometry(initial,'mid360');assert mid['center']['z']>mid['anchor']['z'] and initial['supports']['mid360']['style']=='pedestal'
        for key in SUPPORTS:assert_buffer_dimensions(browser,[box_geometry(initial,key)['size'][a] for a in 'xyz'])
        result['initialExactGeometry']=assert_metrics(browser,initial)
        result['webgl']=browser.evaluate('return {contexts:__coverageQA.contexts,drawCalls:__coverageQA.drawCalls}')
        verify_numeric_and_shared(browser,initial,result)
        verify_support_parameters(browser,initial,result)
        verify_pointer_drags(browser,initial,result)
        verify_legacy_persistence_baseline(browser,initial,result)
        verify_recommendations(browser,result)
        verify_large_box_view_fit(browser,initial,result)
        verify_layouts(browser,initial,result)
        rec.verify_auth_assets(browser,result)
        refine.errors(browser);result['errors']=browser.all_errors;result['shaderErrors']=browser.all_shader_errors
        polish.verify_loaded_assets(browser,result)
        assert result['sourceDigests']==starting,'Source changed during final browser acceptance'
        shared.verify_checked_build(result)
        result['nativeWorkerTraceSummary']=browser.evaluate("return __cameraQA.events.filter(e=>e.type==='created'||e.type==='terminated').map(e=>({type:e.type,id:e.id,url:e.url}))")
        result['pass']=True
    except Exception as error:
        result['failure']=str(error)
        if browser:
            try:
                browser.screenshot('supports-failure.png');result['failureText']=browser.evaluate('return document.body.innerText');result['failureConfig']=browser.config();result['failureScene']=state(browser);result['errors']=browser.all_errors+browser.evaluate('return __coverageQA.errors')
            except Exception as capture:result['captureFailure']=str(capture)
        raise
    finally:
        (ARTIFACTS/'final-browser-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if browser:
            try:browser.call('',method='DELETE')
            except OSError:pass
        for process in processes:process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()
        for log in logs:log.close()
        if downloads:downloads.cleanup()
        print(json.dumps({'pass':result['pass'],'failure':result.get('failure'),'report':str(ARTIFACTS/'final-browser-report.json')},ensure_ascii=False))

if __name__=='__main__':serve() if '--serve' in sys.argv else verify()
