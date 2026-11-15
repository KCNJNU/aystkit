import type { Vector3 } from 'three';
import { cameraPlateGeometry, cameraPlateLateralForY, framePlacement, framePlacementFromMount, frameRails } from './frame';
import type { FrameMount } from './frame';
import { sanitizeSupports, supportForSensor, sanitizeSupportPoint, worldPoint } from './mounting';

/** Vehicle coordinates: X forward, Y left, Z up; angles are degrees. */
export interface Sensor {
  id: string;
  name: string;
  kind: 'camera' | 'lidar' | 'depth' | 'scan';
  color: string;
  enabled: boolean;
  x: number;
  y: number;
  z: number;
  /** Installation member and relative position; XYZ is derived from this mount. */
  mount?: FrameMount;
  /** Normalized XYZ location within the assigned support volume. */
  inSupport?: SupportPoint;
  yaw: number;
  pitch: number;
  hfov: number;
  vfov: number;
  minRange: number;
  maxRange: number;
  aspect?: number;
  /** Intrinsic vertical band center; independent of installation pitch. */
  verticalOffset?: number;
  note?: string;
}

export type FrameSide = 'front' | 'rear' | 'left' | 'right';
export type SupportId = 'front' | 'camera1' | 'camera3' | 'camera4' | 'mid360' | 'bluesea' | 'stl19p';
export interface SupportPoint { x: number; y: number; z: number }
export interface SupportBox {
  rail: string; t: number;
  offsetX: number; offsetY: number; offsetZ: number;
  sizeX: number; sizeY: number; sizeZ: number;
  thickness: number;
  /** Net gap below the member, only for the three under-beam supports. */
  beamGap?: number;
  style: 'bracket' | 'pedestal';
}

export interface CameraPlateLayout {
  lateral: number;
  width: number;
  spacing: number;
  sensorHeight: number;
  thickness: number;
}

export interface Chassis {
  length: number;
  width: number;
  height: number;
  frameHeight: number;
  frameWidth: number;
  /** Independent length of the aluminium frame, rather than a chassis ratio. */
  frameLength?: number;
  /** Legacy common height, used only when a side has no independent height. */
  frameMidHeight?: number;
  /** Independent middle-member heights above the chassis deck. */
  frameMidHeights?: Partial<Record<FrameSide, number>>;
  /** One shared front plate; its two sensor slots translate together along Y. */
  cameraPlate?: Partial<CameraPlateLayout>;
}

export interface Analysis {
  width: number;
  length: number;
  height: number;
  resolution: number;
  occlusion: boolean;
}

export interface Config {
  /** One-time corrections to the known physical installation defaults. */
  layoutRevision?: number;
  sensors: Sensor[];
  supports?: Partial<Record<SupportId, Partial<SupportBox>>>;
  chassis: Chassis;
  analysis: Analysis;
}

export interface SensorBasis {
  forward: Vector3;
  right: Vector3;
  up: Vector3;
}

/**
 * Camera optical FOVs come from the supplied specifications. Installation and
 * analysis defaults carry their provenance on each sensor. Range is an analysis
 * cutoff, never a manufacturer detection guarantee.
 */
export const CURRENT_LAYOUT_REVISION = 8;

