import { describe, expect, it } from 'vitest';
import { framePlacement, framePlacementFromMount, framePosition, frameRails } from './frame';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { supportForSensor } from './mounting';

describe('frame installation model', () => {
  it('defines sixteen aluminium members and two slots on one mounting plate with a single varying axis', () => {
    const rails = frameRails(DEFAULT_CONFIG.chassis);
    expect(rails).toHaveLength(18);
    expect(new Set(rails.map(rail => rail.id)).size).toBe(18);
    expect(rails.filter(rail => rail.kind === 'profile')).toHaveLength(16);
    expect(rails.filter(rail => rail.kind === 'plate')).toHaveLength(2);
    for (const rail of rails) {
      expect(rail.end[rail.axis]).toBeGreaterThan(rail.start[rail.axis]);
      for (const axis of ['x', 'y', 'z'] as const) if (axis !== rail.axis) expect(rail.end[axis]).toBe(rail.start[axis]);
      expect(framePosition(rail, 0)).toEqual(rail.start);
      expect(framePosition(rail, 1)).toEqual(rail.end);
    }
  });

  it('projects a legacy position to the nearest member instead of into the frame volume', () => {
    const chassis = DEFAULT_CONFIG.chassis;
    const placement = framePlacement({ x: 0, y: .24, z: 1.2 }, chassis);
    expect(placement.mount.rail).toBe('x-top-left');
    expect(placement).toMatchObject({ x: 0, y: .325, z: chassis.height + chassis.frameHeight });
    expect(framePlacementFromMount(placement.mount, chassis)).toEqual(placement);
  });

  it('locks a selected rail at intersections and clips to its endpoints', () => {
    const rail = frameRails(DEFAULT_CONFIG.chassis).find(item => item.id === 'z-front-left')!;
    const below = framePlacement({ x: -9, y: -9, z: -9 }, DEFAULT_CONFIG.chassis, rail.id);
    const above = framePlacement({ x: -9, y: -9, z: 9 }, DEFAULT_CONFIG.chassis, rail.id);
    expect(below).toEqual({ ...rail.start, mount: { rail: rail.id, t: 0 } });
    expect(above).toEqual({ ...rail.end, mount: { rail: rail.id, t: 1 } });
    expect(framePlacement(rail.end, DEFAULT_CONFIG.chassis, rail.id).mount.rail).toBe(rail.id);
  });

  it('validates untrusted mounts and clamps only finite fractions', () => {
    const chassis = DEFAULT_CONFIG.chassis;
    for (const mount of [null, [], {}, { rail: 'missing', t: .5 }, { rail: 'z-front-left', t: NaN }, { rail: 'z-front-left', t: '1' }]) {
      expect(framePlacementFromMount(mount, chassis)).toBeNull();
    }
    expect(framePlacementFromMount({ rail: 'z-front-left', t: 4 }, chassis)?.mount.t).toBe(1);
    expect(framePlacementFromMount({ rail: 'z-front-left', t: -4 }, chassis)?.mount.t).toBe(0);
    const invalid = framePlacement({ x: NaN, y: Infinity, z: -Infinity }, chassis);
    expect([invalid.x, invalid.y, invalid.z].every(Number.isFinite)).toBe(true);
  });

  it('migrates defaults and old imported XYZ positions once and remains idempotent', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    expect(config.sensors.every(sensor => !!sensor.mount)).toBe(true);
    expect(sanitizeConfig(config)).toEqual(config);
    for (const sensor of config.sensors) {
      const box = supportForSensor(config, sensor.id);
      for (const axis of ['x', 'y', 'z'] as const) {
        expect(sensor[axis]).toBeGreaterThanOrEqual(box.min[axis]);
        expect(sensor[axis]).toBeLessThanOrEqual(box.max[axis]);
      }
      const original = DEFAULT_CONFIG.sensors.find(item => item.id === sensor.id)!;
      expect(sensor.yaw).toBe(original.yaw);
      expect(sensor.pitch).toBe(original.pitch);
      expect(sensor.note?.match(/当前坐标来自安装空间盒/g)).toHaveLength(1);
    }
    expect(DEFAULT_CONFIG.sensors[0].x).toBe(-.5);
    expect(DEFAULT_CONFIG.sensors[0].mount).toEqual({ rail: 'y-top-rear', t: .5 });
  });

  it('derives XYZ from a valid mount instead of trusting inconsistent imported coordinates', () => {
    const config = sanitizeConfig({ sensors: [{ id: 'camera1', x: -6, y: 6, z: 4, mount: { rail: 'z-front-left', t: .3 } }] });
    const expected = framePlacementFromMount({ rail: 'z-front-left', t: .3 }, config.chassis)!;
    expect(config.sensors[0].mount).toEqual(expected.mount);
    expect(config.sensors[0].x).toBeCloseTo(expected.x - .06);
    expect(config.sensors[0].y).toBeCloseTo(expected.y);
    expect(config.sensors[0].z).toBeCloseTo(expected.z - .10);
    const invalid = sanitizeConfig({ sensors: [{ id: 'camera1', x: 0, y: .25, z: 1.15, mount: { rail: 'missing', t: .3 } }] });
    expect(invalid.sensors[0].mount?.rail).toBe('x-top-left');
  });

  it('preserves member and fractional placement when frame dimensions change or shrink', () => {
    const config = sanitizeConfig({ sensors: [{ id: 'camera4', mount: { rail: 'z-rear-right', t: .725 } }] });
    const resized = sanitizeConfig({ ...config, chassis: { length: .37, width: .2, height: .05, frameHeight: .1, frameWidth: .123 } });
    const sensor = resized.sensors.find(item => item.id === 'camera4')!;
    expect(sensor.mount).toEqual({ rail: 'z-rear-right', t: .725 });
    expect(sensor.x).toBe(-.37 / 2);
    expect(sensor.y).toBeCloseTo(-.123 / 2 - .06);
    expect(sensor.z).toBeCloseTo(.05 + .1 * .725 + .10);
    expect(sanitizeConfig(resized)).toEqual(resized);
  });
});
