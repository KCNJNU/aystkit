import { Vector3 } from 'three';
import { sanitizeConfig, type Chassis, type Config, type Sensor, type SensorBasis } from './model';

const RAD = Math.PI / 180;
const EPS = 1e-8;

/** Physical mounting basis, independent of a lidar's intrinsic vertical offset. */
export function sensorBasis(sensor: Sensor): SensorBasis {
  const yaw = sensor.yaw * RAD;
  const pitch = sensor.pitch * RAD;
  return {
    forward: new Vector3(Math.cos(pitch) * Math.cos(yaw), Math.cos(pitch) * Math.sin(yaw), -Math.sin(pitch)),
    right: new Vector3(Math.sin(yaw), -Math.cos(yaw), 0),
    up: new Vector3(Math.sin(pitch) * Math.cos(yaw), Math.sin(pitch) * Math.sin(yaw), Math.cos(pitch)),
  };
}

interface PreparedSensor {
  sensor: Sensor;
  basis: SensorBasis;
  halfHorizontal: number;
  lowerVertical: number;
  upperVertical: number;
}

const prepare = (sensor: Sensor): PreparedSensor => ({
  sensor,
  basis: sensorBasis(sensor),
  halfHorizontal: sensor.hfov * RAD / 2,
  lowerVertical: ((sensor.verticalOffset ?? 0) - sensor.vfov / 2) * RAD,
  upperVertical: ((sensor.verticalOffset ?? 0) + sensor.vfov / 2) * RAD,
});

/** Segment vs chassis AABB. Touching only the origin/target is not an occlusion. */
function bodyBlocks(sensor: Sensor, dx: number, dy: number, dz: number, chassis: Chassis): boolean {
  let enter = 0;
  let leave = 1;
  const origins = [sensor.x, sensor.y, sensor.z];
  const deltas = [dx, dy, dz];
  const minimum = [-chassis.length / 2, -chassis.width / 2, 0];
  const maximum = [chassis.length / 2, chassis.width / 2, chassis.height];
  for (let axis = 0; axis < 3; axis += 1) {
    if (Math.abs(deltas[axis]) < EPS) {
      // A ray lying along a face only grazes the box; it does not enter the body.
      if (origins[axis] <= minimum[axis] + EPS || origins[axis] >= maximum[axis] - EPS) return false;
      continue;
    }
    const t1 = (minimum[axis] - origins[axis]) / deltas[axis];
    const t2 = (maximum[axis] - origins[axis]) / deltas[axis];
    enter = Math.max(enter, Math.min(t1, t2));
    leave = Math.min(leave, Math.max(t1, t2));
    if (leave - enter <= EPS) return false;
  }
  return leave > EPS && enter < 1 - EPS && leave - enter > EPS;
}

function contains(prepared: PreparedSensor, point: Vector3, chassis?: Chassis, occlusion = true): boolean {
  const { sensor, basis } = prepared;
  if (!sensor.enabled) return false;
  const dx = point.x - sensor.x;
  const dy = point.y - sensor.y;
  const dz = point.z - sensor.z;
  const distance = Math.hypot(dx, dy, dz);
  if (distance < EPS || distance < sensor.minRange - EPS || distance > sensor.maxRange + EPS) return false;
  const forward = dx * basis.forward.x + dy * basis.forward.y + dz * basis.forward.z;
  const right = dx * basis.right.x + dy * basis.right.y + dz * basis.right.z;
  const up = dx * basis.up.x + dy * basis.up.y + dz * basis.up.z;
  const horizontal = Math.atan2(right, forward);
  if (sensor.hfov < 360 && Math.abs(horizontal) > prepared.halfHorizontal + EPS) return false;
  if (sensor.kind === 'scan') {
    // A LaserScan is an ideal zero-thickness plane, not a narrow 3-D frustum.
    if (Math.abs(up) > EPS) return false;
    return !(occlusion && chassis && bodyBlocks(sensor, dx, dy, dz, chassis));
  }
  // Perspective camera/depth frusta have rectangular image-plane boundaries.
  // A scanning lidar instead has an azimuth/elevation band around its own axis.
  const vertical = sensor.kind === 'lidar'
    ? Math.atan2(up, Math.hypot(forward, right))
    : Math.atan2(up, forward);
  if (sensor.kind !== 'lidar' && forward <= EPS) return false;
  if (vertical < prepared.lowerVertical - EPS || vertical > prepared.upperVertical + EPS) return false;
  return !(occlusion && chassis && bodyBlocks(sensor, dx, dy, dz, chassis));
}

/** Range is 3D Euclidean distance from the optical/scanning origin, in metres. */
export function sensorContains(sensor: Sensor, point: Vector3, chassis?: Chassis, occlusion = true): boolean {
  return contains(prepare(sensor), point, chassis, occlusion);
}

/** Reuse one mounting basis when sampling many points for a single pose. */
export function createSensorCoverageTest(sensor: Sensor, chassis?: Chassis, occlusion = true): (point: Vector3) => boolean {
  const prepared = prepare(sensor);
  return (point) => contains(prepared, point, chassis, occlusion);
}

export interface CoverageResult {
  cells: { x: number; y: number; count: number }[];
  total: number;
  covered: number;
  blind: number;
  overlap: number;
  percent: number;
  blindArea: number;
  area: number;
  perSensor: Record<string, number>;
  step: number;
  cellLength: number;
  cellWidth: number;
}

/**
 * Uniform cell-center sampling of the requested height slice. The chassis XY
 * footprint is excluded at every height; counts and areas are grid estimates.
 * Configuration limits cap the grid at 90,000 cells (300 × 300).
 */
export function computeCoverage(input: Config): CoverageResult {
  const config = sanitizeConfig(input);
  const { analysis, chassis } = config;
  const columns = Math.ceil(analysis.length / analysis.resolution);
  const rows = Math.ceil(analysis.width / analysis.resolution);
  const cellLength = analysis.length / columns;
  const cellWidth = analysis.width / rows;
  const cellArea = cellLength * cellWidth;
  const active = config.sensors.filter((sensor) => sensor.enabled && (
    sensor.kind !== 'scan' || (Math.abs(sensor.pitch * RAD) <= EPS && Math.abs(sensor.z - analysis.height) <= EPS)
  )).map(prepare);
  const perSensor = Object.fromEntries(config.sensors.map((sensor) => [sensor.id, 0]));
  const cells: CoverageResult['cells'] = [];
  let covered = 0;
  let overlap = 0;
  const point = new Vector3(0, 0, analysis.height);
  for (let row = 0; row < rows; row += 1) {
    const y = -analysis.width / 2 + (row + 0.5) * cellWidth;
    for (let column = 0; column < columns; column += 1) {
      const x = -analysis.length / 2 + (column + 0.5) * cellLength;
      if (Math.abs(x) <= chassis.length / 2 && Math.abs(y) <= chassis.width / 2) continue;
      point.set(x, y, analysis.height);
      let count = 0;
      for (const sensor of active) {
        if (contains(sensor, point, chassis, analysis.occlusion)) {
          count += 1;
          perSensor[sensor.sensor.id] += 1;
        }
      }
      cells.push({ x, y, count });
      if (count > 0) covered += 1;
      if (count > 1) overlap += 1;
    }
  }
  const total = cells.length;
  const blind = total - covered;
  return {
    cells, total, covered, blind, overlap,
    percent: total === 0 ? 0 : covered / total * 100,
    blindArea: blind * cellArea,
    area: total * cellArea,
    perSensor,
    step: analysis.resolution,
    cellLength,
    cellWidth,
  };
}
