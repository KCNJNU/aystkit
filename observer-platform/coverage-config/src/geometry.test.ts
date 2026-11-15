import { describe, expect, it } from 'vitest';
import { Vector3 } from 'three';
import { DEFAULT_CONFIG, sanitizeConfig, type Sensor } from './model';
import { computeCoverage, sensorBasis, sensorContains } from './geometry';
import { framePlacement } from './frame';

const camera = (changes: Partial<Sensor> = {}): Sensor => ({
  ...DEFAULT_CONFIG.sensors[0], x: 0, y: 0, z: 1, yaw: 0, pitch: 0,
  hfov: 90, vfov: 60, minRange: 0.5, maxRange: 3, verticalOffset: 0, ...changes,
});

describe('sensor geometry', () => {
  it('uses X forward, Y left, Z up, with positive pitch pointing down', () => {
    const basis = sensorBasis(camera({ yaw: 90, pitch: 30 }));
    expect(basis.forward.y).toBeCloseTo(Math.sqrt(3) / 2);
    expect(basis.forward.z).toBeCloseTo(-0.5);
    expect(basis.right.x).toBeCloseTo(1);
    expect(basis.forward.dot(basis.up)).toBeCloseTo(0);
    expect(basis.forward.dot(basis.right)).toBeCloseTo(0);
    expect(basis.forward.length()).toBeCloseTo(1);
    expect(basis.up.length()).toBeCloseTo(1);
  });

  it('checks rectangular camera corners and 3D near/far distances', () => {
    const sensor = camera();
    expect(sensorContains(sensor, new Vector3(1, 0, 1))).toBe(true);
    expect(sensorContains(sensor, new Vector3(1, 1, 1 + Math.tan(Math.PI / 6)))).toBe(true);
    expect(sensorContains(sensor, new Vector3(1, 1.01, 1))).toBe(false);
    expect(sensorContains(sensor, new Vector3(1, 0, 1.59))).toBe(false);
    expect(sensorContains(sensor, new Vector3(-1, 0, 1))).toBe(false);
    expect(sensorContains(sensor, new Vector3(0.49, 0, 1))).toBe(false);
    expect(sensorContains(sensor, new Vector3(0.5, 0, 1))).toBe(true);
    expect(sensorContains(sensor, new Vector3(3.01, 0, 1))).toBe(false);
    expect(sensorContains(sensor, new Vector3(2.5, 2, 1))).toBe(false);
  });

  it('rotates yaw and downward pitch rather than moving the sensor origin', () => {
    expect(sensorContains(camera({ yaw: 90 }), new Vector3(0, 2, 1))).toBe(true);
    expect(sensorContains(camera({ yaw: 90 }), new Vector3(2, 0, 1))).toBe(false);
    expect(sensorContains(camera({ pitch: 45 }), new Vector3(1, 0, 0))).toBe(true);
    expect(sensorContains(camera({ pitch: 45 }), new Vector3(1, 0, 1.9))).toBe(false);
    expect(sensorContains(camera({ enabled: false }), new Vector3(1, 0, 1))).toBe(false);
  });

  it('models the lidar intrinsic -7° to 52° band at all azimuths', () => {
    const lidar = camera({ kind: 'lidar', hfov: 360, vfov: 59, verticalOffset: 22.5, pitch: 0 });
    const point = (elevation: number, yaw: number) => new Vector3(
      Math.cos(elevation * Math.PI / 180) * Math.cos(yaw * Math.PI / 180),
      Math.cos(elevation * Math.PI / 180) * Math.sin(yaw * Math.PI / 180),
      1 + Math.sin(elevation * Math.PI / 180),
    );
    for (const yaw of [0, 90, 180, -90]) {
      expect(sensorContains(lidar, point(-7, yaw))).toBe(true);
      expect(sensorContains(lidar, point(52, yaw))).toBe(true);
      expect(sensorContains(lidar, point(-8, yaw))).toBe(false);
      expect(sensorContains(lidar, point(53, yaw))).toBe(false);
    }
  });

  it('respects chassis occlusion and permits outward rays from its surface', () => {
    const body = DEFAULT_CONFIG.chassis;
    const rearFacingIn = camera({ x: -0.6, z: 0.2, minRange: 0 });
    const across = new Vector3(1, 0, 0.2);
    expect(sensorContains(rearFacingIn, across, body, true)).toBe(false);
    expect(sensorContains(rearFacingIn, across, body, false)).toBe(true);
    expect(sensorContains(camera({ x: 0.5, z: 0.2, minRange: 0 }), across, body)).toBe(true);
    expect(sensorContains(camera({ x: -0.6, z: 1, minRange: 0 }), new Vector3(1, 0, 1), body)).toBe(true);
    expect(sensorContains(camera({ x: -0.6, y: 0.325, z: 0.2, minRange: 0 }), new Vector3(1, 0.325, 0.2), body)).toBe(true);
  });
});

