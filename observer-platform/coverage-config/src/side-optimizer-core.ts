import { Vector3 } from 'three';
import type { Chassis, Config, Sensor } from './model';
import type { CoverageResult } from './geometry';
import type { SupportGeometry } from './mounting';
import { computeNearFieldMetrics, nearFieldFlags, NEAR_FIELD_REGIONS } from './near-field';
import type { NearFieldMetrics, NearFieldRegion } from './near-field';
import { sidePositionReference } from './side-symmetry';
import type { SideAlignmentOffsets } from './side-symmetry';

type SideId = 'camera3' | 'camera4';
export interface CameraAngle { yaw: number; pitch: number }
type AngleRange = [number, number];
interface RefinementLevel { positionStepCm: number; positionRadiusCm: number; angleStep: number; angleRadius: number }
interface CandidateMask extends CameraAngle { mask: Uint32Array; residual: Uint32Array }
type NearCounts = Record<'near' | NearFieldRegion, number>;
interface Plan { deltaCm: number; left: CandidateMask; right: CandidateMask; overallCovered: number; cameraCovered: number; near: NearCounts; centerDistanceCm: number; penalty: number }
export interface SideOptimizerMetrics { cameraCovered: number; cameraPercent: number; overallCovered: number; overallPercent: number; nearField: NearFieldMetrics }
export interface SideOptimizerResult {
  status: 'improved' | 'unchanged' | 'no-cameras' | 'no-area';
  reason?: string;
  coverageProtection: 'overall' | 'all-regions';
  config: Config;
  before: SideOptimizerMetrics;
  after: SideOptimizerMetrics;
  movedIds: string[];
  rotatedIds: string[];
  changedIds: string[];
  translatedIds?: string[];
  evaluations: number;
  visibilityEvaluations?: number;
  commonDeltaCm?: number;
  commonReferenceXCm?: number;
  alignmentOffsetsCm?: SideAlignmentOffsets;
  beamMidpointXCm?: number;
  recommendedBoxCenterXCm?: number;
  beforeCenterDistanceCm?: number;
  afterCenterDistanceCm?: number;
  movementRangeCm?: [number, number];
  positionScanComplete?: boolean;
  searchLimited?: boolean;
}
export interface SideOptimizerEngine {
  normalize: (config: Config) => Config;
  coverage: (config: Config) => CoverageResult;
  support: (config: Config, id: SideId) => SupportGeometry;
  prepareSensor: (sensor: Sensor) => (point: Vector3) => boolean;
  visible: (prepared: (point: Vector3) => boolean, point: Vector3, chassis: Chassis, occlusion: boolean) => boolean;
}
export interface SideOptimizerOptions {
  onProgress?: (progress: number) => void;
  shouldCancel?: () => boolean;
  signal?: AbortSignal;
  yieldControl?: () => Promise<void>;
  yieldEvery?: number;
  maxEvaluations?: number;
  maxMs?: number;
  positionCandidatesCm?: number[];
  leftAngles?: CameraAngle[];
  rightAngles?: CameraAngle[];
  leftYawRange?: AngleRange;
  rightYawRange?: AngleRange;
  pitchRange?: AngleRange;
  positionStepCm?: number;
  yawStep?: number;
  pitchStep?: number;
  refinement?: boolean;
  refinementLevels?: RefinementLevel[];
  seedCount?: number;
  nearFieldRangeMeters?: number;
  coverageProtection?: 'overall' | 'all-regions';
}

/**
 * Joint finite-candidate optimization of the two side cameras.
 *
 * The engine adapter exposes normalize, coverage, support, prepareSensor and
 * visible. Only supports.camera3/camera4.t and camera3/camera4.yaw/pitch are
 * optimized. Returned world coordinates are rebuilt by engine.normalize.
 * This is a coarse search with local refinement, not a continuous global proof.
 */
