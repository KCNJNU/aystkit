import { describe, expect, it } from 'vitest';
import { Vector3 } from 'three';
import { DEFAULT_CONFIG, sanitizeConfig, type Config, type Sensor } from './model';
import { supportGeometry } from './mounting';
import { optimizeSideCameras, type SideOptimizerEngine } from './side-optimizer-core';

const ids = ['camera3', 'camera4'] as const;
const get = (config: Config, id: string) => config.sensors.find(sensor => sensor.id === id)!;
function fixture(x = .2): Config {
  const config = sanitizeConfig(DEFAULT_CONFIG);
  config.analysis = { length: 4, width: 4, height: 0, resolution: .2, occlusion: true };
  for (const id of ids) {
    const box = supportGeometry(config, id);
    config.supports![id]!.t = (x - box.rail.start.x) / (box.rail.end.x - box.rail.start.x);
  }
  return sanitizeConfig(config);
}

function engine(test: (sensor: Sensor, index: number) => boolean): SideOptimizerEngine {
  const points = [new Vector3(-.2, .6, 0), new Vector3(-.2, -.6, 0), new Vector3(-.8, 0, 0),
    new Vector3(2, 2, 0), new Vector3(2, -2, 0), new Vector3(-2, 2, 0), new Vector3(2.2, 0, 0)];
  const prepared = (sensor: Sensor) => (point: Vector3) => sensor.enabled && ids.some(id => id === sensor.id) && test(sensor, points.indexOf(point));
  return {
    normalize: sanitizeConfig, support: supportGeometry,
    prepareSensor: sensor => point => sensor.enabled && ids.some(id => id === sensor.id) && test(sensor, points.findIndex(item => item.x === point.x && item.y === point.y)),
    visible: (fn, point) => fn(point),
    coverage: input => {
      const config = sanitizeConfig(input), perSensor = Object.fromEntries(config.sensors.map(sensor => [sensor.id, 0]));
      const cells = points.map(point => {
        let count = 0;
        for (const sensor of config.sensors) if (prepared(sensor)(point)) { count++; perSensor[sensor.id]++; }
        return { x: point.x, y: point.y, count };
      });
      const covered = cells.filter(cell => cell.count > 0).length;
      return { cells, total: cells.length, covered, blind: cells.length - covered, overlap: cells.filter(cell => cell.count > 1).length,
        percent: covered / cells.length * 100, blindArea: (cells.length - covered) * .01, area: cells.length * .01,
        perSensor, step: .1, cellLength: .1, cellWidth: .1 };
    },
  };
}
const finiteAngles = { leftAngles: [{ yaw: 90, pitch: 45 }], rightAngles: [{ yaw: -90, pitch: 45 }], refinement: false, yieldControl: async () => {} };
const boxX = (config: Config) => supportGeometry(config, 'camera3').center.x;

