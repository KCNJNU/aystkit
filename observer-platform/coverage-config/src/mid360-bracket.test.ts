import { describe, expect, it } from 'vitest';
import { CURRENT_LAYOUT_REVISION, DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { Config } from './model';
import { moveSensorWithinSupport, supportForSensor, supportGeometry, supportOpeningDirection } from './mounting';
import { loadBaseline } from './comparison';

const lidar = (config: Config) => config.sensors.find(sensor => sensor.id === 'mid360')!;
const legacy = (): Config => {
  const config = sanitizeConfig(DEFAULT_CONFIG);
  return { ...config, layoutRevision: 6, supports: { ...config.supports, mid360: {
    ...config.supports!.mid360!, style: 'pedestal', offsetX: 0,
  } } };
};

describe('MID360 camera-style inward mounting box', () => {
  it('uses the same bracket style as the cameras and opens inward from the front beam', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), box = supportGeometry(config, 'mid360');
    expect(box.layout.style).toBe(supportGeometry(config, 'camera1').layout.style);
    expect(box.layout.style).toBe('bracket');
    expect(supportOpeningDirection(box)).toEqual({ x: -1, y: 0, z: 0 });
    expect(box.max.x).toBeCloseTo(box.anchor.x, 12);
    expect(box.center.x).toBeLessThan(box.anchor.x);
    expect(box.center.z).toBeGreaterThan(box.anchor.z);
    expect(lidar(config)).toMatchObject({ yaw: 0, pitch: 29, hfov: 360, vfov: 59 });
  });

  it('migrates revision-6 default pedestal once while retaining local point and optics', () => {
    const old = legacy();
    old.sensors = old.sensors.map(sensor => sensor.id === 'mid360' ? {
      ...sensor, inSupport: { x: .1, y: .8, z: .62 }, yaw: 17.25, pitch: 22.5,
      hfov: 355, vfov: 53, minRange: .12, maxRange: 4.21,
    } : sensor);
    const saved = structuredClone(old), config = sanitizeConfig(old);
    expect(old).toEqual(saved);
    expect(config.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(config.supports!.mid360).toMatchObject({ style: 'bracket', offsetX: -.09 });
    expect(lidar(config)).toMatchObject({ inSupport: { x: .1, y: .8, z: .62 }, yaw: 17.25,
      pitch: 22.5, hfov: 355, vfov: 53, minRange: .12, maxRange: 4.21 });
    expect(sanitizeConfig(config)).toEqual(config);
    expect(loadBaseline(JSON.stringify({ version: 1, config: old }), config)).toEqual(config);
    for (const sensor of old.sensors.filter(sensor => sensor.id !== 'mid360')) {
      expect(config.sensors.find(item => item.id === sensor.id)).toEqual(sensor);
    }
  });

  it('retains custom box dimensions, nonzero offsets, thickness and along-beam position', () => {
    const old = legacy();
    old.supports!.mid360 = { ...old.supports!.mid360, t: .84, sizeX: .34, sizeY: .22, sizeZ: .28,
      offsetX: .137, offsetY: -.071, offsetZ: .213, thickness: .017 };
    const config = sanitizeConfig(old);
    expect(config.supports!.mid360).toEqual({ ...old.supports!.mid360, style: 'bracket' });
    expect(lidar(config).inSupport).toEqual(lidar(old).inSupport);
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('respects already chosen bracket offsets and current-revision user pedestal selection', () => {
    const old = legacy();
    old.supports!.mid360!.style = 'bracket';
    expect(sanitizeConfig(old).supports!.mid360!.offsetX).toBe(0);
    const current = legacy();
    current.layoutRevision = CURRENT_LAYOUT_REVISION;
    const config = sanitizeConfig(current);
    expect(config.supports!.mid360).toMatchObject({ style: 'pedestal', offsetX: 0 });
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('keeps inward structure independent of sensor yaw and leaves box dragging bounded', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    for (const [rail, normal] of [
      ['y-top-front', { x: -1, y: 0, z: 0 }], ['y-top-rear', { x: 1, y: 0, z: 0 }],
      ['x-mid-left', { x: 0, y: -1, z: 0 }], ['x-mid-right', { x: 0, y: 1, z: 0 }],
    ] as const) {
      const changed = sanitizeConfig({ ...config, supports: { ...config.supports, mid360: { ...config.supports!.mid360, rail } },
        sensors: config.sensors.map(sensor => sensor.id === 'mid360' ? { ...sensor, yaw: -87.4 } : sensor) });
      const box = supportForSensor(changed, 'mid360');
      expect(supportOpeningDirection(box)).toEqual(normal);
      const moved = sanitizeConfig(moveSensorWithinSupport(changed, 'mid360', { x: 100, y: -100, z: 100 }));
      expect(lidar(moved)).toMatchObject({ x: box.max.x, y: box.min.y, z: box.max.z, yaw: -87.4 });
    }
  });
});