export const DEFAULT_CONFIG: Config = {
  layoutRevision: CURRENT_LAYOUT_REVISION,
  sensors: [
    {
      id: 'camera1', name: 'camera1 · 后 · 500万', kind: 'camera', color: '#fb923c', enabled: true,
      x: -0.5, y: 0, z: 0.91, yaw: 180, pitch: 36, hfov: 110, vfov: 90,
      mount: { rail: 'y-top-rear', t: .5 },
      minRange: 0, maxRange: 3, aspect: 4 / 3,
      note: '视野来自 A062 规格书；后视安装参数由现有 ArUco 配置换算至底盘中心。查看距离为分析设定。',
    },
    {
      id: 'camera2', name: 'camera2 · 前 · 500万', kind: 'camera', color: '#4ade80', enabled: true,
      x: 0.5, y: -0.2, z: 0.9, yaw: 0, pitch: 35, hfov: 110, vfov: 90,
      mount: { rail: 'plate-front-right', t: .5 },
      minRange: 0, maxRange: 3, aspect: 4 / 3,
      note: '视野来自 A062 规格书；用户确认前相机与 P100R 共用前侧塑料支架，安装空间内的两台设备可分别调整；支架尺寸和安装位置需按实物校准。',
    },
    {
      id: 'camera3', name: 'camera3 · 左 · 200万', kind: 'camera', color: '#c084fc', enabled: true,
      x: 0, y: 0.325, z: 0.9, yaw: 90, pitch: 35, hfov: 84, vfov: 54,
      mount: { rail: 'x-mid-left', t: .5 },
      minRange: 0, maxRange: 3, aspect: 16 / 9,
      note: '视野来自 A0588 规格书；安装位置与倾角为示意值，需按实物校准。',
    },
    {
      id: 'camera4', name: 'camera4 · 右 · 200万', kind: 'camera', color: '#facc15', enabled: true,
      x: 0, y: -0.325, z: 0.9, yaw: -90, pitch: 35, hfov: 84, vfov: 54,
      mount: { rail: 'x-mid-right', t: .5 },
      minRange: 0, maxRange: 3, aspect: 16 / 9,
      note: '视野来自 A0588 规格书；安装位置与倾角为示意值，需按实物校准。',
    },
    {
      id: 'mid360', name: 'MID360 · 激光雷达', kind: 'lidar', color: '#38bdf8', enabled: true,
      x: 0.4, y: 0, z: 1.15, yaw: 0, pitch: 29, hfov: 360, vfov: 59,
      mount: { rail: 'y-top-front', t: .5 },
      verticalOffset: 22.5, minRange: 0.2, maxRange: 3,
      note: '安装参数来自现有项目；固有垂直视野 −7°～52°，与安装倾角分别计算。距离为分析设定。',
    },
    {
      id: 'p100r', name: 'P100R · 深度相机', kind: 'depth', color: '#22d3ee', enabled: true,
      x: 0.33862331, y: 0.01490957, z: 0.78439228, yaw: -0.69296664, pitch: 25.25519811,
      mount: { rail: 'plate-front-depth', t: .5 },
      hfov: 68.984, vfov: 47.498, minRange: 0.1, maxRange: 3,
      note: '用户确认与前相机并排安装于同一块塑料板。原项目光学标定仅供参考；当前点位由共享安装空间推导。视野取现有软件配置，模型省略约 2.4° 横滚角，距离为分析设定。',
    },
    {
      id: 'bluesea', name: '蓝海 · 二维雷达', kind: 'scan', color: '#f472b6', enabled: false,
      x: .4, y: 0, z: .25, yaw: 0, pitch: 0, hfov: 360, vfov: 0,
      mount: { rail: 'y-bottom-front', t: .5 },
      verticalOffset: 0, minRange: .1, maxRange: 3,
      note: '用户确认安装于底梁、MID360 下方；当前位置随底梁配置，未实测。项目蓝海驱动配置全向扫描、10～4,000 cm 输出范围，不代表检测能力保证；当前距离为分析设定。二维扫描无垂直体积，仅同高水平切片计入面积。',
    },
    {
      id: 'stl19p', name: 'STL-19P · 二维雷达', kind: 'scan', color: '#a3e635', enabled: false,
      x: -.51, y: 0, z: .54, yaw: 90, pitch: 0, hfov: 360, vfov: 0,
      mount: { rail: 'y-mid-rear', t: .5 },
      verticalOffset: 0, minRange: .02, maxRange: 3,
      note: '用户确认位于后相机下方。源码安装参考为底盘中心 X=-51 cm、Z=54 cm，整机倒装；当前挂载点已投影至后中梁，并非该实测位姿。驱动输出范围 2～2,500 cm，不代表检测能力保证；当前距离为分析设定。二维扫描无垂直体积，仅同高水平切片计入面积。',
    },
  ],
  chassis: { length: 1, width: 0.65, height: 0.25, frameHeight: 0.9, frameWidth: 0.65, frameLength: 1, frameMidHeight: .45,
    cameraPlate: { lateral: .5, width: .3, spacing: .16, sensorHeight: 4 / 9, thickness: .008 } },
  analysis: { width: 6, length: 6, height: 0.2, resolution: 0.1, occlusion: true },
};

