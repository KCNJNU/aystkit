import { cameraPlateGeometry, framePosition, frameRails } from './frame';
import type { FramePosition, FrameRail } from './frame';
import type { Config, SupportBox, SupportId, SupportPoint } from './model';

export const SUPPORT_IDS: SupportId[] = ['front', 'camera1', 'camera3', 'camera4', 'mid360', 'bluesea', 'stl19p'];
export const BEAM_PROFILE_HALF_HEIGHT = .017;
export const SUPPORT_TOP_HARDWARE_ALLOWANCE = .005;
export const DEFAULT_BEAM_GAP = .01;
export const isBelowBeamSupport = (id: string): boolean => id === 'camera1' || id === 'bluesea' || id === 'stl19p';

/** Offset of a hanging box centre; the entire roof and its bolts clear the member. */
export function belowBeamOffset(sizeZ: number, thickness: number, beamGap: number): number {
  return -(BEAM_PROFILE_HALF_HEIGHT + sizeZ / 2 + thickness + SUPPORT_TOP_HARDWARE_ALLOWANCE + beamGap);
}
const bounded = (value: unknown, fallback: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, typeof value === 'number' && Number.isFinite(value) ? value : fallback));
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const axes = ['x', 'y', 'z'] as const;

export interface SupportGeometry {
  id: SupportId;
  layout: SupportBox;
  anchor: FramePosition;
  center: FramePosition;
  min: FramePosition;
  max: FramePosition;
  belowBeam: boolean;
  beamBottomZ: number;
  beamGap: number | null;
  /** Conservative top envelope of the box roof and its fasteners. */
  topEnvelopeZ: number;
  /** World-space path of the box CENTER; anchor remains on its profile. */
  rail: FrameRail;
}

export function supportIdForSensor(sensorId: string): SupportId {
  return sensorId === 'camera2' || sensorId === 'p100r' ? 'front' :
    SUPPORT_IDS.includes(sensorId as SupportId) ? sensorId as SupportId : 'front';
}

/** Editable estimates only. The photograph provides structure, not measurements. */
export function defaultSupports(config: Config): Record<SupportId, SupportBox> {
  const plate = cameraPlateGeometry(config.chassis);
  const generic = (id: Exclude<SupportId, 'front'>, rail: string, size: FramePosition, offset: FramePosition, style: SupportBox['style'] = 'bracket'): SupportBox => {
    const saved = config.sensors.find(sensor => sensor.id === id)?.mount;
    let selected = saved?.rail ?? rail;
    if (id === 'camera3' && selected === 'x-top-left') selected = 'x-mid-left';
    if (id === 'camera4' && selected === 'x-top-right') selected = 'x-mid-right';
    if (!frameRails(config.chassis).some(item => item.id === selected && item.kind === 'profile')) selected = rail;
    const hanging = isBelowBeamSupport(id), thickness = .008;
    return { rail: selected, t: saved?.t ?? .5, sizeX: size.x, sizeY: size.y, sizeZ: size.z,
      offsetX: offset.x, offsetY: offset.y,
      offsetZ: hanging ? belowBeamOffset(size.z, thickness, DEFAULT_BEAM_GAP) : offset.z,
      thickness, style, ...(hanging ? { beamGap: DEFAULT_BEAM_GAP } : {}) };
  };
  return {
    front: { rail: 'y-mid-front', t: plate.lateral, sizeX: .12, sizeY: plate.width, sizeZ: plate.height,
      offsetX: plate.thickness / 2, offsetY: 0, offsetZ: plate.height / 2, thickness: plate.thickness, style: 'bracket' },
    camera1: generic('camera1', 'y-top-rear', { x: .12, y: .18, z: .12 }, { x: -.06, y: 0, z: .06 }),
    camera3: generic('camera3', 'x-mid-left', { x: .18, y: .12, z: .12 }, { x: 0, y: .06, z: .10 }),
    camera4: generic('camera4', 'x-mid-right', { x: .18, y: .12, z: .12 }, { x: 0, y: -.06, z: .10 }),
    mid360: generic('mid360', 'y-top-front', { x: .18, y: .18, z: .18 }, { x: -.09, y: 0, z: .13 }),
    bluesea: generic('bluesea', 'y-bottom-front', { x: .12, y: .16, z: .10 }, { x: .04, y: 0, z: .04 }),
    stl19p: generic('stl19p', 'y-mid-rear', { x: .12, y: .16, z: .10 }, { x: -.04, y: 0, z: .04 }),
  };
}

