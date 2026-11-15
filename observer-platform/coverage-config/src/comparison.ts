import { computeCoverage } from './geometry';
import type { CoverageResult } from './geometry';
import { sanitizeConfig } from './model';
import type { Config } from './model';

export const BASELINE_STORAGE_KEY = 'observer.sensor-coverage.baseline.v1';

/** A stored baseline has its own version; legacy current-config storage is untouched. */
export function loadBaseline(serialized: string | null, current: Config): Config {
  try {
    const value: unknown = JSON.parse(serialized || 'null');
    if (value && typeof value === 'object' && 'version' in value && value.version === 1 && 'config' in value) {
      const config = value.config;
      if (config && typeof config === 'object' && 'sensors' in config && Array.isArray(config.sensors)
          && 'analysis' in config && 'chassis' in config) return sanitizeConfig(config);
    }
  } catch { /* Invalid browser storage falls back to this visit's current layout. */ }
  return sanitizeConfig(current);
}

/** Recompute both layouts on the current frame, preserving the baseline supports and box-local points. */
export function baselineUnderCurrentConditions(baseline: Config, current: Config): Config {
  return sanitizeConfig({ ...baseline, analysis: current.analysis, supports: baseline.supports,
    chassis: { ...current.chassis, cameraPlate: baseline.chassis.cameraPlate } });
}

export function baselineConditionsChanged(baseline: Config, current: Config): boolean {
  const frame = (chassis: Config['chassis']) => { const value = { ...chassis }; delete value.cameraPlate; return value; };
  return JSON.stringify(baseline.analysis) !== JSON.stringify(current.analysis)
    || JSON.stringify(frame(baseline.chassis)) !== JSON.stringify(frame(current.chassis));
}

export interface ComparisonValue { baseline: number; current: number; delta: number }
export interface CoverageComparison {
  coverage: ComparisonValue | null;
  blind: ComparisonValue | null;
  overlap: ComparisonValue | null;
}

/** No percentage or delta is meaningful if the chassis occupies the entire analysis area. */
export function compareCoverage(current: CoverageResult, baseline: CoverageResult): CoverageComparison {
  if (current.total === 0 || baseline.total === 0) return { coverage: null, blind: null, overlap: null };
  const value = (before: number, after: number): ComparisonValue => ({ baseline: before, current: after, delta: after - before });
  return {
    coverage: value(baseline.percent, current.percent),
    blind: value(baseline.blindArea, current.blindArea),
    overlap: value(baseline.overlap / baseline.total * 100, current.overlap / current.total * 100),
  };
}

export function computeBaselineCoverage(baseline: Config, current: Config): CoverageResult {
  return computeCoverage(baselineUnderCurrentConditions(baseline, current));
}
