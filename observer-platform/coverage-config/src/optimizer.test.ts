import { describe, expect, it } from 'vitest';
import { computeCoverage } from './geometry';
import { supportGeometry } from './mounting';
import { DEFAULT_CONFIG, sanitizeConfig, type Config, type Sensor } from './model';
import { optimizeCameraPositions } from './optimizer';
import type { OptimizeCameraOptions, RecommendationResult } from './optimizer';
import { createRecommendationController } from './optimizer.worker';
import type { RecommendationResponse } from './optimizer.worker';
import { computeNearFieldMetrics, NEAR_FIELD_REGIONS } from './near-field';
import { optimizeSideCameras } from './side-optimizer-core';
import type { SideOptimizerEngine } from './side-optimizer-core';
import { Vector3 } from 'three';

const SIDE_IDS = ['camera3','camera4'] as const;
const get = (config: Config,id: string): Sensor => config.sensors.find(sensor => sensor.id === id)!;
const fixture = (): Config => {
  const config = sanitizeConfig(DEFAULT_CONFIG);
  config.analysis = {length:4,width:4,height:0,resolution:.2,occlusion:true};
  return sanitizeConfig(config);
};
const angles = {
  leftAngles:[{yaw:75,pitch:25},{yaw:90,pitch:35},{yaw:105,pitch:45}],
  rightAngles:[{yaw:-105,pitch:25},{yaw:-90,pitch:35},{yaw:-75,pitch:45}],
};
const finiteOptions = {positionCandidatesCm:[-10,0,10],...angles,refinement:false,coverageProtection:'all-regions' as const};
const cameraCoverage = (config: Config) => computeCoverage({...config,sensors:config.sensors.map(sensor => ({...sensor,
  enabled:sensor.enabled && ['camera','depth'].includes(sensor.kind),
}))});
function verifyFixed(original: Config,result: RecommendationResult) {
  expect(result.config.chassis).toEqual(original.chassis);
  expect(result.config.analysis).toEqual(original.analysis);
  for (const item of original.sensors) {
    const proposed = get(result.config,item.id);
    if (!SIDE_IDS.includes(item.id as typeof SIDE_IDS[number])) expect(proposed).toEqual(item);
    else for (const key of ['y','z','enabled','inSupport','hfov','vfov','minRange','maxRange'] as const) expect(proposed[key]).toEqual(item[key]);
  }
  for (const id of Object.keys(original.supports!) as (keyof NonNullable<Config['supports']>)[]) {
    if (id !== 'camera3' && id !== 'camera4') expect(result.config.supports![id]).toEqual(original.supports![id]);
    else {
      const {t:_before,...before} = original.supports![id]!;
      const {t:afterT,...after} = result.config.supports![id]!;
      expect(after).toEqual(before);
      expect(afterT).toBeGreaterThanOrEqual(0);
      expect(afterT).toBeLessThanOrEqual(1);
    }
  }
  const leftDelta = get(result.config,'camera3').x-get(original,'camera3').x;
  const rightDelta = get(result.config,'camera4').x-get(original,'camera4').x;
  const offsets=result.alignmentOffsetsCm??{camera3:0,camera4:0};
  if(result.status==='improved') {
    expect(leftDelta-offsets.camera3/100).toBeCloseTo(rightDelta-offsets.camera4/100,10);
    expect(supportGeometry(result.config,'camera3').center.x).toBeCloseTo(supportGeometry(result.config,'camera4').center.x,10);
    expect(get(result.config,'camera3').yaw).toBeCloseTo(-get(result.config,'camera4').yaw,10);
    expect(get(result.config,'camera3').pitch).toBeCloseTo(get(result.config,'camera4').pitch,10);
  } else expect(leftDelta).toBeCloseTo(rightDelta,10);
  const actual = computeCoverage(result.config),cameras = cameraCoverage(result.config);
  expect(result.after.overallCovered).toBe(actual.covered);
  expect(result.after.overallBlindArea).toBe(actual.blindArea);
  expect(result.after.cameraCovered).toBe(cameras.covered);
  expect(result.after.cameraBlindArea).toBe(cameras.blindArea);
  expect(result.after.totalCells).toBe(actual.total);
  expect(result.after.nearField).toEqual(computeNearFieldMetrics(result.config,actual));
  expect(result.after.nearField!.covered).toBeGreaterThanOrEqual(result.before.nearField!.covered);
  expect(result.after.overallCovered).toBeGreaterThanOrEqual(result.before.overallCovered);
  for(const key of NEAR_FIELD_REGIONS) expect(result.after.nearField![key].covered).toBeGreaterThanOrEqual(result.before.nearField![key].covered);
}

