import { describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { frameRails } from './frame';
import { supportForSensor } from './mounting';
import { computeCoverage } from './geometry';
import { compareCoverage, computeBaselineCoverage, loadBaseline } from './comparison';

describe('photo frame and two additional installation points', () => {
  it('keeps the original twelve persisted rail IDs before the four middle members', () => {
    const rails = frameRails(DEFAULT_CONFIG.chassis);
    expect(rails.slice(0, 12).map(rail => rail.id)).toEqual([
      'x-bottom-left', 'x-bottom-right', 'y-bottom-front', 'y-bottom-rear',
      'x-top-left', 'x-top-right', 'y-top-front', 'y-top-rear',
      'z-front-left', 'z-front-right', 'z-rear-left', 'z-rear-right',
    ]);
    expect(rails.slice(12, 16).map(rail => rail.id)).toEqual(['x-mid-left', 'x-mid-right', 'y-mid-front', 'y-mid-rear']);
    expect(rails[16].id).toBe('plate-front-right');
    expect(rails[17].id).toBe('plate-front-depth');
  });

  it('attaches scanner supports to the confirmed lower members and raises MID360 above its beam', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    expect(config.sensors).toHaveLength(8);
    const find = (id: string) => config.sensors.find(sensor => sensor.id === id)!;
    const mid = find('mid360'), blue = find('bluesea'), rear = find('camera1'), stl = find('stl19p'), depth = find('p100r');
    expect(blue).toMatchObject({ x: .54, y: mid.y, kind: 'scan', enabled: false, vfov: 0 });
    expect(stl).toMatchObject({ x: -.54, y: rear.y, kind: 'scan', enabled: false, vfov: 0 });
    expect(mid.z).toBeCloseTo(config.chassis.height + config.chassis.frameHeight + .175);
    expect(blue.z).toBeLessThan(mid.z);
    expect(stl.z).toBeLessThan(rear.z);
    expect(blue.mount).toEqual({ rail: 'y-bottom-front', t: .5 });
    expect(blue.z).toBeCloseTo(config.chassis.height - .09);
    expect(stl.mount).toEqual({ rail: 'y-mid-rear', t: .5 });
    expect(depth.mount?.rail).toBe('plate-front-depth');
    expect([depth.x, depth.y, depth.z]).not.toEqual([blue.x, blue.y, blue.z]);
    expect(DEFAULT_CONFIG.sensors.find(sensor => sensor.id === 'stl19p')).toMatchObject({ x: -.51, z: .54 });
    expect(blue.note).toContain('未实测');
    expect(stl.note).toContain('并非该实测位姿');
  });

  it('migrates six-sensor storage and a saved baseline without enabling either new scan', () => {
    const legacy = {
      sensors: DEFAULT_CONFIG.sensors.slice(0, 6).map(sensor => ({ ...sensor, mount: { rail: sensor.mount!.rail, t: .31 }, enabled: true })),
      chassis: { length: 1, width: .65, height: .25, frameHeight: .9, frameWidth: .5 },
      analysis: { ...DEFAULT_CONFIG.analysis, height: .7 },
    };
    const config = sanitizeConfig(legacy);
    expect(config.chassis).toMatchObject({ frameLength: 1, frameWidth: .5, frameMidHeight: .45 });
    for (const sensor of config.sensors.slice(0, 6)) {
      const legacyMount = legacy.sensors.find(item => item.id === sensor.id)!.mount;
      // Revision-3 vertical plate fractions migrate into one shared board height.
      expect(sensor.mount).toEqual(sensor.id === 'camera2' || sensor.id === 'p100r'
        ? { ...legacyMount, t: config.chassis.cameraPlate!.lateral } : legacyMount);
    }
    expect(config.sensors.slice(6).every(sensor => sensor.enabled === false)).toBe(true);
    const restored = loadBaseline(JSON.stringify({ version: 1, config: legacy }), config);
    expect(restored).toEqual(config);
    expect(sanitizeConfig(JSON.parse(JSON.stringify(config)))).toEqual(config);
    const comparison = compareCoverage(computeCoverage(config), computeBaselineCoverage(restored, config));
    expect(comparison.coverage!.delta).toBe(0);
    expect(comparison.overlap!.delta).toBe(0);
    expect(comparison.blind!.delta).toBe(0);
  });

  it('forces scan dimensionality and keeps explicit user enablement after import', () => {
    const config = sanitizeConfig({ sensors: [{ id: 'bluesea', kind: 'camera', enabled: true, vfov: 80, verticalOffset: 85, hfov: 900 }] });
    const scan = config.sensors.find(sensor => sensor.id === 'bluesea')!;
    expect(scan).toMatchObject({ kind: 'scan', enabled: true, vfov: 0, verticalOffset: 0, hfov: 360 });
    expect(sanitizeConfig(config)).toEqual(config);
    expect(config.sensors.find(sensor => sensor.id === 'stl19p')!.enabled).toBe(false);
  });

  it('uses an independent frame length and keeps a valid middle layer even on a very short frame', () => {
    expect(sanitizeConfig({ chassis: { frameHeight: .3 } }).chassis.frameMidHeight).toBe(.15);
    for (const frameHeight of [.1, .15, .2, .9]) {
      for (const frameMidHeight of [-10, 100, NaN]) {
        const config = sanitizeConfig({ chassis: { length: 1.8, frameLength: .8, height: .3, frameHeight, frameMidHeight } });
        const rails = frameRails(config.chassis), middle = rails.find(rail => rail.id === 'y-mid-front')!;
        expect(middle.start.x).toBe(.4);
        expect(middle.start.z).toBeGreaterThan(config.chassis.height);
        expect(middle.start.z).toBeLessThan(config.chassis.height + config.chassis.frameHeight);
        expect(config.chassis.frameMidHeight).toBeGreaterThanOrEqual(.05);
        expect(config.chassis.frameMidHeight).toBeLessThanOrEqual(frameHeight - .05 + 1e-12);
      }
    }
  });

  it('retains a middle member and relative position when the frame dimensions change', () => {
    const original = sanitizeConfig({ sensors: [{ id: 'bluesea', mount: { rail: 'x-mid-left', t: .725 } }] });
    const config = sanitizeConfig({ ...original, chassis: { length: 1, width: .65, height: .4, frameLength: 2, frameWidth: .8, frameHeight: 1.3, frameMidHeight: .95 } });
    const sensor = config.sensors.find(item => item.id === 'bluesea')!;
    expect(sensor.mount).toEqual({ rail: 'x-mid-left', t: .725 });
    const anchor = supportForSensor(config, sensor.id).anchor;
    expect(anchor.x).toBeCloseTo(.45, 12);
    expect(anchor.y).toBe(.4);
    expect(anchor.z).toBe(1.35);
    expect(sensor.x).toBeCloseTo(.45 + .04);
    expect(sensor.y).toBe(.4);
    expect(sensor.z).toBeCloseTo(1.35 - .09);
    expect(original.chassis.frameLength).toBe(1);
  });
});