type UnknownRecord = Record<string, unknown>;
const record = (value: unknown): UnknownRecord =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {};
const bounded = (value: unknown, fallback: number, min: number, max: number): number =>
  Math.max(min, Math.min(max, typeof value === 'number' && Number.isFinite(value) ? value : fallback));
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const near = (value: unknown, target: number): boolean => finite(value) && Math.abs(value - target) <= 1e-6;
const plateSlots = new Set(['plate-front-right', 'plate-front-depth']);

function nearestProfile(position: { x: number; y: number; z: number }, chassis: Chassis) {
  const candidates = frameRails(chassis).filter(rail => rail.kind !== 'plate')
    .map(rail => framePlacement(position, chassis, rail.id));
  const distance = (point: typeof position) => (point.x - position.x) ** 2 + (point.y - position.y) ** 2 + (point.z - position.z) ** 2;
  return candidates.reduce((best, candidate) => distance(candidate) < distance(best) ? candidate : best);
}

/** Limit untrusted configurations and retain the eight known installation points. */
export function sanitizeConfig(input: unknown): Config {
  const source = record(input);
  const chassis = record(source.chassis);
  const analysis = record(source.analysis);
  const candidates = Array.isArray(source.sensors) ? source.sensors.map(record) : [];
  const revision = finite(source.layoutRevision) ? source.layoutRevision : 0;
  // Only revision 3 and earlier used a vertical plate fraction. Revision 4
  // already stored lateral movement and must retain its configured board pose.
  const migrateSharedPlate = revision < 4;
  const migrateOldInstallation = revision < 3;
  const frameHeight = bounded(chassis.frameHeight, DEFAULT_CONFIG.chassis.frameHeight, 0.1, 3);
  const length = bounded(chassis.length, DEFAULT_CONFIG.chassis.length, 0.2, 4);
  const legacyMidHeight = bounded(chassis.frameMidHeight, frameHeight / 2, .05, frameHeight - .05);
  const midHeights = record(chassis.frameMidHeights);
  const frameMidHeights = Object.fromEntries((['front', 'rear', 'left', 'right'] as const).map(side =>
    [side, bounded(midHeights[side], legacyMidHeight, .05, frameHeight - .05)]
  )) as Record<FrameSide, number>;
  const suppliedPlate = record(chassis.cameraPlate);
  const cameraPlate = Object.fromEntries((['lateral', 'width', 'spacing', 'sensorHeight', 'thickness'] as const)
    .map(key => [key, finite(suppliedPlate[key]) ? suppliedPlate[key] : undefined])) as Partial<CameraPlateLayout>;
  const sanitizedChassis: Chassis = {
    length,
    width: bounded(chassis.width, DEFAULT_CONFIG.chassis.width, 0.2, 3),
    height: bounded(chassis.height, DEFAULT_CONFIG.chassis.height, 0.05, 2),
    frameHeight,
    frameWidth: bounded(chassis.frameWidth, DEFAULT_CONFIG.chassis.frameWidth, 0.1, 3),
    frameLength: bounded(chassis.frameLength, length, .2, 4),
    frameMidHeight: legacyMidHeight,
    frameMidHeights,
    cameraPlate,
  };
  const oldCamera = candidates.find(candidate => candidate.id === 'camera2');
  const oldCameraMount = record(oldCamera?.mount);
  const oldCameraDefaultPost = oldCameraMount.rail === 'z-front-right' && near(oldCameraMount.t, (.9 - .25) / .9);
  const oldCameraRawDefault = !oldCameraMount.rail && near(oldCamera?.x, .5) && near(oldCamera?.y, -.2) && near(oldCamera?.z, .9);
  let oldCameraY: number | undefined;
  if (migrateSharedPlate && oldCameraMount.rail === 'plate-front-right') {
    // Revision 3 stored the camera's vertical fraction in mount.t. Revision 4
    // stores only the shared lateral fraction there; preserve the old height.
    if (!finite(suppliedPlate.sensorHeight)) cameraPlate.sensorHeight = bounded(oldCameraMount.t, 4 / 9, 0, 1);
    oldCameraY = -sanitizedChassis.frameWidth * .32;
  } else if (migrateOldInstallation && oldCameraDefaultPost) {
    const previous = framePlacementFromMount(oldCameraMount, sanitizedChassis)!;
    const plate = cameraPlateGeometry(sanitizedChassis), bottom = plate.center.z - plate.height / 2;
    if (!finite(suppliedPlate.sensorHeight)) cameraPlate.sensorHeight = (previous.z - bottom) / plate.height;
    oldCameraY = -sanitizedChassis.frameWidth * .32;
  } else if (migrateSharedPlate && oldCameraRawDefault) {
    const plate = cameraPlateGeometry(sanitizedChassis), bottom = plate.center.z - plate.height / 2;
    if (!finite(suppliedPlate.sensorHeight)) cameraPlate.sensorHeight = (.9 - bottom) / plate.height;
    oldCameraY = -.2;
  }
  if (oldCameraY !== undefined && !finite(suppliedPlate.lateral)) {
    const plate = cameraPlateGeometry(sanitizedChassis);
    cameraPlate.lateral = cameraPlateLateralForY(sanitizedChassis, oldCameraY + plate.spacing / 2);
  }
  const plate = cameraPlateGeometry(sanitizedChassis);
  sanitizedChassis.cameraPlate = { lateral: plate.lateral, width: plate.width, spacing: plate.spacing,
    sensorHeight: plate.sensorHeight, thickness: plate.thickness };
  const result: Config = {
    layoutRevision: CURRENT_LAYOUT_REVISION,
    sensors: DEFAULT_CONFIG.sensors.map((base) => {
      const candidate = candidates.find((candidate) => candidate.id === base.id);
      const value = candidate ?? {};
      const minRange = bounded(value.minRange, base.minRange, 0, 29.99);
      const vfov = base.kind === 'scan' ? 0 : bounded(value.vfov, base.vfov, 1, 179);
      const offsetLimit = 90 - vfov / 2;
      const slot = base.id === 'camera2' ? 'plate-front-right' : base.id === 'p100r' ? 'plate-front-depth' : null;
      const oldMount = record(value.mount);
      let requestedMount: unknown = value.mount;
      if (migrateOldInstallation && base.id === 'bluesea' && oldMount.rail === 'y-mid-front' && near(oldMount.t, .5)) requestedMount = base.mount;
      const defaultMount = candidate ? null : framePlacementFromMount(base.mount, sanitizedChassis);
      // These two optical points are physically fixed to one plate, including
      // imported/custom legacy mounts. Their saved XYZ never moves the board.
      let placement = slot ? framePlacementFromMount({ rail: slot, t: plate.lateral }, sanitizedChassis)!
        : framePlacementFromMount(requestedMount, sanitizedChassis) ?? defaultMount ?? framePlacement({
        x: bounded(value.x, base.x, -6, 6), y: bounded(value.y, base.y, -6, 6),
        z: bounded(value.z, base.z, 0, 5),
      }, sanitizedChassis, base.kind === 'scan' ? base.mount?.rail : undefined);
      if (plateSlots.has(placement.mount.rail)) {
        if (!slot) placement = nearestProfile(placement, sanitizedChassis);
      }
      return {
        ...base,
        note: `${base.note ?? ''} 当前坐标为框架布局模拟值，原安装数据仅供参考。`,
        enabled: typeof value.enabled === 'boolean' ? value.enabled : base.enabled,
        ...placement, yaw: bounded(value.yaw, base.yaw, -180, 180),
        pitch: bounded(value.pitch, base.pitch, -90, 90),
        hfov: bounded(value.hfov, base.hfov, 1, base.kind === 'lidar' || base.kind === 'scan' ? 360 : 179),
        vfov,
        verticalOffset: base.kind === 'lidar'
          ? bounded(value.verticalOffset, base.verticalOffset ?? 0, -offsetLimit, offsetLimit)
          : 0,
        minRange,
        maxRange: Math.max(minRange + 0.01, bounded(value.maxRange, base.maxRange, 0.01, 30)),
      };
    }),
    chassis: sanitizedChassis,
    analysis: {
      width: bounded(analysis.width, DEFAULT_CONFIG.analysis.width, 0.5, 12),
      length: bounded(analysis.length, DEFAULT_CONFIG.analysis.length, 0.5, 12),
      height: bounded(analysis.height, DEFAULT_CONFIG.analysis.height, 0, 4),
      resolution: bounded(analysis.resolution, DEFAULT_CONFIG.analysis.resolution, 0.04, 0.2),
      occlusion: typeof analysis.occlusion === 'boolean' ? analysis.occlusion : DEFAULT_CONFIG.analysis.occlusion,
    },
  };
  // Revision 7 replaces the former MID360 mould with the camera-style bracket.
  // Correct only its former default zero normal offset; custom geometry survives.
  const suppliedSupports = record(source.supports), suppliedMid = record(suppliedSupports.mid360);
  let requestedSupports = source.supports;
  if (revision < 7 && suppliedMid.style === 'pedestal') {
    const migratedMid: Record<string, unknown> = { ...suppliedMid, style: 'bracket' };
    if (near(suppliedMid.offsetX, 0) && (suppliedMid.rail === undefined || suppliedMid.rail === 'y-top-front')) {
      migratedMid.offsetX = -bounded(suppliedMid.sizeX, .18, .04, 1.5) / 2;
    }
    requestedSupports = { ...suppliedSupports, mid360: migratedMid };
  }
  // Revision 8 carries three supports below their member. Missing beamGap is
  // inferred once from an already-below legacy offset, or defaults to 1 cm.
  result.supports = sanitizeSupports(requestedSupports, result);
  result.sensors = result.sensors.map(sensor => {
    const candidate = candidates.find(value => value.id === sensor.id) ?? {};
    const box = supportForSensor(result, sensor.id);
    const frontPoint = sensor.id === 'camera2' || sensor.id === 'p100r';
    // Missing point data uses the original relative slot, so a newly imported
    // box carries its defaults rather than projecting back to an old world pose.
    const fallback: SupportPoint = frontPoint ? {
      x: .5 + plate.thickness / (.12 * 2),
      y: .5 + (sensor.id === 'camera2' ? -1 : 1) * plate.spacing / (plate.width * 2),
      z: plate.sensorHeight,
    } : { x: .5, y: .5, z: sensor.id === 'mid360' ? .75 : .5 };
    const inSupport = sanitizeSupportPoint(candidate.inSupport, fallback);
    const rail = sensor.id === 'camera2' ? 'plate-front-right' : sensor.id === 'p100r' ? 'plate-front-depth' : box.layout.rail;
    return { ...sensor, ...worldPoint(box, inSupport), inSupport,
      mount: { rail, t: box.layout.t },
      note: `${DEFAULT_CONFIG.sensors.find(item => item.id === sensor.id)?.note ?? ''} 塑料支架尺寸与点位为可编辑估计值，非照片实测；当前坐标来自安装空间盒。` };
  });
  return result;
}