describe('joint side-camera box position and angle recommendation',() => {
  it('matches an independent exhaustive search of a small joint candidate set',async () => {
    const config = fixture(),original = structuredClone(config);
    const before=computeNearFieldMetrics(config,computeCoverage(config));
    let minimumCenter=Infinity,maximumNear=-1,maximumOverall=-1;
    for (const delta of [-10,0,10]) for (const left of angles.leftAngles) for (const right of angles.rightAngles) {
      if(left.yaw!==-right.yaw||left.pitch!==right.pitch)continue;
      const proposal = structuredClone(config);
      const reference=(supportGeometry(config,'camera3').center.x+supportGeometry(config,'camera4').center.x)/2;
      for (const id of SIDE_IDS) {const box=supportGeometry(config,id);proposal.supports![id]!.t=(reference+delta/100-box.rail.start.x)/(box.rail.end.x-box.rail.start.x);}
      Object.assign(get(proposal,'camera3'),left); Object.assign(get(proposal,'camera4'),right);
      const coverage=computeCoverage(proposal),near=computeNearFieldMetrics(config,coverage);
      if(coverage.covered<computeCoverage(config).covered||near.covered<before.covered||NEAR_FIELD_REGIONS.some(key=>near[key].covered<before[key].covered))continue;
      const center=Math.abs((reference+delta/100)*100);
      if(center<minimumCenter-1e-8||Math.abs(center-minimumCenter)<=1e-8&&(near.covered>maximumNear||(near.covered===maximumNear&&coverage.covered>maximumOverall))){
        minimumCenter=center;maximumNear=near.covered;maximumOverall=coverage.covered;
      }
    }
    const result = await optimizeCameraPositions(config,finiteOptions);
    expect(result.after.nearField!.covered).toBe(maximumNear);
    expect(result.after.overallCovered).toBe(maximumOverall);
    expect(config).toEqual(original);
    verifyFixed(config,result);
  });

  it('marks angle-only improvements as applicable without translating either box',async () => {
    const config = fixture();
    config.sensors.forEach(sensor => {sensor.enabled = SIDE_IDS.includes(sensor.id as typeof SIDE_IDS[number]);});
    get(config,'camera3').pitch=0; get(config,'camera4').pitch=0;
    const result = await optimizeCameraPositions(config,{positionCandidatesCm:[0],leftAngles:[{yaw:90,pitch:45}],rightAngles:[{yaw:-90,pitch:45}],refinement:false});
    expect(result.status).toBe('improved');
    expect(result.translatedIds).toEqual([]);
    expect(result.rotatedIds).toEqual(['camera3','camera4']);
    expect(result.movedIds).toEqual(['camera3','camera4']);
    expect(result.commonDeltaCm).toBe(0);
    verifyFixed(config,result);
  });

  it('keeps the current layout when every candidate ties or is worse',async () => {
    const config = fixture();
    const result = await optimizeCameraPositions(config,{positionCandidatesCm:[0],leftAngles:[get(config,'camera3')],rightAngles:[{yaw:-90,pitch:55}],refinement:false});
    expect(result.status).toBe('no-improvement');
    expect(result.config).toEqual(config);
    expect(result.after).toEqual(result.before);
    expect(result.movedIds).toEqual([]);
  });

  it('intersects both rail limits and never clamps just one of the side boxes',async () => {
    const config = fixture(); config.supports!.camera4!.t=1;
    for(const id of SIDE_IDS)get(config,id).pitch=0;
    const normalized = sanitizeConfig(config);
    const result = await optimizeCameraPositions(normalized,finiteOptions);
    const reference=(supportGeometry(normalized,'camera3').center.x+supportGeometry(normalized,'camera4').center.x)/2;
    expect(result.movementRangeCm![1]).toBeCloseTo((supportGeometry(normalized,'camera4').rail.end.x-.09-reference)*100,10);
    expect(result.commonDeltaCm!).toBeLessThanOrEqual(result.movementRangeCm![1]);
    verifyFixed(normalized,result);
    for (const id of SIDE_IDS) {
      const box = supportGeometry(result.config,id);
      expect(box.min.x).toBeGreaterThanOrEqual(box.rail.start.x-1e-10);
      expect(box.max.x).toBeLessThanOrEqual(box.rail.end.x+1e-10);
    }
  });

  it('does not optimize front/depth cameras when both side cameras are disabled',async () => {
    const config = fixture(); SIDE_IDS.forEach(id => {get(config,id).enabled=false;});
    const result = await optimizeCameraPositions(config);
    expect(result.status).toBe('no-cameras');
    expect(result.config).toEqual(config);
    expect(result.movedIds).toEqual([]);
    expect(result.before.overallCovered).toBe(computeCoverage(config).covered);
    const empty = fixture(); empty.analysis.length=.5; empty.analysis.width=.5;
    const noArea = await optimizeCameraPositions(empty);
    expect(noArea.status).toBe('no-area');
    expect(noArea.before.totalCells).toBe(0);
  });

  it('supports a disabled side camera without changing its angle or enabled state',async () => {
    const config = fixture(); get(config,'camera4').enabled=false;
    const result = await optimizeCameraPositions(config,finiteOptions);
    expect(get(result.config,'camera4').yaw).toBe(get(config,'camera4').yaw);
    expect(get(result.config,'camera4').pitch).toBe(get(config,'camera4').pitch);
    verifyFixed(config,result);
  });

  it('retains non-divisible grid dimensions and two-dimensional scan slice semantics',async () => {
    const config = fixture();
    config.analysis = {...config.analysis,length:3.137,width:2.087,resolution:.167,height:get(config,'bluesea').z};
    get(config,'bluesea').enabled=true; get(config,'stl19p').enabled=true;
    const normalized = sanitizeConfig(config);
    const result = await optimizeCameraPositions(normalized,finiteOptions);
    verifyFixed(normalized,result);
  });

  it('honors budgets and reports finite monotonic progress',async () => {
    const config = fixture(),progress: number[]=[];
    const bounded = await optimizeCameraPositions(config,{maxEvaluations:1,onProgress:value=>progress.push(value)});
    expect(bounded.status).toBe('no-improvement');
    expect(bounded.evaluations).toBe(1);
    expect(progress.at(-1)).toBe(1);
    expect(progress.every((value,index)=>Number.isFinite(value)&&value>=0&&value<=1&&(!index||value>=progress[index-1]))).toBe(true);
    const short = await optimizeCameraPositions(config,{maxMs:1});
    verifyFixed(config,short);
  });

  it('cancels before starting and cooperatively during an asynchronous search',async () => {
    const config = fixture();
    await expect(optimizeCameraPositions(config,{shouldCancel:()=>true})).rejects.toMatchObject({name:'AbortError'});
    let cancelled=false;
    await expect(optimizeCameraPositions(config,{...finiteOptions,shouldCancel:()=>cancelled,onProgress:value=>{if(value>0)cancelled=true;}})).rejects.toMatchObject({name:'AbortError'});
  });
});

