import { describe, expect, it } from 'vitest';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { FrameSide } from './model';
import { cameraPlateGeometry, frameRails } from './frame';
import { computeCoverage } from './geometry';
import { baselineUnderCurrentConditions, compareCoverage, computeBaselineCoverage, loadBaseline } from './comparison';

const sides: FrameSide[] = ['front', 'rear', 'left', 'right'];
const middleIds: Record<FrameSide, string> = {
  front: 'y-mid-front', rear: 'y-mid-rear', left: 'x-mid-left', right: 'x-mid-right',
};
const heights = { front: .2, rear: .35, left: .55, right: .7 };

describe('independent heights of the four middle frame members', () => {
  it('migrates the old shared height to all four faces and defaults to half the frame height', () => {
    const old = { chassis: { frameHeight: 1.2, frameMidHeight: .67 } };
    const migrated = sanitizeConfig(old);
    expect(migrated.chassis.frameMidHeights).toEqual({ front: .67, rear: .67, left: .67, right: .67 });
    expect(migrated.chassis.frameMidHeight).toBe(.67);
    expect(old.chassis).not.toHaveProperty('frameMidHeights');
    expect(sanitizeConfig({ chassis: { frameHeight: .3 } }).chassis.frameMidHeights).toEqual({ front: .15, rear: .15, left: .15, right: .15 });
    expect(sanitizeConfig(DEFAULT_CONFIG).chassis.frameMidHeights).toEqual({ front: .45, rear: .45, left: .45, right: .45 });
  });

  it('builds four horizontal members at independent heights with the same stable IDs', () => {
    const config = sanitizeConfig({ chassis: { frameMidHeights: heights } });
    const rails = frameRails(config.chassis);
    expect(rails).toHaveLength(18);
    expect(rails.slice(0, 12)).toEqual(frameRails(sanitizeConfig(DEFAULT_CONFIG).chassis).slice(0, 12));
    for (const side of sides) {
      const rail = rails.find(member => member.id === middleIds[side])!;
      expect(rail.start.z).toBe(config.chassis.height + heights[side]);
      expect(rail.end.z).toBe(rail.start.z);
    }
    // The renderer also accepts a valid partial chassis before persistence sanitizes it.
    const direct = frameRails({ ...DEFAULT_CONFIG.chassis, frameMidHeight: .4, frameMidHeights: { front: .6 } });
    expect(direct.find(rail => rail.id === 'y-mid-front')!.start.z).toBeCloseTo(.85);
    expect(direct.find(rail => rail.id === 'y-mid-rear')!.start.z).toBeCloseTo(.65);
  });

  it('changes only one face and the sensor mounted to it while preserving every mount and optical parameter', () => {
    const mounts: Record<string, string> = { camera1: 'y-mid-front', camera3: 'y-mid-rear', camera4: 'x-mid-left', mid360: 'x-mid-right' };
    const original = sanitizeConfig({ ...DEFAULT_CONFIG,
      sensors: DEFAULT_CONFIG.sensors.map((sensor, index) => mounts[sensor.id] ? { ...sensor, mount: { rail: mounts[sensor.id], t: .31 + index * .1 } } : sensor),
      chassis: { ...DEFAULT_CONFIG.chassis, frameMidHeights: heights },
    });
    const after = sanitizeConfig({ ...original,
      chassis: { ...original.chassis, frameMidHeights: { ...original.chassis.frameMidHeights, front: .62 } },
    });
    for (const sensor of original.sensors) {
      const moved = after.sensors.find(item => item.id === sensor.id)!;
      expect(moved.mount).toEqual(sensor.mount);
      expect(moved.x).toBe(sensor.x);
      expect(moved.y).toBe(sensor.y);
      if (sensor.mount?.rail === 'y-mid-front') {
        expect(moved.z).toBeCloseTo(sensor.z + .42);
        expect({ ...moved, z: sensor.z }).toEqual(sensor);
      } else if (sensor.mount?.rail === 'plate-front-depth' || sensor.mount?.rail === 'plate-front-right') {
        expect(moved.z).toBeCloseTo(sensor.z + .42);
        expect({ ...moved, z: sensor.z }).toEqual(sensor);
      } else expect(moved).toEqual(sensor);
    }
    const beforeRails = frameRails(original.chassis), afterRails = frameRails(after.chassis);
    for (const rail of beforeRails) {
      if (rail.id !== 'y-mid-front' && rail.kind !== 'plate') expect(afterRails.find(member => member.id === rail.id)).toEqual(rail);
    }
    for (const rail of afterRails.filter(member => member.kind === 'plate')) {
      expect(rail.start.z).toBeCloseTo(cameraPlateGeometry(after.chassis).camera2.z);
      expect(rail.end.z).toBe(rail.start.z);
    }
  });

  it('uses the legacy height only for absent or invalid side values and never accepts unknown fields', () => {
    const config = sanitizeConfig({ chassis: { frameMidHeight: .3, frameMidHeights: { front: NaN, rear: Infinity, left: -3, right: 7, roof: .4 } } });
    expect(config.chassis.frameMidHeights).toEqual({ front: .3, rear: .3, left: .05, right: .85 });
    expect(sanitizeConfig({ chassis: { frameMidHeight: .31, frameMidHeights: { front: .7 } } }).chassis.frameMidHeights)
      .toEqual({ front: .7, rear: .31, left: .31, right: .31 });
    for (const invalid of [[], 'middle', 9, null, { front: '0.2', rear: false, left: {}, right: [] }]) {
      expect(sanitizeConfig({ chassis: { frameMidHeight: .28, frameMidHeights: invalid } }).chassis.frameMidHeights)
        .toEqual({ front: .28, rear: .28, left: .28, right: .28 });
    }
  });

  it('clamps each height independently when the frame is shortened, without modifying mounted fractions', () => {
    const config = sanitizeConfig({ chassis: { frameMidHeights: { front: .1, rear: .2, left: .3, right: .7 } } });
    const short = sanitizeConfig({ ...config, chassis: { ...config.chassis, frameHeight: .25 } });
    expect(short.chassis.frameMidHeights).toEqual({ front: .1, rear: .2, left: .2, right: .2 });
    for (const sensor of short.sensors) expect(sensor.mount).toEqual(config.sensors.find(original => original.id === sensor.id)!.mount);
    const smallest = sanitizeConfig({ ...config, chassis: { ...config.chassis, frameHeight: .1 } });
    expect(smallest.chassis.frameMidHeights).toEqual({ front: .05, rear: .05, left: .05, right: .05 });
    expect(sanitizeConfig(short)).toEqual(short);
  });

  it('preserves the four independent heights through JSON import, persistence and baseline restore', () => {
    const current = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis, frameMidHeights: heights } });
    const decoded = sanitizeConfig(JSON.parse(JSON.stringify(current)));
    expect(decoded).toEqual(current);
    expect(sanitizeConfig(decoded)).toEqual(current);
    const restored = loadBaseline(JSON.stringify({ version: 1, config: current }), sanitizeConfig(DEFAULT_CONFIG));
    expect(restored).toEqual(current);
    restored.chassis.frameMidHeights!.front = .8;
    expect(current.chassis.frameMidHeights!.front).toBe(.2);
  });

  it('maps the stored baseline to all four current member heights before reporting a layout difference', () => {
    const base = sanitizeConfig({ ...DEFAULT_CONFIG,
      sensors: DEFAULT_CONFIG.sensors.map(sensor => sensor.id === 'camera1' ? { ...sensor, mount: { rail: 'y-mid-rear', t: .5 } } : sensor),
    });
    const originalBaseline = JSON.stringify(base);
    const current = sanitizeConfig({ ...base, chassis: { ...base.chassis, frameMidHeights: heights } });
    const comparable = baselineUnderCurrentConditions(base, current);
    expect(comparable.chassis.frameMidHeights).toEqual(heights);
    expect(comparable.sensors).toEqual(current.sensors);
    expect(JSON.stringify(base)).toBe(originalBaseline);
    const delta = compareCoverage(computeCoverage(current), computeBaselineCoverage(base, current));
    expect(delta.coverage!.delta).toBe(0);
    expect(delta.blind!.delta).toBe(0);
    expect(delta.overlap!.delta).toBe(0);
  });
});
