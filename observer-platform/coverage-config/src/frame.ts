import type { CameraPlateLayout, Chassis, FrameSide } from './model';

export interface FramePosition { x: number; y: number; z: number }
export interface FrameMount { rail: string; t: number }
export interface FrameRail {
  id: string;
  label: string;
  kind?: 'profile' | 'plate';
  axis: 'x' | 'y' | 'z';
  start: FramePosition;
  end: FramePosition;
}
export interface FramePlacement extends FramePosition { mount: FrameMount }

export interface CameraPlateGeometry extends CameraPlateLayout {
  center: FramePosition;
  height: number;
  minCenterY: number;
  maxCenterY: number;
  camera2: FramePosition;
  p100r: FramePosition;
}

const finiteBound = (value: unknown, fallback: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, typeof value === 'number' && Number.isFinite(value) ? value : fallback));

function middleZ(chassis: Chassis, side: FrameSide): number {
  const fallback = typeof chassis.frameMidHeight === 'number' && Number.isFinite(chassis.frameMidHeight)
    ? chassis.frameMidHeight : chassis.frameHeight / 2;
  return chassis.height + finiteBound(chassis.frameMidHeights?.[side], fallback, .05, chassis.frameHeight - .05);
}

/** Geometry is independent of frameRails so it can supply both shared slots. */
export function cameraPlateGeometry(chassis: Chassis): CameraPlateGeometry {
  const raw = chassis.cameraPlate ?? {};
  const width = finiteBound(raw.width, .3, .04, chassis.frameWidth);
  const spacing = finiteBound(raw.spacing, .16, .02, width - .02);
  const thickness = finiteBound(raw.thickness, .008, .001, .1);
  const lateral = finiteBound(raw.lateral, .5, 0, 1);
  const sensorHeight = finiteBound(raw.sensorHeight, 4 / 9, 0, 1);
  const minCenterY = -chassis.frameWidth / 2 + width / 2;
  const maxCenterY = chassis.frameWidth / 2 - width / 2;
  const frontX = (chassis.frameLength ?? chassis.length) / 2;
  const bottom = middleZ(chassis, 'front'), top = chassis.height + chassis.frameHeight;
  const height = top - bottom;
  const z = bottom + height * sensorHeight;
  const slot = (offset: number): FramePosition => {
    const startY = minCenterY + offset, endY = maxCenterY + offset;
    const y = lateral === 0 ? startY : lateral === 1 ? endY : startY + (endY - startY) * lateral;
    return { x: frontX + thickness, y, z };
  };
  return { width, spacing, thickness, lateral, sensorHeight, height, minCenterY, maxCenterY,
    center: { x: frontX + thickness / 2, y: minCenterY + (maxCenterY - minCenterY) * lateral, z: (bottom + top) / 2 },
    camera2: slot(-spacing / 2), p100r: slot(spacing / 2) };
}

export function cameraPlateLateralForY(chassis: Chassis, centerY: number): number {
  const plate = cameraPlateGeometry(chassis), span = plate.maxCenterY - plate.minCenterY;
  if (span <= 1e-12) return .5;
  return finiteBound(Number.isFinite(centerY) ? (centerY - plate.minCenterY) / span : plate.lateral, plate.lateral, 0, 1);
}