describe('recommendation worker request lifecycle',() => {
  it('does not publish progress or results from a superseded or cancelled request',async () => {
    const config=fixture();
    const result=await optimizeCameraPositions(config,{maxEvaluations:1});
    const pending: {resolve:(value:RecommendationResult)=>void;options?:OptimizeCameraOptions}[]=[];
    const messages: RecommendationResponse[]=[];
    const controller=createRecommendationController(message=>messages.push(message),(_config,options)=>new Promise(resolve=>pending.push({resolve,options})));
    const first=controller({type:'start',generation:1,key:'first',config});
    const second=controller({type:'start',generation:2,key:'second',config});
    expect(pending[0].options!.shouldCancel!()).toBe(true);
    pending[0].options!.onProgress!(.9); pending[0].resolve(result); await first;
    expect(messages.filter(message=>message.type==='result')).toEqual([]);
    pending[1].resolve(result); await second;
    expect(messages.filter(message=>message.type==='result').map(message=>message.key)).toEqual(['second']);
    const third=controller({type:'start',generation:3,key:'third',config});
    await controller({type:'cancel',generation:3,key:'third'});
    expect(pending[2].options!.shouldCancel!()).toBe(true);
    pending[2].resolve(result); await third;
    expect(messages.filter(message=>message.type==='result').map(message=>message.key)).toEqual(['second']);
  });
});

