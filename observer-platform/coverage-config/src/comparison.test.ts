import { describe, expect, it } from 'vitest';
import { computeCoverage } from './geometry';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { supportForSensor, worldPoint } from './mounting';
import { baselineConditionsChanged, baselineUnderCurrentConditions, compareCoverage, computeBaselineCoverage, loadBaseline } from './comparison';

const saved = () => {
  const config = sanitizeConfig(DEFAULT_CONFIG);
  config.supports!.camera1 = { ...config.supports!.camera1, rail: 'x-top-left', t: .3 };
  config.analysis.height = .45;
  return sanitizeConfig(config);
};
const frameOnly = (config: typeof DEFAULT_CONFIG) => { const value = { ...config.chassis }; delete value.cameraPlate; return value; };

describe('coverage comparison baseline with mounting cuboids', () => {
  it('starts from an independent complete layout without sharing supports or sensor fractions', () => {
    const current = saved(), baseline = loadBaseline(null, current);
    expect(baseline).toEqual(current);
    expect(baseline).not.toBe(current);
    baseline.supports!.camera1!.t = .9;
    baseline.sensors[0].inSupport!.x = .8;
    expect(current.supports!.camera1!.t).toBe(.3);
    expect(current.sensors[0].inSupport!.x).toBe(.5);
    expect(DEFAULT_CONFIG.sensors[0].mount).toEqual({ rail: 'y-top-rear', t: .5 });
  });

  it('reloads a complete versioned baseline and sanitizes imported support geometry and fractions', () => {
    const baseline = saved(), current = sanitizeConfig(DEFAULT_CONFIG);
    expect(loadBaseline(JSON.stringify({ version: 1, config: baseline }), current)).toEqual(baseline);
    baseline.supports!.camera1!.t = 99;
    baseline.sensors[0].inSupport!.x = -99;
    const bounded = loadBaseline(JSON.stringify({ version: 1, config: baseline }), current);
    expect(bounded.supports!.camera1!.t).toBe(1);
    expect(bounded.sensors[0].inSupport!.x).toBe(0);
    expect(bounded.sensors[0]).toMatchObject(worldPoint(supportForSensor(bounded, 'camera1'), bounded.sensors[0].inSupport!));
    for (const corrupted of ['{', 'null', '{}', '{"version":2,"config":{}}', '{"version":1,"config":{"sensors":[]}}']) {
      expect(loadBaseline(corrupted, current)).toEqual(current);
    }
  });

  it('uses common current frame and analysis while retaining baseline supports, fractions and optics', () => {
    const baseline = saved(), snapshot = JSON.stringify(baseline);
    const current = sanitizeConfig({ ...baseline,
      analysis: { height: 1.2, width: 4, length: 3, resolution: .2, occlusion: false },
      chassis: { ...baseline.chassis, length: .7, width: .5, height: .3, frameHeight: 1, frameWidth: .4 },
    });
    const comparable = baselineUnderCurrentConditions(baseline, current);
    expect(comparable.analysis).toEqual(current.analysis);
    expect(frameOnly(comparable)).toEqual(frameOnly(current));
    expect(comparable.supports).toEqual(baseline.supports);
    for (const sensor of comparable.sensors) {
      const savedSensor = baseline.sensors.find(item => item.id === sensor.id)!;
      expect(sensor.inSupport).toEqual(savedSensor.inSupport);
      expect(sensor).toMatchObject(worldPoint(supportForSensor(comparable, sensor.id), savedSensor.inSupport!));
      expect(sensor.yaw).toBe(savedSensor.yaw);
      expect(sensor.pitch).toBe(savedSensor.pitch);
    }
    expect(JSON.stringify(baseline)).toBe(snapshot);
    expect(baselineConditionsChanged(baseline, current)).toBe(true);
    expect(baselineConditionsChanged(baseline, sanitizeConfig(baseline))).toBe(false);
    expect(computeBaselineCoverage(baseline, current).total).toBe(computeCoverage(current).total);
  });

  it('preserves saved support offsets, sizes and both independent front points when comparing another layout', () => {
    const baseline = saved(), current = structuredClone(baseline);
    Object.assign(current.supports!.front!, { t: .9, sizeX: .3, sizeZ: .6, offsetX: .17, offsetZ: .3 });
    current.sensors.find(sensor => sensor.id === 'camera2')!.inSupport = { x: .2, y: .8, z: .3 };
    current.sensors.find(sensor => sensor.id === 'p100r')!.inSupport = { x: .9, y: .2, z: .8 };
    const normalized = sanitizeConfig(current), comparable = baselineUnderCurrentConditions(baseline, normalized);
    expect(comparable.supports).toEqual(baseline.supports);
    expect(comparable.sensors).toEqual(baseline.sensors);
    expect(baselineConditionsChanged(baseline, normalized)).toBe(false);
    expect(comparable.sensors.find(sensor => sensor.id === 'camera2')!.x).not.toBe(normalized.sensors.find(sensor => sensor.id === 'camera2')!.x);
  });

  it('recomputes both layouts on changed middle heights without replacing the baseline support setup', () => {
    const baseline = saved(), current = structuredClone(baseline);
    current.chassis.frameMidHeights!.left = .6;
    current.supports!.camera3!.offsetY = .2;
    const normalized = sanitizeConfig(current), comparable = baselineUnderCurrentConditions(baseline, normalized);
    expect(comparable.supports!.camera3).toEqual(baseline.supports!.camera3);
    expect(supportForSensor(comparable, 'camera3').anchor.z).toBe(supportForSensor(normalized, 'camera3').anchor.z);
    expect(comparable.sensors.find(sensor => sensor.id === 'camera3')!.y).toBe(baseline.sensors.find(sensor => sensor.id === 'camera3')!.y);
    expect(comparable.sensors.find(sensor => sensor.id === 'camera3')!.z).toBeCloseTo(baseline.sensors.find(sensor => sensor.id === 'camera3')!.z + .15, 12);
  });

  it('reports signed percentage points and square metres after disabling sensors', () => {
    const baseline = saved(), current = sanitizeConfig(baseline);
    current.sensors.forEach(sensor => { sensor.enabled = false; });
    const before = computeBaselineCoverage(baseline, current), after = computeCoverage(current);
    const comparison = compareCoverage(after, before);
    expect(comparison.coverage!.delta).toBeCloseTo(-before.percent);
    expect(comparison.blind!.delta).toBeCloseTo(before.area - before.blindArea);
    expect(comparison.overlap!.delta).toBeCloseTo(-before.overlap / before.total * 100);
    expect(compareCoverage(before, before).coverage!.delta).toBe(0);
    expect(sanitizeConfig(baseline)).toEqual(baseline);
  });

  it('measures a real position change inside one box and leaves the saved point untouched', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    initial.analysis = { length: 3, width: 2, height: .2, resolution: .05, occlusion: false };
    initial.sensors.forEach(sensor => { sensor.enabled = sensor.id === 'camera1'; });
    Object.assign(initial.supports!.camera1!, { sizeX: .6, sizeY: .6, sizeZ: .6 });
    Object.assign(initial.sensors[0], { inSupport: { x: .9, y: .9, z: .1 }, yaw: 180, pitch: 45, hfov: 70, vfov: 55, maxRange: 2 });
    const baseline = sanitizeConfig(initial), current = structuredClone(baseline);
    current.sensors[0].inSupport = { x: 1, y: .5, z: 1 };
    const normalized = sanitizeConfig(current), comparison = compareCoverage(computeCoverage(normalized), computeBaselineCoverage(baseline, normalized));
    expect(comparison.coverage!.delta).toBeGreaterThan(0);
    expect(comparison.blind!.delta).toBeLessThan(0);
    expect(baseline.sensors[0].inSupport).toEqual({ x: .9, y: .9, z: .1 });
  });

  it('does not count a common analysis or frame change as installation improvement', () => {
    const baseline = saved();
    const current = sanitizeConfig({ ...baseline,
      analysis: { length: 4, width: 3, height: 1.2, resolution: .2, occlusion: false },
      chassis: { ...baseline.chassis, height: .5, frameHeight: .4, frameWidth: .3, length: .7 },
    });
    const comparison = compareCoverage(computeCoverage(current), computeBaselineCoverage(baseline, current));
    expect(comparison.coverage!.delta).toBe(0);
    expect(comparison.blind!.delta).toBe(0);
    expect(comparison.overlap!.delta).toBe(0);
  });

  it('does not produce misleading deltas for a fully excluded area', () => {
    const current = saved();
    current.analysis = { ...current.analysis, length: .5, width: .5 };
    current.chassis = { ...current.chassis, length: 1, width: 1 };
    const empty = computeCoverage(current);
    expect(empty.total).toBe(0);
    expect(compareCoverage(empty, empty)).toEqual({ coverage: null, blind: null, overlap: null });
  });
});