/** Sixteen aluminium members plus two Y-only slots on one shared plastic plate. */
export function frameRails(chassis: Chassis): FrameRail[] {
  const x = (chassis.frameLength ?? chassis.length) / 2, y = chassis.frameWidth / 2;
  const bottom = chassis.height, top = chassis.height + chassis.frameHeight;
  const middle = (side: FrameSide) => middleZ(chassis, side);
  const rails: FrameRail[] = [];
  for (const [level, levelName, z] of [['bottom', '下层', bottom], ['top', '上层', top]] as const) {
    for (const [side, sideName, sideY] of [['left', '左', y], ['right', '右', -y]] as const) {
      rails.push({ id: `x-${level}-${side}`, label: `${levelName}${sideName}纵梁 · 前后`, axis: 'x', start: { x: -x, y: sideY, z }, end: { x, y: sideY, z } });
    }
    for (const [side, sideName, sideX] of [['front', '前', x], ['rear', '后', -x]] as const) {
      rails.push({ id: `y-${level}-${side}`, label: `${levelName}${sideName}横梁 · 左右`, axis: 'y', start: { x: sideX, y: -y, z }, end: { x: sideX, y, z } });
    }
  }
  for (const [frontBack, frontBackName, sideX] of [['front', '前', x], ['rear', '后', -x]] as const) {
    for (const [leftRight, leftRightName, sideY] of [['left', '左', y], ['right', '右', -y]] as const) {
      rails.push({ id: `z-${frontBack}-${leftRight}`, label: `${frontBackName}${leftRightName}立柱 · 高度`, axis: 'z', start: { x: sideX, y: sideY, z: bottom }, end: { x: sideX, y: sideY, z: top } });
    }
  }
  // Keep the original twelve IDs and ordering stable for stored mounts.
  for (const [side, sideName, sideY] of [['left', '左', y], ['right', '右', -y]] as const) {
    const z = middle(side);
    rails.push({ id: `x-mid-${side}`, label: `中层${sideName}纵梁 · 前后`, axis: 'x', start: { x: -x, y: sideY, z }, end: { x, y: sideY, z } });
  }
  for (const [side, sideName, sideX] of [['front', '前', x], ['rear', '后', -x]] as const) {
    const z = middle(side);
    rails.push({ id: `y-mid-${side}`, label: `中层${sideName}横梁 · 左右`, axis: 'y', start: { x: sideX, y: -y, z }, end: { x: sideX, y, z } });
  }
  rails.forEach(rail => { rail.kind = 'profile'; });
  const plate = cameraPlateGeometry(chassis);
  for (const [id, name, position, offset] of [
    ['plate-front-right', '前相机共享塑料板 · 左右', plate.camera2, -plate.spacing / 2],
    ['plate-front-depth', '深度相机共享塑料板 · 左右', plate.p100r, plate.spacing / 2],
  ] as const) {
    rails.push({ id, label: name, kind: 'plate', axis: 'y',
      start: { ...position, y: plate.minCenterY + offset }, end: { ...position, y: plate.maxCenterY + offset } });
  }
  return rails;
}

const boundedT = (t: number): number => Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;

/** Preserve the fixed axes exactly, including non-centimetre frame dimensions. */
export function framePosition(rail: FrameRail, t: number): FramePosition {
  const result = { ...rail.start }, fraction = boundedT(t);
  result[rail.axis] = fraction === 1 ? rail.end[rail.axis]
    : fraction === 0 ? rail.start[rail.axis]
      : rail.start[rail.axis] + (rail.end[rail.axis] - rail.start[rail.axis]) * fraction;
  return result;
}

/** A stored mount is authoritative; its fraction survives changes to frame dimensions. */
export function framePlacementFromMount(mount: unknown, chassis: Chassis): FramePlacement | null {
  if (!mount || typeof mount !== 'object' || Array.isArray(mount)) return null;
  const value = mount as Record<string, unknown>;
  if (typeof value.rail !== 'string' || typeof value.t !== 'number' || !Number.isFinite(value.t)) return null;
  const rail = frameRails(chassis).find(item => item.id === value.rail);
  if (!rail) return null;
  const t = boundedT(value.t);
  return { ...framePosition(rail, t), mount: { rail: rail.id, t } };
}

/** Project to a selected member, or migrate a legacy XYZ position to the nearest member. */
export function framePlacement(position: FramePosition, chassis: Chassis, railId?: string): FramePlacement {
  const rails = frameRails(chassis), selected = rails.find(item => item.id === railId);
  const candidates = selected ? [selected] : rails;
  let nearest: FramePlacement | undefined, minimum = Infinity;
  for (const rail of candidates) {
    const value = position[rail.axis], start = rail.start[rail.axis], length = rail.end[rail.axis] - start;
    const t = length > 1e-12 ? boundedT(Number.isFinite(value) ? (value - start) / length : 0)
      : rail.kind === 'plate' ? cameraPlateGeometry(chassis).lateral : 0;
    const point = framePosition(rail, t);
    const distance = (['x', 'y', 'z'] as const).reduce((sum, axis) => {
      const delta = Number.isFinite(position[axis]) ? position[axis] - point[axis] : 0;
      return sum + delta * delta;
    }, 0);
    // Stable order resolves an exact corner tie deterministically.
    if (!nearest || distance < minimum) { minimum = distance; nearest = { ...point, mount: { rail: rail.id, t } }; }
  }
  return nearest!;
}
