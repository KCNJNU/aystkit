import { supportGeometry } from './mounting';
import { computeCoverage, createSensorCoverageTest } from './geometry';
import { sanitizeConfig, type Config } from './model';
import { optimizeSideCameras } from './side-optimizer-core';
import type { SideOptimizerEngine, SideOptimizerOptions } from './side-optimizer-core';
import { computeNearFieldMetrics } from './near-field';
import type { NearFieldMetrics } from './near-field';
import type { SideAlignmentOffsets } from './side-symmetry';

export type OptimizeCameraOptions = SideOptimizerOptions;
export interface RecommendationMetrics {
  cameraPercent: number;
  overallPercent: number;
  cameraBlindArea: number;
  overallBlindArea: number;
  cameraCovered: number;
  overallCovered: number;
  totalCells: number;
  nearField?: NearFieldMetrics;
}
export interface RecommendationResult {
  status: 'improved' | 'no-improvement' | 'no-cameras' | 'no-area';
  config: Config;
  before: RecommendationMetrics;
  after: RecommendationMetrics;
  /** Includes rotations so an angle-only recommendation remains applicable. */
  movedIds: string[];
  rotatedIds?: string[];
  translatedIds?: string[];
  changedIds?: string[];
  commonDeltaCm?: number;
  alignmentOffsetsCm?: SideAlignmentOffsets;
  commonReferenceXCm?: number;
  beamMidpointXCm?: number;
  recommendedBoxCenterXCm?: number;
  beforeCenterDistanceCm?: number;
  afterCenterDistanceCm?: number;
  coverageProtection?: 'overall' | 'all-regions';
  movementRangeCm?: [number, number];
  reason?: string;
  evaluations: number;
  elapsedMs: number;
  positionScanComplete?: boolean;
  searchLimited?: boolean;
}
const metrics = (config: Config, rangeMeters = 1): RecommendationMetrics => {
  const overall = computeCoverage(config);
  const cameras = computeCoverage({...config, sensors:config.sensors.map(sensor => ({...sensor,
    enabled:sensor.enabled && (sensor.kind === 'camera' || sensor.kind === 'depth'),
  }))});
  return {
    cameraPercent:cameras.percent, overallPercent:overall.percent,
    cameraBlindArea:cameras.blindArea, overallBlindArea:overall.blindArea,
    cameraCovered:cameras.covered, overallCovered:overall.covered, totalCells:overall.total,
    nearField:computeNearFieldMetrics(config,overall,rangeMeters),
  };
};
/**
 * Only side cameras: align box X, translate the aligned pair, mirror yaw/pitch.
 * Other installation parameters stay fixed. Finite candidates are checked
 * against the production grid, without claiming a continuous global optimum.
 */
export async function optimizeCameraPositions(input: Config, options: OptimizeCameraOptions = {}): Promise<RecommendationResult> {
  const started = performance.now();
  const config = sanitizeConfig(input);
  const engine: SideOptimizerEngine = {
    normalize:sanitizeConfig,
    coverage:computeCoverage,
    support:supportGeometry,
    prepareSensor:sensor => createSensorCoverageTest(sensor,config.chassis,config.analysis.occlusion),
    visible:(prepared,point) => prepared(point),
  };
  const recommendation = await optimizeSideCameras(config,engine,options);
  return {
    ...recommendation,
    status:recommendation.status === 'unchanged' ? 'no-improvement' : recommendation.status,
    before:metrics(config,options.nearFieldRangeMeters), after:metrics(recommendation.config,options.nearFieldRangeMeters), elapsedMs:performance.now()-started,
  };
}
