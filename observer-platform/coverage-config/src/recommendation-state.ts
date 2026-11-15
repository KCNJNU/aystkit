import { sanitizeConfig, type Config } from './model';
import { supportGeometry } from './mounting';
import { NEAR_FIELD_REGIONS, type NearFieldMetrics } from './near-field';
import { sideLayoutIsSymmetric } from './side-symmetry';

const SIDE_IDS = ['camera3', 'camera4'] as const;

interface ProtectedMetrics { overallCovered: number; nearField?: NearFieldMetrics }
interface ProtectedRecommendation { before: ProtectedMetrics; after: ProtectedMetrics; coverageProtection?: 'overall' | 'all-regions' }
export const recommendationIsSymmetric = sideLayoutIsSymmetric;

/** Both the preview and apply path enforce the user's coverage protections. */
export function recommendationIsProtected(result: ProtectedRecommendation): boolean {
  const before = result.before.nearField, after = result.after.nearField;
  return Boolean(before && after && result.after.overallCovered >= result.before.overallCovered &&
    after.totalCells === before.totalCells && NEAR_FIELD_REGIONS.every(key => after[key].totalCells === before[key].totalCells) &&
    (result.coverageProtection === 'overall' || after.covered >= before.covered &&
      NEAR_FIELD_REGIONS.every(key => after[key].covered >= before[key].covered)));
}

/** Use the same checks for preview and application of a worker result. */
export function recommendationIsApplicable(result: ProtectedRecommendation & { status: string; config: Config }): boolean {
  return result.status === 'improved' && recommendationIsProtected(result) && recommendationIsSymmetric(result.config) &&
    SIDE_IDS.every(id => {
      const sensor = result.config.sensors.find(item => item.id === id);
      const t = result.config.supports?.[id]?.t;
      if (!sensor?.enabled || typeof t !== 'number' || !Number.isFinite(t) || t < 0 || t > 1 ||
        !Number.isFinite(sensor.yaw) || !Number.isFinite(sensor.pitch) || Math.abs(sensor.yaw) > 180 || Math.abs(sensor.pitch) > 90) return false;
      const box = supportGeometry(result.config, id);
      return box.min.x >= box.rail.start.x - box.layout.offsetX - 1e-8 &&
        box.max.x <= box.rail.end.x - box.layout.offsetX + 1e-8;
    });
}

/** Apply only the two side poses and their shared rail translation. */
export function applySideRecommendation(current: Config, proposed: Config): Config {
  const base = sanitizeConfig(current);
  const supports = { ...base.supports! };
  for (const id of SIDE_IDS) {
    const candidate = proposed.supports?.[id];
    if (!candidate || typeof candidate.t !== 'number' || !Number.isFinite(candidate.t) || candidate.t < 0 || candidate.t > 1 || supportGeometry(base, id).rail.axis !== 'x') {
      throw new RangeError('Invalid side-box recommendation.');
    }
    supports[id] = { ...supportGeometry(base, id).layout, t: candidate.t };
  }
  const sensors = base.sensors.map(sensor => {
    if (sensor.id !== 'camera3' && sensor.id !== 'camera4') return sensor;
    const candidate = proposed.sensors.find(item => item.id === sensor.id);
    if (!candidate || !Number.isFinite(candidate.yaw) || !Number.isFinite(candidate.pitch) ||
      Math.abs(candidate.yaw) > 180 || Math.abs(candidate.pitch) > 90) throw new RangeError('Invalid side-camera angles.');
    return { ...sensor, yaw: candidate.yaw, pitch: candidate.pitch };
  });
  const next = sanitizeConfig({ ...base, supports, sensors });
  for (const id of SIDE_IDS) {
    const box = supportGeometry(next, id);
    const beamStart = box.rail.start.x - box.layout.offsetX;
    const beamEnd = box.rail.end.x - box.layout.offsetX;
    if (box.min.x < beamStart - 1e-8 || box.max.x > beamEnd + 1e-8) {
      throw new RangeError('Recommended box extends beyond the mounting beam.');
    }
  }
  if (!recommendationIsSymmetric(next)) throw new RangeError('Side boxes must have the same X and mirrored camera angles.');
  if (!SIDE_IDS.every(id => next.sensors.find(sensor => sensor.id === id)?.enabled)) throw new RangeError('Both side cameras must be enabled for a symmetric recommendation.');
  return next;
}