const SIDE_IDS: SideId[] = ['camera3', 'camera4'];
const CAMERA_KINDS = new Set(['camera', 'depth']);
const EPS = 1e-8;
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const radians = (degrees: number) => degrees * Math.PI / 180;
const rounded = (value: number) => Math.round(value * 1e9) / 1e9;
const popcount = (value: number) => {
  value -= (value >>> 1) & 0x55555555;
  value = (value & 0x33333333) + ((value >>> 2) & 0x33333333);
  return (((value + (value >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
};
const countMask = (mask: Uint32Array) => mask.reduce((sum, word) => sum + popcount(word), 0);
const angularDistance = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

export class OptimizationCancelledError extends Error {
  readonly code = 'ABORT_ERR';
  constructor() {
    super('Side-camera optimization was cancelled.');
    this.name = 'AbortError';
  }
}

function valuesBetween(minimum: number, maximum: number, step: number, extras: number[] = []): number[] {
  if (!(step > 0) || !Number.isFinite(step)) throw new RangeError('Search steps must be positive.');
  const values = [minimum, maximum, ...extras];
  for (let index = Math.ceil((minimum - EPS) / step); index * step <= maximum + EPS; index++) values.push(index * step);
  return [...new Set(values.filter(value => Number.isFinite(value) && value >= minimum - EPS && value <= maximum + EPS).map(rounded))].sort((a, b) => a - b);
}

function uniqueAngles(angles: CameraAngle[]): CameraAngle[] {
  const seen = new Set();
  return angles.filter(({yaw, pitch}) => {
    if (!Number.isFinite(yaw) || !Number.isFinite(pitch)) throw new TypeError('Candidate angles must be finite.');
    const key = `${rounded(yaw)}:${rounded(pitch)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).map(({yaw, pitch}) => ({yaw:rounded(yaw), pitch:rounded(pitch)}));
}

/**
 * Options may narrow candidates for deterministic exhaustive checks:
 * positionCandidatesCm, leftAngles, rightAngles, refinement:false.
 * Default search: every 0.5 cm position with mirrored 15-degree yaw /
 * 10-degree pitch candidates, then local 5-degree / 1-degree angle refinement.
 */
export async function optimizeSideCameras(input: Config, engine: SideOptimizerEngine, options: SideOptimizerOptions = {}): Promise<SideOptimizerResult> {
  for (const name of ['normalize', 'coverage', 'support', 'prepareSensor', 'visible'] as const) {
    if (typeof engine?.[name] !== 'function') throw new TypeError(`Missing engine.${name}.`);
  }
  const started=performance.now(),base=engine.normalize(clone(input)),baseline=engine.coverage(base);
  const rangeMeters=Number.isFinite(options.nearFieldRangeMeters) && options.nearFieldRangeMeters!>0 ? options.nearFieldRangeMeters! : 1;
  const baselineNear=computeNearFieldMetrics(base,baseline,rangeMeters);
  const coverageProtection=options.coverageProtection??'overall';
  const side=SIDE_IDS.map(id=>base.sensors.find(sensor=>sensor.id===id)!);
  if(side.some(sensor=>!sensor)) throw new TypeError('Both side-camera records are required.');
  const initialMetrics:SideOptimizerMetrics={cameraCovered:0,cameraPercent:0,overallCovered:baseline.covered,overallPercent:baseline.percent,nearField:baselineNear};
  const early=(status:SideOptimizerResult['status'],reason:string):SideOptimizerResult=>({status,reason,coverageProtection,config:base,before:initialMetrics,after:initialMetrics,movedIds:[],rotatedIds:[],changedIds:[],translatedIds:[],evaluations:0});
  if(!side.some(sensor=>sensor.enabled)) return early('no-cameras','请先启用左相机和右相机。');
  if(!side.every(sensor=>sensor.enabled)) return early('unchanged','需要同时启用左右相机才能计算镜像对称推荐，已保留当前参数。');
  if(!baseline.total) return early('no-area','当前切片没有可分析区域。');
  const checkCancelled=()=>{if(options.signal?.aborted || options.shouldCancel?.()) throw new OptimizationCancelledError();};
  let lastProgress=0,workSinceYield=0,evaluations=0,visibilityEvaluations=0;
  const report=(progress:number)=>{checkCancelled();lastProgress=Math.max(lastProgress,Math.min(1,Math.max(0,progress)));options.onProgress?.(lastProgress);};
  const yieldControl=options.yieldControl || (()=>new Promise<void>(resolve=>setTimeout(resolve,0)));
  const checkpoint=async(work=1)=>{checkCancelled();workSinceYield+=work;if(workSinceYield>=(options.yieldEvery??256)){workSinceYield=0;await yieldControl();checkCancelled();}};
  checkCancelled();report(0);
  const supports=SIDE_IDS.map(id=>engine.support(base,id));
  if(supports.some(support=>support.rail.axis!=='x')) return early('unchanged','左右安装盒需要挂载在前后方向的梁上。');
  const {commonReferenceXCm,alignmentOffsetsCm}=sidePositionReference(base);
  const referenceX=commonReferenceXCm/100;
  // Rail paths include offsetX; subtract it to recover the physical beam ends.
  const beamMidpointXCm=supports.reduce((sum,box)=>sum+(box.rail.start.x+box.rail.end.x)/2-box.layout.offsetX,0)/supports.length*100;
  const beforeCenterDistanceCm=supports.reduce((sum,box)=>sum+Math.abs(box.center.x*100-beamMidpointXCm),0)/supports.length;
  const centerDeltaCm=beamMidpointXCm-commonReferenceXCm;
  const centerDistance=(deltaCm:number)=>Math.abs(commonReferenceXCm+deltaCm-beamMidpointXCm);
  const centerInfo={beamMidpointXCm,beforeCenterDistanceCm};
  const minDeltaCm=Math.max(...supports.map(support=>(Math.max(support.rail.start.x,support.rail.start.x-support.layout.offsetX+support.layout.sizeX/2)-referenceX)*100));
  const maxDeltaCm=Math.min(...supports.map(support=>(Math.min(support.rail.end.x,support.rail.end.x-support.layout.offsetX-support.layout.sizeX/2)-referenceX)*100));
  if(minDeltaCm>maxDeltaCm+EPS) return {...early('unchanged','当前框架没有能同时容纳两侧完整安装盒的对齐位置。'),commonReferenceXCm,alignmentOffsetsCm,...centerInfo,recommendedBoxCenterXCm:commonReferenceXCm,afterCenterDistanceCm:beforeCenterDistanceCm};
  const baselineBoxesLegal=supports.every(box=>box.min.x>=box.rail.start.x-box.layout.offsetX-EPS && box.max.x<=box.rail.end.x-box.layout.offsetX+EPS);
  const baselineAligned=Math.abs(supports[0].center.x-supports[1].center.x)<=EPS;
  const mirrored=(left:CameraAngle,right:CameraAngle)=>Math.abs(left.yaw+right.yaw)<=EPS && Math.abs(left.pitch-right.pitch)<=EPS;
  const mirror=(left:CameraAngle):CameraAngle=>({yaw:-left.yaw,pitch:left.pitch});
  const baselineMirrored=mirrored(side[0],side[1]);
  const baselineLegal=baselineBoxesLegal&&baselineAligned&&baselineMirrored;
  const points=baseline.cells.map(cell=>new Vector3(cell.x,cell.y,base.analysis.height));
  const words=Math.ceil(points.length/32),keys=['near',...NEAR_FIELD_REGIONS] as const;
  const regionMasks=Object.fromEntries(keys.map(key=>[key,new Uint32Array(words)])) as Record<typeof keys[number],Uint32Array>;
  for(let index=0;index<points.length;index++) {
    const flags=nearFieldFlags(base,points[index],rangeMeters);
    for(const key of keys) if(flags[key]) regionMasks[key][index>>>5]|=1<<(index&31);
  }
  const fixedOverall=new Uint32Array(words),fixedCamera=new Uint32Array(words);
  const sensorIsActive=(sensor:Sensor)=>sensor.enabled && (sensor.kind!=='scan' || Math.abs(radians(sensor.pitch))<=EPS && Math.abs(sensor.z-base.analysis.height)<=EPS);
  const maskForSensor=(sensor:Sensor):Uint32Array=>{
    const mask=new Uint32Array(words);
    if(!sensorIsActive(sensor)) return mask;
    const prepared=engine.prepareSensor(sensor);
    for(let index=0;index<points.length;index++) {
      if((index&255)===0) checkCancelled();
      if(engine.visible(prepared,points[index],base.chassis,base.analysis.occlusion)) mask[index>>>5]|=1<<(index&31);
    }
    visibilityEvaluations++;return mask;
  };
  for(const sensor of base.sensors) {
    if(SIDE_IDS.some(id=>id===sensor.id) || !sensorIsActive(sensor)) continue;
    const mask=maskForSensor(sensor);
    for(let word=0;word<words;word++){fixedOverall[word]|=mask[word];if(CAMERA_KINDS.has(sensor.kind)) fixedCamera[word]|=mask[word];}
    await checkpoint(64);
  }
  const fixedCount=countMask(fixedOverall);
  const fixedRegions=Object.fromEntries(keys.map(key=>[key,regionMasks[key].reduce((sum,word,index)=>sum+popcount(word&fixedOverall[index]),0)])) as NearCounts;
  const makeMask=(sensor:Sensor):CandidateMask=>{
    const mask=maskForSensor(sensor),residual=new Uint32Array(words);
    for(let word=0;word<words;word++) residual[word]=mask[word]&~fixedOverall[word];
    return {yaw:sensor.yaw,pitch:sensor.pitch,mask,residual};
  };
  const positions=new Map<number,Config>(),masks=new Map<string,CandidateMask>();
  const configAt=(deltaCm:number):Config=>{
    const key=rounded(deltaCm);
    if(!positions.has(key)) {
      if(deltaCm<minDeltaCm-EPS || deltaCm>maxDeltaCm+EPS) throw new RangeError('Candidate exceeds whole-box rail limits.');
      const config=clone(base);
      for(let index=0;index<SIDE_IDS.length;index++) {
        const span=supports[index].rail.end.x-supports[index].rail.start.x;
        if(span<=EPS) throw new RangeError('Cannot translate on a zero-length rail.');
        const t=(referenceX+deltaCm/100-supports[index].rail.start.x)/span;
        if(t < -EPS || t > 1+EPS) throw new RangeError('Candidate would be clamped by normalization.');
        config.supports={...config.supports,[SIDE_IDS[index]]:{...supports[index].layout,t}};
      }
      const normalized=engine.normalize(config);
      for(let index=0;index<SIDE_IDS.length;index++) {
        const moved=normalized.sensors.find(sensor=>sensor.id===SIDE_IDS[index])!;
        const box=engine.support(normalized,SIDE_IDS[index]);
        if(Math.abs(moved.x-side[index].x-(deltaCm+alignmentOffsetsCm[SIDE_IDS[index]])/100)>1e-7 || Math.abs(box.center.x-referenceX-deltaCm/100)>1e-7 || box.min.x<box.rail.start.x-box.layout.offsetX-EPS || box.max.x>box.rail.end.x-box.layout.offsetX+EPS) throw new Error('Normalization violated aligned whole-box translation.');
      }
      positions.set(key,normalized);
    }
    return positions.get(key)!;
  };
  const candidate=(index:number,deltaCm:number,angle:CameraAngle):CandidateMask=>{
    if(!side[index].enabled) angle=side[index];
    const key=`${index}:${rounded(deltaCm)}:${rounded(angle.yaw)}:${rounded(angle.pitch)}`;
    if(!masks.has(key)) masks.set(key,makeMask({...configAt(deltaCm).sensors.find(sensor=>sensor.id===SIDE_IDS[index])!,yaw:angle.yaw,pitch:angle.pitch}));
    return masks.get(key)!;
  };
  const score=(deltaCm:number,left:CandidateMask,right:CandidateMask):Plan=>{
    let overallCovered=fixedCount,cameraCovered=0;
    const near={...fixedRegions};
    for(let word=0;word<words;word++) {
      const additional=left.residual[word]|right.residual[word];
      overallCovered+=popcount(additional);
      cameraCovered+=popcount(fixedCamera[word]|left.mask[word]|right.mask[word]);
      if(regionMasks.near[word]) for(const key of keys) near[key]+=popcount(additional&regionMasks[key][word]);
    }
    evaluations++;
    const penalty=Math.abs(deltaCm)+.2*(angularDistance(left.yaw,side[0].yaw)+Math.abs(left.pitch-side[0].pitch)+angularDistance(right.yaw,side[1].yaw)+Math.abs(right.pitch-side[1].pitch));
    return {deltaCm,left,right,overallCovered,cameraCovered,near,centerDistanceCm:centerDistance(deltaCm),penalty};
  };
  // The current pose is a reference even when its box overhangs. It must not be
  // routed through configAt(0), which intentionally accepts only legal layouts.
  const basePlan=score(0,makeMask(side[0]),makeMask(side[1]));
  basePlan.centerDistanceCm=beforeCenterDistanceCm;
  if(basePlan.overallCovered!==baseline.covered || basePlan.near.near!==baselineNear.covered) throw new Error('Baseline masks disagree with the production grid.');
  const protectedPlan=(plan:Plan)=>mirrored(plan.left,plan.right) && plan.overallCovered>=baseline.covered && (coverageProtection==='overall' || plan.near.near>=baselineNear.covered && NEAR_FIELD_REGIONS.every(key=>plan.near[key]>=baselineNear[key].covered));
  const rankBetter=(a:Plan,b:Plan|null):boolean=>!b || a.centerDistanceCm<b.centerDistanceCm-EPS || Math.abs(a.centerDistanceCm-b.centerDistanceCm)<=EPS && (a.near.near>b.near.near || a.near.near===b.near.near && (a.overallCovered>b.overallCovered || a.overallCovered===b.overallCovered && a.penalty<b.penalty-EPS));
  const better=(a:Plan,b:Plan|null):boolean=>protectedPlan(a) && rankBetter(a,b);
  // Infeasible central poses still need angle refinement. Their closest coarse
  // angle is ranked by the number of cells missing from the protection floors.
  const deficit=(plan:Plan)=>Math.max(0,baseline.covered-plan.overallCovered)+(coverageProtection==='overall'?0:Math.max(0,baselineNear.covered-plan.near.near)+NEAR_FIELD_REGIONS.reduce((sum,key)=>sum+Math.max(0,baselineNear[key].covered-plan.near[key]),0));
  const seedBetter=(a:Plan,b:Plan|null)=>!b || deficit(a)<deficit(b) || deficit(a)===deficit(b)&&rankBetter(a,b);
  let best:Plan|null=baselineLegal?basePlan:null;
  const metrics=(plan:Plan):SideOptimizerMetrics=>{
    const region=(key:NearFieldRegion)=>({totalCells:baselineNear[key].totalCells,covered:plan.near[key],percent:baselineNear[key].totalCells ? plan.near[key]/baselineNear[key].totalCells*100 : 0});
    return {cameraCovered:plan.cameraCovered,cameraPercent:plan.cameraCovered/baseline.total*100,overallCovered:plan.overallCovered,overallPercent:plan.overallCovered/baseline.total*100,
      nearField:{rangeMeters,totalCells:baselineNear.totalCells,covered:plan.near.near,percent:baselineNear.totalCells?plan.near.near/baselineNear.totalCells*100:0,
        blindArea:(baselineNear.totalCells-plan.near.near)*baseline.cellLength*baseline.cellWidth,
        left:region('left'),right:region('right'),leftRear:region('leftRear'),rightRear:region('rightRear'),rear:region('rear')}};
  };
  const fallback=(reason:string):SideOptimizerResult=>({status:'unchanged',reason,coverageProtection,config:base,before:metrics(basePlan),after:metrics(basePlan),movedIds:[],rotatedIds:[],changedIds:[],translatedIds:[],evaluations,visibilityEvaluations,commonDeltaCm:0,commonReferenceXCm,alignmentOffsetsCm,...centerInfo,recommendedBoxCenterXCm:commonReferenceXCm,afterCenterDistanceCm:beforeCenterDistanceCm,movementRangeCm:[minDeltaCm,maxDeltaCm]});
  // One shared left-camera angle determines the right-camera mirror. Independent
  // left/right combinations must never enter the selectable candidate set.
  const yawBounds:AngleRange=[Math.max((options.leftYawRange??[45,135])[0],-(options.rightYawRange??[-135,-45])[1]),Math.min((options.leftYawRange??[45,135])[1],-(options.rightYawRange??[-135,-45])[0])];
  const pitchBounds:AngleRange=options.pitchRange??[0,85];
  const originalPair=baselineMirrored?[{yaw:side[0].yaw,pitch:side[0].pitch}]:[];
  let angles:CameraAngle[];
  if(options.leftAngles || options.rightAngles) {
    const left=options.leftAngles?uniqueAngles([...options.leftAngles,side[0]]):null;
    const right=options.rightAngles?uniqueAngles([...options.rightAngles,side[1]]):null;
    const proposed=left??right!.map(angle=>mirror(angle));
    angles=uniqueAngles([...proposed.filter(angle=>!right||right.some(other=>mirrored(angle,other))),...originalPair]);
  } else {
    const extrasYaw=[side[0].yaw,-side[1].yaw,(side[0].yaw-side[1].yaw)/2];
    const extrasPitch=[side[0].pitch,side[1].pitch,(side[0].pitch+side[1].pitch)/2];
    angles=uniqueAngles([...valuesBetween(...yawBounds,options.yawStep??15,extrasYaw).flatMap(yaw=>valuesBetween(...pitchBounds,options.pitchStep??10,extrasPitch).map(pitch=>({yaw,pitch}))),...originalPair]);
  }
  angles.sort((a,b)=>(angularDistance(a.yaw,side[0].yaw)+angularDistance(-a.yaw,side[1].yaw)+Math.abs(a.pitch-side[0].pitch)+Math.abs(a.pitch-side[1].pitch))-(angularDistance(b.yaw,side[0].yaw)+angularDistance(-b.yaw,side[1].yaw)+Math.abs(b.pitch-side[0].pitch)+Math.abs(b.pitch-side[1].pitch)));
  if(!angles.length) return fallback('指定角度范围中没有同时满足左右镜像的角度，已保留当前参数。');
  const centerFirst=(values:number[])=>values.sort((a,b)=>centerDistance(a)-centerDistance(b)||Math.abs(a)-Math.abs(b)||a-b);
  const provided=options.positionCandidatesCm;
  const deltas=centerFirst(provided ? valuesBetween(minDeltaCm,maxDeltaCm,Math.max(1,maxDeltaCm-minDeltaCm+1),[...provided,0]).filter(value=>value===0||provided.some(item=>Math.abs(item-value)<=EPS)) : valuesBetween(minDeltaCm,maxDeltaCm,Math.min(.5,options.positionStepCm??.5),[0,centerDeltaCm]));
  if(!deltas.length) return fallback('指定范围中没有整盒位于梁内的同步位置。');
  const maxEvaluations=Math.max(1,Math.floor(options.maxEvaluations??250_000)),maxMs=Math.max(1,options.maxMs??20_000);
  const deadline=started+maxMs,coarseDeadline=deadline-Math.min(7000,maxMs*.35),refineDeadline=deadline-Math.min(1000,maxMs*.05);
  const finalReserve=Math.min(deltas.length,Math.max(0,maxEvaluations-1));
  const coarseEvaluations=Math.max(1,maxEvaluations-finalReserve-Math.min(10_000,Math.floor(maxEvaluations*.25)));
  const STOP=Symbol('budget');
  const budget=(phase:'coarse'|'refine'|'final')=>{
    checkCancelled();
    const evaluationLimit=phase==='coarse'?coarseEvaluations:phase==='refine'?Math.max(1,maxEvaluations-finalReserve):maxEvaluations;
    const timeLimit=phase==='coarse'?coarseDeadline:phase==='refine'?refineDeadline:deadline;
    if(evaluations>=evaluationLimit||performance.now()>=timeLimit)throw STOP;
  };
  const consider=(plan:Plan)=>{if(better(plan,best))best=plan;};
  let mainLimited=false,positionScanComplete=false;
  const seeds:Plan[][]=[];
  const seedCount=Math.max(1,Math.floor(options.seedCount??2));
  try {
    // Every potentially better position gets its own mirrored angle grid;
    // positions farther than a protected pose are provably dominated.
    for(let position=0;position<deltas.length;position++) {
      const delta=deltas[position];
      // With center distance as the first objective, a farther position cannot
      // beat an already protected pose; this is an exact position prune.
      if(best && centerDistance(delta)>best.centerDistanceCm+EPS)break;
      const positionSeeds:Plan[]=[];
      for(const angle of angles) {
        budget('coarse');const plan=score(delta,candidate(0,delta,angle),candidate(1,delta,mirror(angle)));
        consider(plan);positionSeeds.push(plan);positionSeeds.sort((a,b)=>seedBetter(a,b)?-1:seedBetter(b,a)?1:0);positionSeeds.length=Math.min(seedCount,positionSeeds.length);
        await checkpoint(32);
      }
      seeds.push(positionSeeds);report(.65*(position+1)/deltas.length);
    }
    positionScanComplete=true;
  } catch(error){if(error!==STOP)throw error;mainLimited=true;}
  if(options.refinement!==false) {
    const levels=options.refinementLevels??[{positionStepCm:.5,positionRadiusCm:0,angleStep:5,angleRadius:10},{positionStepCm:.5,positionRadiusCm:0,angleStep:1,angleRadius:5}];
    try {
      // Refine the nearest-to-center feasible and infeasible seeds first. Once
      // a more central protected pose exists, farther poses cannot improve it.
      for(let position=0;position<seeds.length;position++) {
        if(best && centerDistance(deltas[position])>best.centerDistanceCm+EPS)break;
        for(const seed of seeds[position]) {
          let local=seed;
          for(const level of levels) for(let pass=0;pass<2;pass++) {
            const old=local,center=local.left;
            const lo=Math.max(yawBounds[0],center.yaw-level.angleRadius),hi=Math.min(yawBounds[1],center.yaw+level.angleRadius);
            const pLo=Math.max(pitchBounds[0],center.pitch-level.angleRadius),pHi=Math.min(pitchBounds[1],center.pitch+level.angleRadius);
            const nearby=lo<=hi&&pLo<=pHi?valuesBetween(lo,hi,level.angleStep,[center.yaw]).flatMap(yaw=>valuesBetween(pLo,pHi,level.angleStep,[center.pitch]).map(pitch=>({yaw,pitch}))):[center];
            for(const angle of nearby) {
              budget('refine');const plan=score(local.deltaCm,candidate(0,local.deltaCm,angle),candidate(1,local.deltaCm,mirror(angle)));
              if(seedBetter(plan,local))local=plan;consider(plan);await checkpoint(32);
            }
            if(local===old)break;
          }
        }
        report(.65+.25*(position+1)/Math.max(1,seeds.length));
      }
    } catch(error){if(error!==STOP)throw error;mainLimited=true;}
  }
  // Spread the newly refined angle across the full grid as an additional
  // check. positionScanComplete includes positions pruned by the first objective.
  const finalAngles=best as Plan|null;
  try {
    for(let index=0;index<deltas.length;index++) {
      const delta=deltas[index];
      if(best && centerDistance(delta)>best.centerDistanceCm+EPS)break;
      budget('final');const angle=finalAngles?.left??angles[0];
      consider(score(delta,candidate(0,delta,angle),candidate(1,delta,mirror(angle))));
      await checkpoint(32);report(.9+.09*(index+1)/deltas.length);
    }
  } catch(error){if(error!==STOP)throw error;mainLimited=true;}
  checkCancelled();
  const chosen=best as Plan|null;
  if(!chosen) {
    report(1);
    return {...fallback(coverageProtection==='overall'?'未找到两盒对齐、角度镜像且整体覆盖不下降的推荐方案。':'未找到两盒对齐、角度镜像且整体、近场及侧后分区覆盖均不下降的推荐方案。'),positionScanComplete,searchLimited:mainLimited||!positionScanComplete};
  }
  const config=clone(configAt(chosen.deltaCm));
  for(let index=0;index<SIDE_IDS.length;index++){const sensor=config.sensors.find(item=>item.id===SIDE_IDS[index])!,angle=index===0?chosen.left:chosen.right;sensor.yaw=angle.yaw;sensor.pitch=angle.pitch;}
  const normalized=engine.normalize(config),finalStats=engine.coverage(normalized),finalNear=computeNearFieldMetrics(normalized,finalStats,rangeMeters);
  const finalSides=SIDE_IDS.map(id=>normalized.sensors.find(sensor=>sensor.id===id)!);
  if(!mirrored(finalSides[0],finalSides[1]) || Math.abs(engine.support(normalized,'camera3').center.x-engine.support(normalized,'camera4').center.x)>EPS) throw new Error('Final side-camera symmetry verification failed.');
  if(finalStats.covered!==chosen.overallCovered||finalStats.covered<baseline.covered||finalNear.covered!==chosen.near.near||NEAR_FIELD_REGIONS.some(key=>finalNear[key].covered!==chosen.near[key])||coverageProtection==='all-regions'&&(finalNear.covered<baselineNear.covered||NEAR_FIELD_REGIONS.some(key=>finalNear[key].covered<baselineNear[key].covered))) throw new Error('Final coverage verification failed.');
  const changedIds:string[]=[],rotatedIds:string[]=[],translatedIds:string[]=[];
  for(let index=0;index<SIDE_IDS.length;index++) {
    const sensor=normalized.sensors.find(item=>item.id===SIDE_IDS[index])!,original=side[index];
    const rotated=Math.abs(sensor.yaw-original.yaw)>EPS||Math.abs(sensor.pitch-original.pitch)>EPS;
    const translated=Math.hypot(sensor.x-original.x,sensor.y-original.y,sensor.z-original.z)>EPS;
    if(rotated)rotatedIds.push(sensor.id);if(translated)translatedIds.push(sensor.id);if(rotated||translated)changedIds.push(sensor.id);
  }
  const improved=!baselineLegal || better(chosen,basePlan);
  const limited=mainLimited||!positionScanComplete;
  const corrections=[!baselineBoxesLegal?'安装盒悬出':null,!baselineAligned?'左右盒前后位置':null,!baselineMirrored?'左右相机镜像角度':null].filter(Boolean).join('、');
  const protectionDescription=coverageProtection==='overall'?'整体覆盖不下降':'整体及各侧后近场分区覆盖均不下降';
  const nearChange=chosen.near.near<basePlan.near.near?`近场覆盖减少${basePlan.near.near-chosen.near.near}格，已按居中优先排序。`:'';
  const reason=!improved?`未找到在覆盖约束内进一步居中或改善覆盖的布局，保留当前参数。${limited?' 预算内搜索尚未完成。':''}`:[
    !baselineLegal?`已纠正${corrections}，两盒已对齐且角度镜像。`:null,
    `已按${protectionDescription}、尽量居中、再优化近场的顺序推荐。`,
    nearChange||null,
    limited?(!positionScanComplete?'预算内尚未完成全部0.5 cm位置的角度搜索。':'角度细化为预算内有限搜索。'):null,
  ].filter(Boolean).join(' ');
  report(1);
  return {status:improved?'improved':'unchanged',reason,coverageProtection,config:improved?normalized:base,before:metrics(basePlan),after:improved?{...metrics(chosen),nearField:finalNear}:metrics(basePlan),movedIds:improved?changedIds:[],changedIds:improved?changedIds:[],rotatedIds:improved?rotatedIds:[],translatedIds:improved?translatedIds:[],evaluations,visibilityEvaluations,commonDeltaCm:improved?chosen.deltaCm:0,commonReferenceXCm,alignmentOffsetsCm,...centerInfo,recommendedBoxCenterXCm:improved?commonReferenceXCm+chosen.deltaCm:commonReferenceXCm,afterCenterDistanceCm:improved?chosen.centerDistanceCm:beforeCenterDistanceCm,movementRangeCm:[minDeltaCm,maxDeltaCm],positionScanComplete,searchLimited:limited};
}