/** Analytic visibility makes near-field/far-field tradeoffs explicit and stable. */
function syntheticEngine(mode:'tradeoff'|'plateau'|'cannot-correct'|'overlap-loss'='tradeoff'):SideOptimizerEngine {
  const points=mode==='overlap-loss'?[new Vector3(.2,.6,0),new Vector3(-.8,0,0),new Vector3(.2,-.6,0),new Vector3(-.8,.6,0),new Vector3(-.8,-.6,0),new Vector3(2,2,0),new Vector3(2,-2,0)]:[new Vector3(-.2,.6,0),new Vector3(.2,.6,0),new Vector3(-.2,-.6,0),new Vector3(.2,-.6,0),new Vector3(.1,.8,0),new Vector3(2,2,0),new Vector3(2,-2,0),new Vector3(-2,2,0)];
  const test=(sensor:Sensor)=>(point:Vector3)=>{
    if(!sensor.enabled||!SIDE_IDS.some(id=>id===sensor.id))return false;
    const index=points.findIndex(item=>item.x===point.x&&item.y===point.y);
    if(mode==='overlap-loss')return sensor.id==='camera3'?(sensor.x>.45?index===0:index===3||index===5):(sensor.x>.45?index===1||index===2:index===4||index===6);
    if(sensor.id==='camera4')return sensor.pitch===50 ? index===2||index===3 : index===2;
    if(mode==='cannot-correct'&&sensor.x<.45)return index===1||index===4;
    if(sensor.pitch===45)return index===0||index===1||index>=5; // a mirrored compromise retains far-field coverage
    if(sensor.pitch===55)return index===1||index===4||index>=5; // gains side cells but loses left-rear coverage
    if(sensor.pitch===50&&(mode!=='plateau'||sensor.x>=.02-1e-10&&sensor.x<=.08+1e-10))return index===0||index===1||mode==='plateau'&&index>=5;
    return index===0||index>=5;
  };
  return {
    normalize:sanitizeConfig,support:supportGeometry,prepareSensor:test,
    visible:(prepared,point)=>prepared(point),
    coverage:input=>{
      const config=sanitizeConfig(input),perSensor=Object.fromEntries(config.sensors.map(sensor=>[sensor.id,0]));
      const cells=points.map(point=>{let count=0;for(const sensor of config.sensors)if(test(sensor)(point)){count++;perSensor[sensor.id]++;}return {x:point.x,y:point.y,count};});
      const covered=cells.filter(cell=>cell.count>0).length;
      return {cells,total:cells.length,covered,blind:cells.length-covered,overlap:cells.filter(cell=>cell.count>1).length,percent:covered/cells.length*100,blindArea:(cells.length-covered)*.01,area:cells.length*.01,perSensor,step:.1,cellLength:.1,cellWidth:.1};
    },
  };
}