describe('absolute beam-center recommendation priority', () => {
  it('makes an equal-coverage move from the front to the beam center applicable', async () => {
    const config = fixture(), result = await optimizeSideCameras(config, engine(() => true), finiteAngles);
    expect(result.status).toBe('improved');
    expect(result.coverageProtection).toBe('overall');
    expect(result.before).toEqual(result.after);
    expect(result.beamMidpointXCm).toBeCloseTo(0, 10);
    expect(result.beforeCenterDistanceCm).toBeCloseTo(20, 10);
    expect(result.afterCenterDistanceCm).toBeCloseTo(0, 10);
    expect(boxX(result.config)).toBeCloseTo(0, 10);
    expect(result.commonDeltaCm).toBeCloseTo(-20, 10);
  });

  it('prefers protected center coverage over a larger near-field gain farther forward', async () => {
    const result = await optimizeSideCameras(fixture(), engine((sensor, index) => index < (sensor.x > .1 && sensor.pitch === 45 ? 3 : 2)), finiteAngles);
    expect(boxX(result.config)).toBeCloseTo(0, 10);
    expect(result.after.nearField.covered).toBe(2);
    expect(result.after.overallCovered).toBe(result.before.overallCovered);
  });

  it('searches and refines a different mirrored angle at center even if its coarse angles violate the floor', async () => {
    const scene = engine((sensor, index) => {
      if (sensor.x > .15) return index < 4;
      const distance = Math.abs(Math.abs(sensor.yaw) - 99) + Math.abs(sensor.pitch - 52);
      return index < Math.max(0, 4 - Math.ceil(distance / 5));
    });
    const result = await optimizeSideCameras(fixture(), scene, { yieldControl: async () => {}, maxMs: 5000 });
    expect(result.status).toBe('improved');
    expect(result.positionScanComplete).toBe(true);
    expect(boxX(result.config)).toBeCloseTo(0, 10);
    expect(get(result.config, 'camera3').yaw).toBe(99);
    expect(get(result.config, 'camera4').yaw).toBe(-99);
    expect(get(result.config, 'camera3').pitch).toBe(52);
    expect(result.after.overallCovered).toBe(4);
  });

  it('chooses the closest protected 0.5 cm position when the exact midpoint loses overall coverage', async () => {
    const result = await optimizeSideCameras(fixture(), engine((sensor, index) => index < (sensor.x >= .03 - 1e-10 ? 4 : 3)), finiteAngles);
    expect(result.after.overallCovered).toBe(result.before.overallCovered);
    expect(boxX(result.config)).toBeCloseTo(.03, 9);
    expect(result.afterCenterDistanceCm).toBeCloseTo(3, 9);
  });

  it('uses physical beam ends instead of their offset box-center paths', async () => {
    const config = fixture(); config.supports!.camera3!.offsetX = .08; config.supports!.camera4!.offsetX = -.04;
    const result = await optimizeSideCameras(sanitizeConfig(config), engine(() => true), finiteAngles);
    expect(result.beamMidpointXCm).toBeCloseTo(0, 10);
    expect(boxX(result.config)).toBeCloseTo(0, 10);
    expect(supportGeometry(result.config, 'camera4').center.x).toBeCloseTo(0, 10);
    for (const id of ids) {
      const box = supportGeometry(result.config, id);
      expect(box.min.x).toBeGreaterThanOrEqual(box.rail.start.x - box.layout.offsetX - 1e-8);
      expect(box.max.x).toBeLessThanOrEqual(box.rail.end.x - box.layout.offsetX + 1e-8);
    }
  });

  it('allows near-field coverage to decrease when centering increases overall coverage, and retains strict regional protection on request', async () => {
    const config = fixture(), scene = engine((sensor, index) => sensor.x > .1 ? index < 3 : index >= 3);
    const result = await optimizeSideCameras(config, scene, finiteAngles);
    expect(boxX(result.config)).toBeCloseTo(0, 10);
    expect(result.before.overallCovered).toBe(3);
    expect(result.after.overallCovered).toBe(4);
    expect(result.after.nearField.covered).toBeLessThan(result.before.nearField.covered);
    expect(result.reason).toContain('近场覆盖减少');
    const protectedResult = await optimizeSideCameras(config, scene, { ...finiteAngles, coverageProtection: 'all-regions' });
    expect(boxX(protectedResult.config)).toBeGreaterThan(.1);
    expect(protectedResult.after.nearField.covered).toBe(protectedResult.before.nearField.covered);
  });

  it('returns the unchanged baseline and an explicit incomplete-search flag when the budget cannot evaluate another candidate', async () => {
    const config = fixture(), result = await optimizeSideCameras(config, engine(() => true), { maxEvaluations: 1 });
    expect(result.status).toBe('unchanged');
    expect(result.config).toEqual(config);
    expect(result.coverageProtection).toBe('overall');
    expect(result.positionScanComplete).toBe(false);
    expect(result.searchLimited).toBe(true);
  });
});