export function sanitizeSupports(input: unknown, config: Config): Record<SupportId, SupportBox> {
  const raw = record(input), defaults = defaultSupports(config);
  const profiles = frameRails(config.chassis).filter(rail => rail.kind === 'profile');
  return Object.fromEntries(SUPPORT_IDS.map(id => {
    const value = record(raw[id]), fallback = defaults[id];
    // The shared front bracket stays on the front middle member and slides Y.
    const rail = id === 'front' ? 'y-mid-front' : typeof value.rail === 'string' && profiles.some(item => item.id === value.rail) ? value.rail : fallback.rail;
    const sizeX = bounded(value.sizeX, fallback.sizeX, .04, 1.5);
    const sizeY = bounded(value.sizeY, fallback.sizeY, .04, id === 'front' ? config.chassis.frameWidth : 1.5);
    const sizeZ = bounded(value.sizeZ, fallback.sizeZ, .04, 1.5);
    const thickness = bounded(value.thickness, fallback.thickness, .001, Math.min(.1, sizeX, sizeY, sizeZ));
    const hanging = isBelowBeamSupport(id);
    const previousGap = typeof value.offsetZ === 'number' && Number.isFinite(value.offsetZ)
      ? -value.offsetZ - BEAM_PROFILE_HALF_HEIGHT - sizeZ / 2 - thickness - SUPPORT_TOP_HARDWARE_ALLOWANCE
      : -1;
    const gap = bounded(value.beamGap, previousGap >= -1e-9 ? Math.max(0, previousGap) : DEFAULT_BEAM_GAP, 0, .5);
    return [id, { rail, t: bounded(value.t, fallback.t, 0, 1), sizeX, sizeY, sizeZ,
      offsetX: bounded(value.offsetX, fallback.offsetX, -.5, .5),
      offsetY: bounded(value.offsetY, fallback.offsetY, -.5, .5),
      // A hanging centre is derived, never truncated by the legacy ±0.5 m limit.
      offsetZ: hanging ? belowBeamOffset(sizeZ, thickness, gap) : bounded(value.offsetZ, fallback.offsetZ, -.5, .5),
      thickness, ...(hanging ? { beamGap: gap } : {}),
      style: value.style === 'pedestal' || value.style === 'bracket' ? value.style : fallback.style }];
  })) as Record<SupportId, SupportBox>;
}

export function supportGeometry(config: Config, key: SupportId): SupportGeometry {
  const layout = sanitizeSupports(config.supports, config)[key];
  const original = frameRails(config.chassis).find(item => item.id === layout.rail && item.kind === 'profile')!;
  const anchorRail: FrameRail = { ...original, start: { ...original.start }, end: { ...original.end } };
  if (key === 'front') {
    anchorRail.start.y += layout.sizeY / 2;
    anchorRail.end.y -= layout.sizeY / 2;
  }
  const anchor = framePosition(anchorRail, layout.t);
  const offset = { x: layout.offsetX, y: layout.offsetY, z: layout.offsetZ };
  const shifted = (point: FramePosition): FramePosition => ({ x: point.x + offset.x, y: point.y + offset.y, z: point.z + offset.z });
  const center = shifted(anchor);
  const belowBeam = isBelowBeamSupport(key);
  return { id: key, layout, anchor, center,
    belowBeam, beamBottomZ: anchor.z - BEAM_PROFILE_HALF_HEIGHT,
    beamGap: belowBeam ? layout.beamGap ?? DEFAULT_BEAM_GAP : null,
    topEnvelopeZ: center.z + layout.sizeZ / 2 + layout.thickness + SUPPORT_TOP_HARDWARE_ALLOWANCE,
    min: { x: center.x - layout.sizeX / 2, y: center.y - layout.sizeY / 2, z: center.z - layout.sizeZ / 2 },
    max: { x: center.x + layout.sizeX / 2, y: center.y + layout.sizeY / 2, z: center.z + layout.sizeZ / 2 },
    rail: { ...anchorRail, label: original.label, start: shifted(anchorRail.start), end: shifted(anchorRail.end) } };
}