describe('slice coverage', () => {
  it('counts union and overlap separately, excluding the chassis footprint', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    config.analysis = { length: 2, width: 2, height: 0.2, resolution: 0.1, occlusion: false };
    config.sensors.forEach((sensor) => { sensor.enabled = false; });
    const absent = computeCoverage(config);
    expect(absent.total).toBeLessThan(400);
    expect(absent.cells.every((cell) => Math.abs(cell.x) > 0.5 || Math.abs(cell.y) > 0.325)).toBe(true);
    expect(absent.percent).toBe(0);
    expect(absent.blindArea).toBeCloseTo(absent.area);

    Object.assign(config.sensors[0], camera({ pitch: 90, hfov: 179, vfov: 179, minRange: 0, maxRange: 10 }), framePlacement({ x: -.325, y: 0, z: 1.15 }, config.chassis, 'y-top-rear'));
    Object.assign(config.sensors[1], config.sensors[0], { id: 'camera2' });
    config.sensors[0].enabled = true;
    config.sensors[1].enabled = true;
    const full = computeCoverage(config);
    expect(full.covered).toBe(full.total);
    expect(full.percent).toBe(100);
    expect(full.overlap).toBe(full.total);
    expect(full.perSensor.camera1).toBe(full.total);
    expect(full.perSensor.camera2).toBe(full.total);
    expect(full.blindArea).toBe(0);
  });

  it('recomputes blind zones after disabling a sensor and changing slice height', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    config.sensors.forEach((sensor) => { sensor.enabled = false; });
    Object.assign(config.sensors[0], camera({ pitch: 90, vfov: 90, minRange: 0, maxRange: 10, enabled: true }));
    config.analysis.occlusion = false;
    const low = computeCoverage(config);
    config.analysis.height = 3;
    const high = computeCoverage(config);
    expect(low.covered).toBeGreaterThan(high.covered);
    config.sensors[0].enabled = false;
    expect(computeCoverage(config).covered).toBe(0);
  });

  it('fills arbitrary analysis dimensions with exact rectangular cell areas', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    config.analysis = { ...config.analysis, length: 2.03, width: 1.27, resolution: 0.1 };
    config.chassis = { ...config.chassis, length: 0.2, width: 0.2 };
    const result = computeCoverage(config);
    expect(result.cellLength * 21).toBeCloseTo(2.03);
    expect(result.cellWidth * 13).toBeCloseTo(1.27);
    expect(result.area).toBeCloseTo(result.total * result.cellLength * result.cellWidth);
    expect(result.cells.every((cell) => Math.abs(cell.x) < 2.03 / 2 && Math.abs(cell.y) < 1.27 / 2)).toBe(true);
  });
});

describe('configuration boundaries', () => {
  it('accepts eight known sensors only and rejects nonfinite or string numeric values', () => {
    const sanitized = sanitizeConfig({
      sensors: [{ id: 'camera1', x: Infinity, y: '2', z: -10, hfov: 999, minRange: 29.99, maxRange: 1 }, { id: 'injected' }],
      analysis: { length: 999, width: -5, resolution: 0, height: NaN },
    });
    expect(sanitized.sensors).toHaveLength(8);
    expect(sanitized.sensors[0].mount).toEqual(framePlacement({ x: DEFAULT_CONFIG.sensors[0].x, y: DEFAULT_CONFIG.sensors[0].y, z: 0 }, sanitized.chassis).mount);
    expect(sanitized.sensors[0].x).toBeCloseTo(-.56);
    expect(sanitized.sensors[0].z).toBeCloseTo(sanitized.chassis.height - .10);
    expect(sanitized.sensors[0].hfov).toBe(179);
    expect(sanitized.sensors[0].maxRange).toBeCloseTo(30);
    expect(sanitized.analysis).toMatchObject({ length: 12, width: 0.5, resolution: 0.04, height: 0.2 });
    expect(DEFAULT_CONFIG.sensors[0].z).toBe(0.91);
  });

  it('keeps lidar vertical-band endpoints inside the physical -90° to 90° interval', () => {
    for (const offset of [-999, 999, undefined]) {
      const config = sanitizeConfig({ sensors: [{ id: 'mid360', vfov: 179, verticalOffset: offset }] });
      const lidar = config.sensors.find((sensor) => sensor.id === 'mid360')!;
      expect((lidar.verticalOffset ?? 0) - lidar.vfov / 2).toBeGreaterThanOrEqual(-90);
      expect((lidar.verticalOffset ?? 0) + lidar.vfov / 2).toBeLessThanOrEqual(90);
    }
    const cameraConfig = sanitizeConfig({ sensors: [{ id: 'camera1', verticalOffset: 89 }] });
    expect(cameraConfig.sensors[0].verticalOffset).toBe(0);
  });
});
