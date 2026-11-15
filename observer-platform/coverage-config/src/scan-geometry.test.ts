import { describe, expect, it } from 'vitest';
import { Vector3 } from 'three';
import { DEFAULT_CONFIG, sanitizeConfig, type Sensor } from './model';
import { computeCoverage, sensorContains } from './geometry';

const scan = (changes: Partial<Sensor> = {}): Sensor => ({
  ...DEFAULT_CONFIG.sensors.find(sensor => sensor.id === 'bluesea')!,
  enabled: true, x: 0, y: 0, z: .7, yaw: 0, pitch: 0, minRange: .1, maxRange: 3, ...changes,
});

describe('zero-thickness two-dimensional scans', () => {
  it('contains real points only on its scan plane and at inclusive near/far ranges', () => {
    const sensor = scan();
    for (const point of [[.1, 0, .7], [3, 0, .7], [-2, 0, .7], [0, 2, .7]]) {
      expect(sensorContains(sensor, new Vector3(...point))).toBe(true);
    }
    for (const point of [[.099, 0, .7], [3.001, 0, .7], [1, 0, .7001], [1, 0, .6999]]) {
      expect(sensorContains(sensor, new Vector3(...point))).toBe(false);
    }
    expect(sensorContains(scan({ enabled: false }), new Vector3(1, 0, .7))).toBe(false);
  });

  it('supports limited scan azimuth and the actual inclined plane without adding vertical FOV', () => {
    expect(sensorContains(scan({ hfov: 90 }), new Vector3(1, 1, .7))).toBe(true);
    expect(sensorContains(scan({ hfov: 90 }), new Vector3(-1, 0, .7))).toBe(false);
    expect(sensorContains(scan({ hfov: 90, yaw: 90 }), new Vector3(0, 1, .7))).toBe(true);
    expect(sensorContains(scan({ pitch: 45 }), new Vector3(1, 0, -.3))).toBe(true);
    expect(sensorContains(scan({ pitch: 45 }), new Vector3(1, 0, .7))).toBe(false);
  });

  it('honours chassis occlusion even for a planar ray', () => {
    const sensor = scan({ x: -.6, z: .2 });
    expect(sensorContains(sensor, new Vector3(1, 0, .2), DEFAULT_CONFIG.chassis, true)).toBe(false);
    expect(sensorContains(sensor, new Vector3(1, 0, .2), DEFAULT_CONFIG.chassis, false)).toBe(true);
  });

  it('contributes slice area only when horizontal and at the same height', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    config.sensors.forEach(sensor => { sensor.enabled = sensor.id === 'bluesea'; });
    const sensor = config.sensors.find(item => item.id === 'bluesea')!;
    config.analysis = { ...config.analysis, height: sensor.z, length: 4, width: 4, resolution: .1, occlusion: false };
    expect(computeCoverage(config).covered).toBeGreaterThan(0);
    config.analysis.height = sensor.z + .0001;
    expect(computeCoverage(config).covered).toBe(0);
    config.analysis.height = sensor.z - .0001;
    expect(computeCoverage(config).covered).toBe(0);
    config.analysis.height = sensor.z;
    sensor.pitch = 20;
    expect(computeCoverage(config).covered).toBe(0);
    // A vertical scan plane still intersects a line of cell centres; that line
    // must not become a strip of positive area merely because the grid is coarse.
    sensor.pitch = 90;
    expect(computeCoverage(config).covered).toBe(0);
  });

  it('counts planar overlap at a shared horizontal slice without changing its area denominator', () => {
    const config = sanitizeConfig({ ...DEFAULT_CONFIG,
      sensors: DEFAULT_CONFIG.sensors.map(sensor => sensor.id === 'stl19p'
        ? { ...sensor, mount: { rail: 'y-bottom-rear', t: .5 } } : sensor),
    });
    config.sensors.forEach(sensor => { sensor.enabled = sensor.kind === 'scan'; });
    const [blue, stl] = config.sensors.filter(sensor => sensor.kind === 'scan');
    config.analysis = { ...config.analysis, height: blue.z, length: 4, width: 4, resolution: .1, occlusion: false };
    const both = computeCoverage(config);
    expect(both.perSensor.bluesea).toBeGreaterThan(0);
    expect(both.perSensor.stl19p).toBeGreaterThan(0);
    expect(both.overlap).toBeGreaterThan(0);
    expect(stl.z).toBe(blue.z);
    stl.enabled = false;
    const single = computeCoverage(config);
    expect(single.area).toBe(both.area);
    expect(single.overlap).toBe(0);
  });
});