/** The frame-side normal is only used to place structural bracket panels. */
function supportOutwardDirection(box: SupportGeometry): FramePosition {
  const side = box.layout.rail.split('-').at(-1);
  return side === 'front' ? { x: 1, y: 0, z: 0 } : side === 'rear' ? { x: -1, y: 0, z: 0 }
    : side === 'left' ? { x: 0, y: 1, z: 0 } : side === 'right' ? { x: 0, y: -1, z: 0 }
    : Math.abs(box.anchor.x) >= Math.abs(box.anchor.y)
      ? { x: Math.sign(box.anchor.x) || 1, y: 0, z: 0 } : { x: 0, y: Math.sign(box.anchor.y) || 1, z: 0 };
}

/** The support's open face is structural, independent of optical yaw/pitch. */
export function supportOpeningDirection(box: SupportGeometry): FramePosition {
  const outward = supportOutwardDirection(box), sign = box.id === 'mid360' ? -1 : 1;
  return { x: outward.x * sign || 0, y: outward.y * sign || 0, z: 0 };
}

export interface SupportBracketPart { center: FramePosition; size: FramePosition }
export interface SupportClampParts {
  foot: SupportBracketPart;
  fasteners: FramePosition[];
  direction: 1 | -1;
}
export function supportClampParts(data: SupportGeometry): SupportClampParts {
  const { anchor, layout, belowBeam, beamBottomZ } = data, thickness = layout.thickness;
  const footX = Math.min(.10, layout.sizeX), footY = Math.min(.10, layout.sizeY);
  const centerZ = belowBeam ? beamBottomZ - thickness / 2 : anchor.z + .018 + thickness / 2;
  const boltZ = belowBeam ? beamBottomZ - thickness - .002 : anchor.z + .020 + thickness;
  return { foot: { center: { x: anchor.x, y: anchor.y, z: centerZ }, size: { x: footX, y: footY, z: thickness } },
    direction: belowBeam ? -1 : 1,
    fasteners: [-footX * .31, footX * .31].flatMap(dx => [-footY * .31, footY * .31].map(dy =>
      ({ x: anchor.x + dx, y: anchor.y + dy, z: boltZ }))) };
}

export interface SupportBracketParts {
  roof: SupportBracketPart;
  backplate: SupportBracketPart;
  openingCenter: FramePosition;
  arms: { start: FramePosition; end: FramePosition }[];
  fasteners: FramePosition[];
}