describe('near-field priority and legal whole-box recommendations',()=>{
  const onlyAngles={coverageProtection:'all-regions' as const,positionCandidatesCm:[0],leftAngles:[{yaw:90,pitch:50}],rightAngles:[{yaw:-90,pitch:50}],refinement:false,yieldControl:async()=>{}};

  it('improves near-field coverage while rejecting a larger near gain that lowers overall coverage',async()=>{
    const config=fixture(),result=await optimizeSideCameras(config,syntheticEngine(),{...onlyAngles,leftAngles:[{yaw:90,pitch:45},{yaw:90,pitch:50}],rightAngles:[{yaw:-90,pitch:45},{yaw:-90,pitch:50}]});
    expect(result.status).toBe('improved');
    expect(result.before.nearField.covered).toBe(2);
    expect(result.after.nearField.covered).toBe(3);
    expect(result.before.overallCovered).toBe(5);
    expect(result.after.overallCovered).toBe(6);
    for(const key of NEAR_FIELD_REGIONS)expect(result.after.nearField[key].covered).toBeGreaterThanOrEqual(result.before.nearField[key].covered);
  });

  it('rejects a total near-field gain that sacrifices one protected side-rear region',async()=>{
    const config=fixture();
    const result=await optimizeSideCameras(config,syntheticEngine(),{...onlyAngles,leftAngles:[{yaw:90,pitch:55}],rightAngles:[{yaw:-90,pitch:55}]});
    expect(result.status).toBe('unchanged');
    expect(result.config).toEqual(config);
    expect(result.after.nearField.leftRear.covered).toBe(result.before.nearField.leftRear.covered);
  });

  it('can correct an overhanging current layout without requiring an area gain',async()=>{
    const config=fixture();for(const id of SIDE_IDS)config.supports![id]!.t=1;
    const current=sanitizeConfig(config);
    const result=await optimizeSideCameras(current,syntheticEngine(),{coverageProtection:'all-regions',positionCandidatesCm:[-10],leftAngles:[get(current,'camera3')],rightAngles:[get(current,'camera4')],refinement:false,yieldControl:async()=>{}});
    expect(result.status).toBe('improved');
    expect(result.reason).toContain('已纠正');
    expect(result.after.nearField.covered).toBe(result.before.nearField.covered);
    expect(result.commonDeltaCm).toBe(-10);
    for(const id of SIDE_IDS){const box=supportGeometry(result.config,id);expect(box.min.x).toBeGreaterThanOrEqual(box.rail.start.x-1e-8);expect(box.max.x).toBeLessThanOrEqual(box.rail.end.x+1e-8);}
  });

  it('returns the untouched baseline with an explanation when no legal protected layout exists',async()=>{
    const config=fixture();for(const id of SIDE_IDS)config.supports![id]!.t=1;
    const current=sanitizeConfig(config);
    const result=await optimizeSideCameras(current,syntheticEngine('cannot-correct'),{coverageProtection:'all-regions',positionCandidatesCm:[-10],leftAngles:[get(current,'camera3')],rightAngles:[get(current,'camera4')],refinement:false,yieldControl:async()=>{}});
    expect(result.status).toBe('unchanged');
    expect(result.reason).toContain('未找到');
    expect(result.config).toEqual(current);
    expect(result.movedIds).toEqual([]);
  });

  it('keeps the boxes centered when a farther plateau offers extra near-field coverage',async()=>{
    const result=await optimizeSideCameras(fixture(),syntheticEngine('plateau'),{coverageProtection:'all-regions',leftAngles:[{yaw:90,pitch:50}],rightAngles:[{yaw:-90,pitch:50}],refinement:false,yieldControl:async()=>{},maxMs:5000});
    expect(result.status).toBe('improved');
    expect(result.positionScanComplete).toBe(true);
    expect(result.commonDeltaCm).toBe(0);
    expect(result.after.nearField.covered).toBe(3);
  });

  it('accounts for nonzero box offsets against the real beam ends',async()=>{
    const config=fixture();config.supports!.camera3!.offsetX=.08;config.supports!.camera4!.offsetX=-.04;
    const normalized=sanitizeConfig(config);
    const result=await optimizeSideCameras(normalized,syntheticEngine(),onlyAngles);
    const boxes=SIDE_IDS.map(id=>supportGeometry(normalized,id)),reference=(boxes[0].center.x+boxes[1].center.x)/2;
    expect(result.movementRangeCm![0]).toBeCloseTo(Math.max(...boxes.map(box=>Math.max(box.rail.start.x,box.rail.start.x-box.layout.offsetX+box.layout.sizeX/2)-reference))*100,10);
    expect(result.movementRangeCm![1]).toBeCloseTo(Math.min(...boxes.map(box=>Math.min(box.rail.end.x,box.rail.end.x-box.layout.offsetX-box.layout.sizeX/2)-reference))*100,10);
    for(const id of SIDE_IDS){const box=supportGeometry(result.config,id);expect(box.min.x).toBeGreaterThanOrEqual(box.rail.start.x-box.layout.offsetX-1e-8);expect(box.max.x).toBeLessThanOrEqual(box.rail.end.x-box.layout.offsetX+1e-8);}
  });

  it('explains when a box cannot fit any point of its offset center path',async()=>{
    const config=fixture();config.chassis.frameLength=.2;
    for(const id of SIDE_IDS)config.supports![id]!.offsetX=.5;
    const normalized=sanitizeConfig(config),result=await optimizeSideCameras(normalized,syntheticEngine(),onlyAngles);
    expect(result.status).toBe('unchanged');
    expect(result.reason).toContain('没有能同时容纳');
    expect(result.config).toEqual(normalized);
  });

  it('protects the near-field union when overlapping regions individually gain coverage',async()=>{
    const config=fixture();for(const id of SIDE_IDS)config.supports![id]!.t=1;
    const current=sanitizeConfig(config),engine=syntheticEngine('overlap-loss'),proposed=structuredClone(current);
    for(const id of SIDE_IDS)proposed.supports![id]!.t=.9;
    const before=computeNearFieldMetrics(current,engine.coverage(current));
    const after=computeNearFieldMetrics(current,engine.coverage(proposed));
    expect(after.covered).toBeLessThan(before.covered);
    for(const key of NEAR_FIELD_REGIONS)expect(after[key].covered).toBeGreaterThanOrEqual(before[key].covered);
    const result=await optimizeSideCameras(current,engine,{coverageProtection:'all-regions',positionCandidatesCm:[-10],leftAngles:[get(current,'camera3')],rightAngles:[get(current,'camera4')],refinement:false,yieldControl:async()=>{}});
    expect(result.status).toBe('unchanged');
    expect(result.config).toEqual(current);
    expect(result.after.nearField.covered).toBe(before.covered);
  });
});
