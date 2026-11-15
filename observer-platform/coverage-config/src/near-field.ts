import type { Config } from './model';
import type { CoverageResult } from './geometry';

export const NEAR_FIELD_REGIONS = ['left', 'right', 'leftRear', 'rightRear', 'rear'] as const;
export type NearFieldRegion = typeof NEAR_FIELD_REGIONS[number];
export interface RegionCoverageMetrics { totalCells: number; covered: number; percent: number }
export interface NearFieldMetrics extends RegionCoverageMetrics {
  rangeMeters: number;
  blindArea: number;
  left: RegionCoverageMetrics;
  right: RegionCoverageMetrics;
  leftRear: RegionCoverageMetrics;
  rightRear: RegionCoverageMetrics;
  rear: RegionCoverageMetrics;
}
export interface NearFieldFlags {
  near: boolean;
  left: boolean;
  right: boolean;
  leftRear: boolean;
  rightRear: boolean;
  rear: boolean;
}
const EPS = 1e-8;

/** Horizontal distance from the chassis footprint, excluding its front sector. */
export function nearFieldFlags(config: Config, point: {x:number;y:number}, rangeMeters = 1): NearFieldFlags {
  const halfLength=config.chassis.length/2, halfWidth=config.chassis.width/2;
  const outside=Math.abs(point.x)>halfLength+EPS || Math.abs(point.y)>halfWidth+EPS;
  const distance=Math.hypot(Math.max(0,Math.abs(point.x)-halfLength),Math.max(0,Math.abs(point.y)-halfWidth));
  const near=outside && point.x<=halfLength+EPS && distance<=rangeMeters+EPS;
  const left=near && point.y>=halfWidth-EPS, right=near && point.y<=-halfWidth+EPS;
  return {near,left,right,leftRear:left&&point.x<=EPS,rightRear:right&&point.x<=EPS,rear:near&&point.x<=-halfLength+EPS};
}

/** Recompute the displayed near-field figures from the production coverage grid. */
export function computeNearFieldMetrics(config: Config, coverage: CoverageResult, rangeMeters = 1): NearFieldMetrics {
  rangeMeters=Number.isFinite(rangeMeters)&&rangeMeters>0?rangeMeters:1;
  const keys=['near',...NEAR_FIELD_REGIONS] as const;
  const counts=Object.fromEntries(keys.map(key=>[key,{totalCells:0,covered:0}])) as Record<typeof keys[number],{totalCells:number;covered:number}>;
  for(const cell of coverage.cells) {
    const flags=nearFieldFlags(config,cell,rangeMeters);
    for(const key of keys) if(flags[key]) {
      counts[key].totalCells++;
      if(cell.count>0) counts[key].covered++;
    }
  }
  const region=(key:typeof keys[number]):RegionCoverageMetrics=>({...counts[key],percent:counts[key].totalCells ? counts[key].covered/counts[key].totalCells*100 : 0});
  return {...region('near'),rangeMeters,
    blindArea:(counts.near.totalCells-counts.near.covered)*coverage.cellLength*coverage.cellWidth,
    left:region('left'),right:region('right'),leftRear:region('leftRear'),rightRear:region('rightRear'),rear:region('rear')};
}