/** Camera-style side opening; mounting side determines whether it rises or hangs. */
export function supportBracketParts(data: SupportGeometry): SupportBracketParts {
  const { layout, anchor, center, min, max } = data, thickness = layout.thickness;
  const opening = supportOpeningDirection(data), alongX = opening.y !== 0;
  const halfDepth = (opening.x ? layout.sizeX : layout.sizeY) / 2;
  const side = { x: center.x - opening.x * (halfDepth - thickness / 2),
    y: center.y - opening.y * (halfDepth - thickness / 2) };
  const backHeight = Math.min(layout.sizeZ * .3, .045);
  const backplate: SupportBracketPart = {
    center: { x: side.x, y: side.y, z: max.z - backHeight / 2 },
    size: opening.x ? { x: thickness, y: layout.sizeY, z: backHeight }
      : { x: layout.sizeX, y: thickness, z: backHeight },
  };
  const roof: SupportBracketPart = {
    center: { x: center.x, y: center.y, z: max.z + thickness / 2 },
    size: { x: layout.sizeX, y: layout.sizeY, z: thickness },
  };
  const openingCenter = { ...backplate.center };
  if (opening.x) openingCenter.x = opening.x > 0 ? max.x : min.x;
  if (opening.y) openingCenter.y = opening.y > 0 ? max.y : min.y;
  const clamp = supportClampParts(data), footX = clamp.foot.size.x, footY = clamp.foot.size.y;
  const footZ = data.belowBeam ? clamp.foot.center.z - clamp.foot.size.z / 2 : anchor.z + .018 + thickness;
  const arms = [-1, 1].map(sign => ({
    start: { x: anchor.x + (alongX ? sign * footX * .31 : 0),
      y: anchor.y + (alongX ? 0 : sign * footY * .31), z: footZ },
    end: { x: side.x + (alongX ? sign * layout.sizeX * .40 : 0),
      y: side.y + (alongX ? 0 : sign * layout.sizeY * .40), z: max.z },
  }));
  const fasteners = [-1, 1].map(sign => ({ x: center.x + (alongX ? sign * layout.sizeX * .35 : 0),
    y: center.y + (alongX ? 0 : sign * layout.sizeY * .35), z: max.z + thickness + .001 }));
  return { roof, backplate, openingCenter, arms, fasteners };
}

export function supportBoxes(config: Config): SupportGeometry[] { return SUPPORT_IDS.map(id => supportGeometry(config, id)); }
export function supportForSensor(config: Config, sensorId: string): SupportGeometry { return supportGeometry(config, supportIdForSensor(sensorId)); }

export function sanitizeSupportPoint(input: unknown, fallback: SupportPoint = { x: .5, y: .5, z: .5 }): SupportPoint {
  const raw = record(input);
  return { x: bounded(raw.x, fallback.x, 0, 1), y: bounded(raw.y, fallback.y, 0, 1), z: bounded(raw.z, fallback.z, 0, 1) };
}
export function worldPoint(box: SupportGeometry, local: SupportPoint): FramePosition {
  const point = sanitizeSupportPoint(local);
  const coordinate = (axis: 'x' | 'y' | 'z', size: number) => point[axis] === 0 ? box.min[axis] : point[axis] === 1 ? box.max[axis] : box.center[axis] + (point[axis] - .5) * size;
  return { x: coordinate('x', box.layout.sizeX), y: coordinate('y', box.layout.sizeY), z: coordinate('z', box.layout.sizeZ) };
}
export function normalizedPoint(box: SupportGeometry, world: FramePosition): SupportPoint {
  return sanitizeSupportPoint({ x: (world.x - box.min.x) / box.layout.sizeX,
    y: (world.y - box.min.y) / box.layout.sizeY, z: (world.z - box.min.z) / box.layout.sizeZ });
}

/** Points move freely inside their own cuboid; the support stays attached. */
export function moveSensorWithinSupport(config: Config, id: string, world: FramePosition): Config {
  const box = supportForSensor(config, id);
  return { ...config, sensors: config.sensors.map(sensor => {
    if (sensor.id !== id) return sensor;
    const candidate = Object.fromEntries(axes.map(axis => [axis, Number.isFinite(world[axis]) ? world[axis] : sensor[axis]])) as unknown as FramePosition;
    const inSupport = normalizedPoint(box, candidate);
    return { ...sensor, ...worldPoint(box, inSupport), inSupport };
  }) };
}
/** Only the selected box moves along its member; its sensor fractions survive. */
export function moveSupportAlongRail(config: Config, key: SupportId, center: FramePosition): Config {
  const box = supportGeometry(config, key), axis = box.rail.axis;
  const span = box.rail.end[axis] - box.rail.start[axis];
  const t = span > 1e-12 ? bounded((center[axis] - box.rail.start[axis]) / span, box.layout.t, 0, 1) : .5;
  return { ...config, supports: { ...config.supports, [key]: { ...box.layout, t } } };
}
