import { describe, expect, it } from 'vitest';
import { CURRENT_LAYOUT_REVISION, DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { Config } from './model';
import { cameraPlateGeometry, cameraPlateLateralForY, framePlacement, framePlacementFromMount, frameRails } from './frame';
import { baselineConditionsChanged, baselineUnderCurrentConditions, compareCoverage, computeBaselineCoverage, loadBaseline } from './comparison';
import { computeCoverage } from './geometry';

const find = (config: Config, id: string) => config.sensors.find(sensor => sensor.id === id)!;
const defaultConfig = () => sanitizeConfig(DEFAULT_CONFIG);

describe('one shared laterally movable front camera plate', () => {
  it('derives both optical points from the plate size, spacing and relative sensor height', () => {
    const config = defaultConfig(), geometry = cameraPlateGeometry(config.chassis);
    const front = find(config, 'camera2'), depth = find(config, 'p100r');
    expect(front.name).toBe('camera2 · 前 · 500万');
    expect(front.mount).toEqual({ rail: 'plate-front-right', t: .5 });
    expect(depth.mount).toEqual({ rail: 'plate-front-depth', t: .5 });
    for (const axis of ['x', 'y', 'z'] as const) {
      expect(front[axis]).toBeCloseTo(geometry.camera2[axis], 12);
      expect(depth[axis]).toBeCloseTo(geometry.p100r[axis], 12);
    }
    expect(geometry.center.x - geometry.thickness / 2).toBeCloseTo(.5);
    expect(front.x).toBeCloseTo(.5 + .008);
    expect(depth.x).toBe(front.x);
    expect(front.z).toBeCloseTo(.9);
    expect(depth.z).toBe(front.z);
    expect(depth.y - front.y).toBeCloseTo(.16);
    expect(geometry.center.z - geometry.height / 2).toBeCloseTo(.7);
    expect(geometry.center.z + geometry.height / 2).toBeCloseTo(1.15);
  });

  it('moves both devices by the same Y delta even when disabled, preserving all other devices and optics', () => {
    const baseline = defaultConfig();
    baseline.sensors.forEach(sensor => { sensor.enabled = false; });
    const current = sanitizeConfig({ ...baseline, supports: { ...baseline.supports, front: { ...baseline.supports!.front, t: .92 } } });
    const deltas = ['camera2', 'p100r'].map(id => {
      const before = find(baseline, id), after = find(current, id);
      expect(after.mount!.t).toBe(.92);
      expect({ ...after, y: before.y, mount: before.mount }).toEqual(before);
      return after.y - before.y;
    });
    expect(deltas[0]).toBeCloseTo(deltas[1]);
    expect(deltas[0]).toBeGreaterThan(0);
    for (const before of baseline.sensors.filter(sensor => !['camera2', 'p100r'].includes(sensor.id))) {
      expect(find(current, before.id)).toEqual(before);
    }
  });

  it('allows a proposed drag position before normalization and fixes both slot fractions only on save', () => {
    const config = defaultConfig(), rail = frameRails(config.chassis).find(member => member.id === 'plate-front-right')!;
    expect(rail.axis).toBe('y');
    const proposal = framePlacementFromMount({ rail: rail.id, t: .8 }, config.chassis)!;
    expect(proposal.mount.t).toBe(.8);
    expect(proposal.y).toBeGreaterThan(find(config, 'camera2').y);
    expect(cameraPlateLateralForY(config.chassis, proposal.y + .08)).toBeCloseTo(.8);
    const projected = framePlacement({ x: -8, y: proposal.y, z: -8 }, config.chassis, rail.id);
    expect(projected.mount.t).toBeCloseTo(.8);
    expect(projected.x).toBe(proposal.x);
    expect(projected.z).toBe(proposal.z);
    const inconsistent = sanitizeConfig({ ...config, sensors: config.sensors.map(sensor => sensor.id === 'camera2'
      ? { ...sensor, ...proposal } : sensor) });
    expect(inconsistent).toEqual(config);
  });

  it('clamps physical board boundaries and handles a full-width board without division by zero', () => {
    for (const lateral of [0, 1]) {
      const config = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
        cameraPlate: { width: .31, spacing: .27, thickness: .008, lateral },
      } });
      const geometry = cameraPlateGeometry(config.chassis);
      expect(geometry.center.y - geometry.width / 2).toBeGreaterThanOrEqual(-config.chassis.frameWidth / 2 - 1e-12);
      expect(geometry.center.y + geometry.width / 2).toBeLessThanOrEqual(config.chassis.frameWidth / 2 + 1e-12);
      expect(cameraPlateLateralForY(config.chassis, geometry.center.y)).toBe(lateral);
      for (const rail of frameRails(config.chassis).filter(member => member.kind === 'plate')) {
        expect(rail.axis).toBe('y');
        expect(rail.start.x).toBe(rail.end.x);
        expect(rail.start.z).toBe(rail.end.z);
      }
    }
    const full = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { width: 100, spacing: 100, lateral: .72 },
    } });
    const geometry = cameraPlateGeometry(full.chassis);
    expect(geometry.width).toBe(full.chassis.frameWidth);
    expect(geometry.spacing).toBeCloseTo(geometry.width - .02);
    expect(geometry.minCenterY).toBe(0);
    expect(geometry.maxCenterY).toBe(0);
    expect(cameraPlateLateralForY(full.chassis, 99)).toBe(.5);
    const projected = framePlacement({ x: 9, y: 9, z: 9 }, full.chassis, 'plate-front-right');
    expect(projected.mount.t).toBe(.72);
    expect([projected.x, projected.y, projected.z].every(Number.isFinite)).toBe(true);
    expect(sanitizeConfig(full)).toEqual(full);
  });

  it('validates all imported plate fields and drops unknown properties', () => {
    const invalids = [undefined, null, [], 'board', 7, {
      lateral: NaN, width: '1', spacing: Infinity, sensorHeight: {}, thickness: false, extra: 9,
    }];
    for (const cameraPlate of invalids) {
      const config = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis, cameraPlate } });
      expect(config.chassis.cameraPlate).toEqual(DEFAULT_CONFIG.chassis.cameraPlate);
      expect(sanitizeConfig(config)).toEqual(config);
    }
    const limits = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { lateral: -10, width: -10, spacing: -10, sensorHeight: 10, thickness: -10 },
    } });
    expect(limits.chassis.cameraPlate).toEqual({ lateral: 0, width: .04, spacing: .02, sensorHeight: 1, thickness: .001 });
    const max = sanitizeConfig({ ...limits, chassis: { ...limits.chassis,
      cameraPlate: { ...limits.chassis.cameraPlate, lateral: 10, sensorHeight: -10, thickness: 10 },
    } });
    expect(max.chassis.cameraPlate).toMatchObject({ lateral: 1, sensorHeight: 0, thickness: .1 });
  });

  it('retains the legacy plate geometry utility while authoritative boxes preserve optical points', () => {
    const before = defaultConfig(), geometry = cameraPlateGeometry(before.chassis);
    const after = sanitizeConfig({ ...before, chassis: { ...before.chassis,
      cameraPlate: { ...before.chassis.cameraPlate, thickness: .028 },
    } });
    const changed = cameraPlateGeometry(after.chassis);
    expect(changed.center.x - geometry.center.x).toBeCloseTo(.01);
    expect(changed.center.y).toBe(geometry.center.y);
    expect(changed.center.z).toBe(geometry.center.z);
    for (const id of ['camera2', 'p100r']) {
      const original = find(before, id), moved = find(after, id);
      expect(moved).toEqual(original);
      expect(after.supports).toEqual(before.supports);
    }
  });

  it('restores the complete shared layout without sharing mutable plate objects', () => {
    const current = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { width: .42, spacing: .21, thickness: .033, lateral: .18, sensorHeight: .73 },
    } });
    const loaded = loadBaseline(JSON.stringify({ version: 1, config: current }), defaultConfig());
    expect(loaded).toEqual(current);
    expect(sanitizeConfig(JSON.parse(JSON.stringify(current)))).toEqual(current);
    loaded.chassis.cameraPlate!.width = .1;
    expect(current.chassis.cameraPlate!.width).toBe(.42);
  });

  it('migrates revision-3 vertical plate fraction into height and keeps its previous lateral position', () => {
    const old = { layoutRevision: 3, chassis: { length: 1, width: .65, height: .25,
      frameHeight: .9, frameWidth: .65, frameLength: 1, frameMidHeight: .45 }, sensors: [
      { id: 'camera2', mount: { rail: 'plate-front-right', t: .63 }, enabled: false, pitch: 18 },
      { id: 'p100r', mount: { rail: 'z-front-left', t: (.78439228 - .25) / .9 }, yaw: 21 },
      { id: 'bluesea', mount: { rail: 'y-mid-front', t: .5 } },
    ] };
    const migrated = sanitizeConfig(old), geometry = cameraPlateGeometry(migrated.chassis);
    expect(migrated.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(geometry.sensorHeight).toBe(.63);
    expect(geometry.camera2.y).toBeCloseTo(-.65 * .32);
    expect(geometry.lateral).toBeCloseTo((-.65 * .32 + .08 + .175) / .35);
    expect(find(migrated, 'camera2')).toMatchObject({ enabled: false, pitch: 18 });
    expect(find(migrated, 'camera2').z).toBeCloseTo(.7 + .45 * .63);
    expect(find(migrated, 'p100r')).toMatchObject({ yaw: 21, mount: { rail: 'plate-front-depth', t: geometry.lateral } });
    expect(find(migrated, 'bluesea').mount).toEqual({ rail: 'y-mid-front', t: .5 });
    expect(sanitizeConfig(migrated)).toEqual(migrated);
    expect(old.sensors[0].mount.t).toBe(.63);
  });

  it('keeps both optical devices on their own shared slots in every saved layout revision', () => {
    for (const revision of [undefined, 3, 4, CURRENT_LAYOUT_REVISION]) {
      const imported = sanitizeConfig({ layoutRevision: revision, sensors: [
        { id: 'camera2', mount: { rail: 'z-front-right', t: .22 } },
        { id: 'p100r', mount: { rail: 'z-front-left', t: .12 } },
      ] });
      expect(find(imported, 'camera2').mount).toEqual({ rail: 'plate-front-right', t: .5 });
      expect(find(imported, 'p100r').mount).toEqual({ rail: 'plate-front-depth', t: .5 });
      expect(sanitizeConfig(imported)).toEqual(imported);
    }
    const modern = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { ...DEFAULT_CONFIG.chassis.cameraPlate, lateral: .77, sensorHeight: .19 },
    }, sensors: [{ id: 'p100r', mount: { rail: 'z-front-left', t: (.78439228 - .25) / .9 } }] });
    expect(find(modern, 'p100r').mount).toEqual({ rail: 'plate-front-depth', t: .77 });
    expect(modern.chassis.cameraPlate).toMatchObject({ lateral: .77, sensorHeight: .19 });
    expect(sanitizeConfig(modern)).toEqual(modern);
  });

  it('rebinds revision-4 detached cameras without changing the configured plate or optical settings', () => {
    const cameraPlate = { lateral: .82, width: .36, spacing: .2, sensorHeight: .28, thickness: .021 };
    const restored = sanitizeConfig({ ...DEFAULT_CONFIG, layoutRevision: 4,
      chassis: { ...DEFAULT_CONFIG.chassis, cameraPlate }, sensors: [
        { id: 'camera2', mount: { rail: 'z-rear-left', t: .15 }, x: -4, y: 5, z: 3,
          yaw: 17, pitch: 23, hfov: 92, vfov: 67, minRange: .2, maxRange: 2.8, enabled: false },
        { id: 'p100r', mount: { rail: 'y-bottom-rear', t: .9 }, x: 3, y: -5, z: .1,
          yaw: -12, pitch: 31, hfov: 65, vfov: 42, minRange: .3, maxRange: 2.4, enabled: true },
      ],
    });
    expect(restored.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(restored.chassis.cameraPlate).toEqual(cameraPlate);
    expect(find(restored, 'camera2')).toMatchObject({
      mount: { rail: 'plate-front-right', t: .82 },
      yaw: 17, pitch: 23, hfov: 92, vfov: 67, minRange: .2, maxRange: 2.8, enabled: false,
    });
    expect(find(restored, 'p100r')).toMatchObject({
      mount: { rail: 'plate-front-depth', t: .82 },
      yaw: -12, pitch: 31, hfov: 65, vfov: 42, minRange: .3, maxRange: 2.4, enabled: true,
    });
    const oldGeometry = cameraPlateGeometry(restored.chassis);
    for (const axis of ['x', 'y', 'z'] as const) {
      expect(find(restored, 'camera2')[axis]).toBeCloseTo(oldGeometry.camera2[axis], 12);
      expect(find(restored, 'p100r')[axis]).toBeCloseTo(oldGeometry.p100r[axis], 12);
    }
    expect(sanitizeConfig(restored)).toEqual(restored);
  });

  it('never treats revision-4 lateral fractions as the old vertical plate fraction', () => {
    for (const sensorHeight of [undefined, .26]) {
      const config = sanitizeConfig({ ...DEFAULT_CONFIG, layoutRevision: 4,
        chassis: { ...DEFAULT_CONFIG.chassis, cameraPlate: { lateral: .81, sensorHeight } },
        sensors: [{ id: 'camera2', mount: { rail: 'plate-front-right', t: .81 } },
          { id: 'p100r', mount: { rail: 'plate-front-depth', t: .81 } }],
      });
      expect(config.chassis.cameraPlate).toMatchObject({ lateral: .81, sensorHeight: sensorHeight ?? 4 / 9 });
      expect(find(config, 'camera2').mount?.t).toBe(.81);
      expect(sanitizeConfig(config)).toEqual(config);
    }
  });

  it('repairs malformed mounts on import and baseline restore without using their XYZ to move the plate', () => {
    for (const mount of [null, undefined, [], {}, { rail: 'missing', t: .5 },
      { rail: 'plate-front-right', t: NaN }, { rail: 'z-front-left', t: '0.2' }]) {
      const source = { ...DEFAULT_CONFIG, layoutRevision: 4, chassis: { ...DEFAULT_CONFIG.chassis,
        cameraPlate: { ...DEFAULT_CONFIG.chassis.cameraPlate, lateral: .34, sensorHeight: .62 },
      }, sensors: [{ id: 'camera2', mount, x: -6, y: -6, z: 5 },
        { id: 'p100r', mount, x: 6, y: 6, z: 0 }] };
      const config = sanitizeConfig(source);
      expect(config.chassis.cameraPlate).toMatchObject({ lateral: .34, sensorHeight: .62 });
      expect(find(config, 'camera2').mount).toEqual({ rail: 'plate-front-right', t: .34 });
      expect(find(config, 'p100r').mount).toEqual({ rail: 'plate-front-depth', t: .34 });
      expect(sanitizeConfig(JSON.parse(JSON.stringify(source)))).toEqual(config);
      expect(loadBaseline(JSON.stringify({ version: 1, config: source }), defaultConfig())).toEqual(config);
      expect(sanitizeConfig(config)).toEqual(config);
    }
  });

  it('preserves the other six devices custom mounts while restoring the two plate-bound cameras', () => {
    const mounts = { camera1: { rail: 'y-mid-rear', t: .21 }, camera3: { rail: 'x-mid-left', t: .33 },
      camera4: { rail: 'x-mid-right', t: .44 }, mid360: { rail: 'y-top-front', t: .55 },
      bluesea: { rail: 'y-mid-front', t: .5 }, stl19p: { rail: 'z-rear-left', t: .66 } };
    const source = { ...DEFAULT_CONFIG, layoutRevision: 4, sensors: [
      ...Object.entries(mounts).map(([id, mount]) => ({ id, mount, enabled: true, yaw: 11, pitch: 7 })),
      { id: 'camera2', mount: { rail: 'z-rear-left', t: .18 } },
      { id: 'p100r', mount: { rail: 'z-rear-right', t: .82 } },
    ] };
    const config = sanitizeConfig(source);
    for (const [id, mount] of Object.entries(mounts)) {
      expect(find(config, id)).toMatchObject({ mount, enabled: true, yaw: 11, pitch: 7 });
    }
    expect(find(config, 'camera2').mount?.rail).toBe('plate-front-right');
    expect(find(config, 'p100r').mount?.rail).toBe('plate-front-depth');
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('corrects cross-wired slots and keeps unrelated devices on aluminium members', () => {
    const config = sanitizeConfig({ ...DEFAULT_CONFIG, sensors: [
      { id: 'camera2', mount: { rail: 'plate-front-depth', t: .99 } },
      { id: 'p100r', mount: { rail: 'plate-front-right', t: .01 } },
      { id: 'camera1', mount: { rail: 'plate-front-depth', t: .5 } },
    ] });
    expect(find(config, 'camera2').mount).toEqual({ rail: 'plate-front-right', t: .5 });
    expect(find(config, 'p100r').mount).toEqual({ rail: 'plate-front-depth', t: .5 });
    const rearMount = find(config, 'camera1').mount!;
    expect(frameRails(config.chassis).find(rail => rail.id === rearMount.rail)!.kind).toBe('profile');
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('retains the baseline plate layout while comparing under the current frame and analysis conditions', () => {
    const baseline = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis,
      cameraPlate: { width: .27, spacing: .13, thickness: .004, lateral: .15, sensorHeight: .23 },
    } });
    const current = sanitizeConfig({ ...baseline, supports: { ...baseline.supports, front: { ...baseline.supports!.front, t: .85, sizeY: .4, offsetZ: .4 } } });
    const comparable = baselineUnderCurrentConditions(baseline, current);
    expect(comparable.chassis.cameraPlate).toEqual(baseline.chassis.cameraPlate);
    expect(comparable.sensors).toEqual(baseline.sensors);
    expect(baselineConditionsChanged(baseline, current)).toBe(false);
    expect(find(comparable, 'camera2').y).not.toBe(find(current, 'camera2').y);
    expect(find(comparable, 'p100r').z).not.toBe(find(current, 'p100r').z);
    const changedFrame = sanitizeConfig({ ...current, chassis: { ...current.chassis, frameHeight: 1.4 } });
    expect(baselineConditionsChanged(baseline, changedFrame)).toBe(true);
    const rescaled = baselineUnderCurrentConditions(baseline, changedFrame);
    expect(rescaled.chassis.frameHeight).toBe(1.4);
    expect(rescaled.chassis.cameraPlate).toEqual(baseline.chassis.cameraPlate);
    expect(find(rescaled, 'camera2').z).toBeCloseTo(find(baseline, 'camera2').z);
  });

  it('reports a real coverage change after the shared board moves instead of moving its baseline too', () => {
    const initial = defaultConfig();
    initial.sensors.forEach(sensor => { sensor.enabled = sensor.id === 'camera2'; });
    Object.assign(find(initial, 'camera2'), { yaw: 45, pitch: 45, hfov: 50, vfov: 40, maxRange: 2 });
    initial.analysis = { length: 3, width: 2, height: .2, resolution: .05, occlusion: false };
    const baseline = sanitizeConfig({ ...initial, supports: { ...initial.supports, front: { ...initial.supports!.front, t: .05 } } });
    const current = sanitizeConfig({ ...baseline, supports: { ...baseline.supports, front: { ...baseline.supports!.front, t: .95 } } });
    const comparison = compareCoverage(computeCoverage(current), computeBaselineCoverage(baseline, current));
    expect(comparison.coverage!.delta).not.toBe(0);
    expect(comparison.blind!.delta).not.toBe(0);
    expect(find(baselineUnderCurrentConditions(baseline, current), 'camera2').y).toBe(find(baseline, 'camera2').y);
  });
});
