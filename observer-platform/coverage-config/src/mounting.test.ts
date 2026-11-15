import { describe, expect, it } from 'vitest';
import { CURRENT_LAYOUT_REVISION, DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { Config, SupportId } from './model';
import { cameraPlateGeometry, frameRails } from './frame';
import { moveSensorWithinSupport, moveSupportAlongRail, normalizedPoint, supportBoxes, supportForSensor, supportGeometry, supportIdForSensor, worldPoint } from './mounting';

const sensor = (config: Config, id: string) => config.sensors.find(value => value.id === id)!;
const point = (value: { x: number; y: number; z: number }) => ({ x: value.x, y: value.y, z: value.z });
const closePoint = (actual: { x: number; y: number; z: number }, expected: { x: number; y: number; z: number }) => {
  for (const axis of ['x', 'y', 'z'] as const) expect(actual[axis]).toBeCloseTo(expected[axis], 12);
};
const optics = (config: Config) => config.sensors.map(value => ({ id: value.id, enabled: value.enabled, yaw: value.yaw, pitch: value.pitch, hfov: value.hfov, vfov: value.vfov, minRange: value.minRange, maxRange: value.maxRange, verticalOffset: value.verticalOffset }));

describe('bounded plastic support volumes', () => {
  it('creates seven distinct attached boxes and eight bounded points, with one front box shared', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), boxes = supportBoxes(config);
    expect(CURRENT_LAYOUT_REVISION).toBe(8);
    expect(boxes).toHaveLength(7);
    expect(supportIdForSensor('camera2')).toBe('front');
    expect(supportIdForSensor('p100r')).toBe('front');
    expect(supportIdForSensor('camera3')).toBe('camera3');
    expect(frameRails(config.chassis).filter(rail => rail.kind === 'profile')).toHaveLength(16);
    for (const value of config.sensors) {
      const box = supportForSensor(config, value.id);
      for (const axis of ['x', 'y', 'z'] as const) {
        expect(value.inSupport![axis]).toBeGreaterThanOrEqual(0);
        expect(value.inSupport![axis]).toBeLessThanOrEqual(1);
        expect(value[axis]).toBeGreaterThanOrEqual(box.min[axis]);
        expect(value[axis]).toBeLessThanOrEqual(box.max[axis]);
      }
    }
    expect(sanitizeConfig(config)).toEqual(config);
    expect(sanitizeConfig(JSON.parse(JSON.stringify(config)))).toEqual(config);
  });

  it('migrates old shared plate positions accurately, retaining optical and range settings', () => {
    const legacy = { ...DEFAULT_CONFIG, layoutRevision: 5, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { lateral: .82, width: .36, spacing: .2, sensorHeight: .28, thickness: .021 } },
      sensors: DEFAULT_CONFIG.sensors.map(value => ({ ...value, yaw: 12, pitch: 18, hfov: 72, vfov: value.kind === 'scan' ? 0 : 45, minRange: .15, maxRange: 2.3, enabled: false })) };
    const original = structuredClone(legacy), config = sanitizeConfig(legacy), old = cameraPlateGeometry(config.chassis);
    closePoint(sensor(config, 'camera2'), old.camera2);
    closePoint(sensor(config, 'p100r'), old.p100r);
    expect(config.supports!.front).toMatchObject({ t: .82, sizeY: .36, thickness: .021, rail: 'y-mid-front' });
    for (const value of config.sensors) expect(value).toMatchObject({ yaw: 12, pitch: 18, hfov: 72, minRange: .15, maxRange: 2.3, enabled: false });
    expect(legacy).toEqual(original);
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('uses relative default slots when imported custom front boxes omit point fractions', () => {
    const defaults = sanitizeConfig(DEFAULT_CONFIG);
    const custom = sanitizeConfig({ ...DEFAULT_CONFIG, supports: { front: {
      t: 1, offsetX: .15, offsetZ: .3, sizeX: .2, sizeY: .4, sizeZ: .5,
    } } });
    for (const id of ['camera2', 'p100r']) expect(sensor(custom, id).inSupport).toEqual(sensor(defaults, id).inSupport);
    closePoint(sensor(custom, 'camera2'), { x: .6566666666666666, y: .018333333333333313, z: .9722222222222222 });
    expect(sensor(custom, 'p100r').y).toBeGreaterThan(sensor(custom, 'camera2').y);
    expect(sanitizeConfig(custom)).toEqual(custom);
  });

  it('moves old upper side mounts to middle members once, retaining along-member fractions', () => {
    const config = sanitizeConfig({ layoutRevision: 5, sensors: [
      { id: 'camera3', mount: { rail: 'x-top-left', t: .23 } },
      { id: 'camera4', mount: { rail: 'x-top-right', t: .81 } },
    ] });
    expect(config.supports!.camera3).toMatchObject({ rail: 'x-mid-left', t: .23 });
    expect(config.supports!.camera4).toMatchObject({ rail: 'x-mid-right', t: .81 });
    closePoint(sensor(config, 'camera3'), { x: -.27, y: .385, z: .8 });
    closePoint(sensor(config, 'camera4'), { x: .31, y: -.385, z: .8 });
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('gives MID360 an inward camera-style bracket above its beam', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), box = supportGeometry(config, 'mid360');
    expect(box.layout.style).toBe('bracket');
    closePoint(box.anchor, { x: .5, y: 0, z: 1.15 });
    closePoint(box.center, { x: .41, y: 0, z: 1.28 });
    closePoint(sensor(config, 'mid360'), { x: .41, y: 0, z: 1.325 });
    expect(sensor(config, 'mid360')).toMatchObject({ yaw: 0, pitch: 29, hfov: 360, vfov: 59, minRange: .2, maxRange: 3 });
  });

  it('allows all three independent coordinates and clips to exact box faces', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG), box = supportForSensor(initial, 'camera3');
    const inside = { x: box.min.x + .03, y: box.max.y - .01, z: box.min.z + .04 };
    const moved = sanitizeConfig(moveSensorWithinSupport(initial, 'camera3', inside));
    closePoint(sensor(moved, 'camera3'), inside);
    expect(sensor(moved, 'camera3').inSupport).not.toEqual(sensor(initial, 'camera3').inSupport);
    const boundary = sanitizeConfig(moveSensorWithinSupport(initial, 'camera3', { x: 9, y: -9, z: 9 }));
    expect(point(sensor(boundary, 'camera3'))).toEqual({ x: box.max.x, y: box.min.y, z: box.max.z });
    expect(sensor(boundary, 'camera3').inSupport).toEqual({ x: 1, y: 0, z: 1 });
    expect(optics(moved)).toEqual(optics(initial));
    expect(moved.supports).toEqual(initial.supports);
  });

  it('keeps the other front camera fixed when one point moves freely inside their shared box', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG), box = supportGeometry(initial, 'front');
    const moved = sanitizeConfig(moveSensorWithinSupport(initial, 'camera2', box.max));
    expect(point(sensor(moved, 'camera2'))).toEqual(box.max);
    expect(sensor(moved, 'p100r')).toEqual(sensor(initial, 'p100r'));
    expect(moved.supports).toEqual(initial.supports);
    for (const value of initial.sensors.filter(value => value.id !== 'camera2')) expect(sensor(moved, value.id)).toEqual(value);
  });

  it('moves a shared box along its Y path, carrying both enabled and disabled points rigidly', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    sensor(initial, 'p100r').enabled = false;
    const box = supportGeometry(initial, 'front');
    const moved = sanitizeConfig(moveSupportAlongRail(initial, 'front', box.rail.end));
    expect(moved.supports!.front!.t).toBe(1);
    for (const id of ['camera2', 'p100r']) {
      const before = sensor(initial, id), after = sensor(moved, id);
      expect(after.inSupport).toEqual(before.inSupport);
      expect(after.x).toBe(before.x); expect(after.z).toBe(before.z);
      expect(after.y - before.y).toBeCloseTo(.175);
    }
    for (const value of initial.sensors.filter(value => !['camera2', 'p100r'].includes(value.id))) expect(sensor(moved, value.id)).toEqual(value);
    expect(optics(moved)).toEqual(optics(initial));
    const full = sanitizeConfig({ ...initial, supports: { ...initial.supports, front: { ...initial.supports!.front, sizeY: initial.chassis.frameWidth } } });
    expect(supportGeometry(full, 'front').rail.start).toEqual(supportGeometry(full, 'front').rail.end);
    expect(sanitizeConfig(moveSupportAlongRail(full, 'front', { x: 99, y: 99, z: 99 })).supports!.front!.t).toBe(.5);
  });

  it('tracks each independently adjustable middle member without changing offsets or local positions', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    const fixtures: [SupportId, 'front' | 'rear' | 'left' | 'right'][] = [['front', 'front'], ['stl19p', 'rear'], ['camera3', 'left'], ['camera4', 'right']];
    for (const [key, side] of fixtures) {
      const after = sanitizeConfig({ ...initial, chassis: { ...initial.chassis, frameMidHeights: { ...initial.chassis.frameMidHeights, [side]: .61 } } });
      expect(after.supports).toEqual(initial.supports);
      const beforeBox = supportGeometry(initial, key), afterBox = supportGeometry(after, key);
      expect(afterBox.center.z - beforeBox.center.z).toBeCloseTo(.16);
      for (const value of initial.sensors) {
        const expectedDelta = supportIdForSensor(value.id) === key ? .16 : 0;
        expect(sensor(after, value.id).z - value.z).toBeCloseTo(expectedDelta);
        expect(sensor(after, value.id).inSupport).toEqual(value.inSupport);
      }
    }
  });

  it('retains normalized XYZ on size changes and derives finite contained positions', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    const moved = sanitizeConfig({ ...initial, sensors: initial.sensors.map(value => value.id === 'camera1' ? { ...value, inSupport: { x: 0, y: .8, z: 1 } } : value) });
    const resized = sanitizeConfig({ ...moved, supports: { ...moved.supports, camera1: { ...moved.supports!.camera1, sizeX: .4, sizeY: .7, sizeZ: .3 } } });
    const box = supportGeometry(resized, 'camera1');
    expect(sensor(resized, 'camera1').inSupport).toEqual({ x: 0, y: .8, z: 1 });
    expect(sensor(resized, 'camera1').x).toBe(box.min.x);
    expect(sensor(resized, 'camera1').z).toBe(box.max.z);
    expect(sensor(resized, 'camera1').y).toBeCloseTo(box.center.y + .3 * .7);
    expect(sanitizeConfig(resized)).toEqual(resized);
  });

  it('rejects malformed imported boxes and normalizes nonfinite points without a free-XYZ bypass', () => {
    const config = sanitizeConfig({ supports: { front: { rail: 'x-top-left', t: 99, sizeY: 99, sizeX: -1, sizeZ: NaN, offsetX: 99, offsetY: -99, offsetZ: '1', thickness: 0, style: 'steel', unknown: 7 },
      camera1: { rail: 'plate-front-depth', sizeX: Infinity } },
      sensors: [{ id: 'camera2', x: 999, y: -999, z: 999, inSupport: { x: 99, y: -99, z: Infinity, unknown: 7 } },
        { id: 'camera3', inSupport: { x: '1', y: NaN, z: null } }] });
    expect(config.supports!.front).toMatchObject({ rail: 'y-mid-front', t: 1, sizeX: .04, sizeY: config.chassis.frameWidth, offsetX: .5, offsetY: -.5, thickness: .001, style: 'bracket' });
    expect(config.supports!.front).not.toHaveProperty('unknown');
    expect(config.supports!.camera1!.rail).toBe('y-top-rear');
    const box = supportGeometry(config, 'front'), front = sensor(config, 'camera2');
    expect(front.inSupport!.x).toBe(1); expect(front.inSupport!.y).toBe(0);
    expect(front.x).toBe(box.max.x); expect(front.y).toBe(box.min.y);
    for (const value of config.sensors) expect(Object.values(value.inSupport!).every(Number.isFinite)).toBe(true);
    expect(sensor(config, 'camera3').inSupport).toEqual({ x: .5, y: .5, z: .5 });
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('preserves current coordinates on nonfinite drag proposals and never mutates its input', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG), saved = structuredClone(initial);
    const after = sanitizeConfig(moveSensorWithinSupport(initial, 'camera1', { x: NaN, y: Infinity, z: -Infinity }));
    closePoint(sensor(after, 'camera1'), sensor(initial, 'camera1'));
    expect(initial).toEqual(saved);
    const box = supportGeometry(initial, 'camera1');
    expect(worldPoint(box, { x: 0, y: 0, z: 0 })).toEqual(box.min);
    expect(worldPoint(box, { x: 1, y: 1, z: 1 })).toEqual(box.max);
    closePoint(worldPoint(box, normalizedPoint(box, box.center)), box.center);
  });
});
