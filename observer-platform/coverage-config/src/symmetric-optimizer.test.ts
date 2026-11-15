import { describe, expect, it } from 'vitest';
import { Vector3 } from 'three';
import { DEFAULT_CONFIG, sanitizeConfig, type Config, type Sensor } from './model';
import { supportGeometry } from './mounting';
import { optimizeCameraPositions } from './optimizer';
import { optimizeSideCameras, type SideOptimizerEngine } from './side-optimizer-core';

const ids = ['camera3', 'camera4'] as const;
const sensor = (config: Config, id: string) => config.sensors.find(item => item.id === id)!;
const fixture = () => {
  const config = sanitizeConfig(DEFAULT_CONFIG);
  config.analysis = { length: 4, width: 4, height: 0, resolution: .2, occlusion: true };
  return sanitizeConfig(config);
};

/** A constant-area scene isolates layout corrections from coverage gains. */
function correctionEngine(asymmetricOnly = false): SideOptimizerEngine {
  const points = [new Vector3(-.2, .6, 0), new Vector3(-.2, -.6, 0), new Vector3(-.8, 0, 0)];
  const test = (item: Sensor) => (_point: Vector3) => item.enabled && ids.some(id => id === item.id) &&
    (!asymmetricOnly || item.id === 'camera3' && item.yaw === 80 && item.pitch === 30);
  return {
    normalize: sanitizeConfig, support: supportGeometry, prepareSensor: test,
    visible: (prepared, point) => prepared(point),
    coverage: input => {
      const config = sanitizeConfig(input), perSensor = Object.fromEntries(config.sensors.map(item => [item.id, 0]));
      const cells = points.map(point => {
        let count = 0;
        for (const item of config.sensors) if (test(item)(point)) { count++; perSensor[item.id]++; }
        return { x: point.x, y: point.y, count };
      });
      const covered = cells.filter(cell => cell.count > 0).length;
      return { cells, total: cells.length, covered, blind: cells.length - covered, overlap: cells.filter(cell => cell.count > 1).length,
        percent: covered / cells.length * 100, blindArea: (cells.length - covered) * .01, area: cells.length * .01,
        perSensor, step: .1, cellLength: .1, cellWidth: .1 };
    },
  };
}

describe('symmetric side-camera recommendations', () => {
  it('aligns the initial boxes, then applies one shared translation and mirrored refined angles in the real geometry engine', async () => {
    const config = fixture();
    config.sensors.forEach(item => { item.enabled = ids.some(id => id === item.id); });
    config.supports!.camera3!.t = .4; config.supports!.camera4!.t = .6;
    sensor(config, 'camera3').yaw = 85; sensor(config, 'camera4').yaw = -95;
    for (const id of ids) sensor(config, id).pitch = 0;
    const current = sanitizeConfig(config);
    const result = await optimizeCameraPositions(current, {
      leftAngles: [{ yaw: 90, pitch: 45 }], rightAngles: [{ yaw: -90, pitch: 45 }],
      positionStepCm: 10, maxMs: 5000, yieldControl: async () => {},
      refinementLevels: [{ positionStepCm: .5, positionRadiusCm: 2, angleStep: 1, angleRadius: 2 }], seedCount: 1,
    });
    expect(result.status).toBe('improved');
    expect(result.positionScanComplete).toBe(true);
    expect(sensor(result.config, 'camera4').yaw).toBe(-sensor(result.config, 'camera3').yaw);
    expect(sensor(result.config, 'camera4').pitch).toBe(sensor(result.config, 'camera3').pitch);
    const boxes = ids.map(id => supportGeometry(result.config, id));
    expect(boxes[0].center.x).toBeCloseTo(boxes[1].center.x, 10);
    expect(result.alignmentOffsetsCm!.camera3).toBeCloseTo(10, 10);
    expect(result.alignmentOffsetsCm!.camera4).toBeCloseTo(-10, 10);
    for (const id of ids) {
      const actualDelta = (sensor(result.config, id).x - sensor(current, id).x) * 100;
      expect(actualDelta - result.alignmentOffsetsCm![id]).toBeCloseTo(result.commonDeltaCm!, 8);
    }
    expect(result.after.overallCovered).toBeGreaterThanOrEqual(result.before.overallCovered);
    expect(result.after.nearField!.covered).toBeGreaterThanOrEqual(result.before.nearField!.covered);
  });

  it('applies equal-coverage angle correction without ever selecting the asymmetric baseline', async () => {
    const config = fixture(); sensor(config, 'camera3').yaw = 80; sensor(config, 'camera3').pitch = 30;
    sensor(config, 'camera4').yaw = -100; sensor(config, 'camera4').pitch = 40;
    const result = await optimizeSideCameras(config, correctionEngine(), {
      positionCandidatesCm: [0], leftAngles: [{ yaw: 90, pitch: 35 }], rightAngles: [{ yaw: -90, pitch: 35 }], refinement: false,
    });
    expect(result.status).toBe('improved');
    expect(result.reason).toContain('镜像');
    expect(result.after).toEqual(result.before);
    expect(sensor(result.config, 'camera3').yaw).toBe(90);
    expect(sensor(result.config, 'camera4').yaw).toBe(-90);
    expect(result.rotatedIds).toEqual(['camera3', 'camera4']);
  });

  it('uses the original asymmetric baseline only as a coverage guard when no mirror can protect it', async () => {
    const config = fixture(); sensor(config, 'camera3').yaw = 80; sensor(config, 'camera3').pitch = 30;
    sensor(config, 'camera4').yaw = -100; sensor(config, 'camera4').pitch = 40;
    const result = await optimizeSideCameras(config, correctionEngine(true), {
      positionCandidatesCm: [0], leftAngles: [{ yaw: 90, pitch: 35 }], rightAngles: [{ yaw: -90, pitch: 35 }], refinement: false,
    });
    expect(result.status).toBe('unchanged');
    expect(result.reason).toContain('未找到');
    expect(result.config).toEqual(config);
    expect(result.movedIds).toEqual([]);
  });

  it('corrects unequal box centers even if the best coverage ties the original layout', async () => {
    const config = fixture(); config.supports!.camera3!.t = .3; config.supports!.camera4!.t = .6;
    const current = sanitizeConfig(config), result = await optimizeSideCameras(current, correctionEngine(), {
      positionCandidatesCm: [0], leftAngles: [sensor(current, 'camera3')], rightAngles: [sensor(current, 'camera4')], refinement: false,
    });
    expect(result.status).toBe('improved');
    expect(result.commonDeltaCm).toBe(0);
    expect(result.commonReferenceXCm).toBeCloseTo(-5, 10);
    expect(supportGeometry(result.config, 'camera3').center.x).toBeCloseTo(-.05, 10);
    expect(supportGeometry(result.config, 'camera4').center.x).toBeCloseTo(-.05, 10);
    expect(result.after).toEqual(result.before);
    expect(result.translatedIds).toEqual(['camera3', 'camera4']);
  });

  it('preserves every parameter and gives an explanation when one side is disabled', async () => {
    const config = fixture(); sensor(config, 'camera4').enabled = false;
    const result = await optimizeCameraPositions(config);
    expect(result.status).toBe('no-improvement');
    expect(result.reason).toContain('同时启用');
    expect(result.config).toEqual(config);
    expect(result.movedIds).toEqual([]);
  });
});
