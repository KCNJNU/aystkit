import { describe, expect, it } from 'vitest';
import { CURRENT_LAYOUT_REVISION, DEFAULT_CONFIG, sanitizeConfig } from './model';
import { cameraPlateGeometry, framePlacement, frameRails } from './frame';
import { baselineUnderCurrentConditions, compareCoverage, computeBaselineCoverage } from './comparison';
import { computeCoverage } from './geometry';

const oldCameraMount = { rail: 'z-front-right', t: (.9 - .25) / .9 };
const oldBlueMount = { rail: 'y-mid-front', t: .5 };

describe('confirmed bottom scanner and front camera mounting plate', () => {
  it('appends two horizontal slots on one plastic plate after the same sixteen aluminium members', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG), rails = frameRails(config.chassis);
    expect(rails).toHaveLength(18);
    expect(rails.slice(0, 16).every(rail => rail.kind === 'profile')).toBe(true);
    const plate = cameraPlateGeometry(config.chassis);
    expect(rails[16]).toEqual({ id: 'plate-front-right', label: '前相机共享塑料板 · 左右', kind: 'plate', axis: 'y',
      start: { x: .508, y: plate.minCenterY - .08, z: plate.camera2.z }, end: { x: .508, y: plate.maxCenterY - .08, z: plate.camera2.z } });
    expect(rails[17]).toMatchObject({ id: 'plate-front-depth', kind: 'plate', axis: 'y' });
  });

  it('puts camera2 on the front plate and BlueSea on the existing bottom member', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    const camera = config.sensors.find(sensor => sensor.id === 'camera2')!;
    const blue = config.sensors.find(sensor => sensor.id === 'bluesea')!;
    expect(config.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(camera.mount).toEqual({ rail: 'plate-front-right', t: .5 });
    expect(camera.x).toBe(.508);
    expect(camera.y).toBeCloseTo(-.08);
    expect(camera.z).toBeCloseTo(.9);
    expect(blue.mount).toEqual({ rail: 'y-bottom-front', t: .5 });
    expect(blue.z).toBeCloseTo(config.chassis.height - .09);
    expect(config.sensors.find(sensor => sensor.id === 'stl19p')!.mount?.rail).toBe('y-mid-rear');
  });

  it('connects the plate to the independently adjusted front middle member and the fixed top member', () => {
    const before = sanitizeConfig({ ...DEFAULT_CONFIG, chassis: { ...DEFAULT_CONFIG.chassis, frameMidHeights: { front: .2, rear: .3, left: .4, right: .5 } } });
    const after = sanitizeConfig({ ...before, chassis: { ...before.chassis, frameMidHeights: { ...before.chassis.frameMidHeights, front: .6 } } });
    const plate = frameRails(after.chassis).find(rail => rail.id === 'plate-front-right')!;
    const board = cameraPlateGeometry(after.chassis);
    expect(board.center.z - board.height / 2).toBeCloseTo(.85);
    expect(board.center.z + board.height / 2).toBeCloseTo(1.15);
    expect(plate.start.z).toBe(plate.end.z);
    const camera = after.sensors.find(sensor => sensor.id === 'camera2')!;
    expect(camera.mount).toEqual(before.sensors.find(sensor => sensor.id === 'camera2')!.mount);
    expect(camera.z).toBeCloseTo(before.sensors.find(sensor => sensor.id === 'camera2')!.z + .4);
    expect(after.supports!.front!.sizeZ).toBe(before.supports!.front!.sizeZ);
    expect(after.sensors.find(sensor => sensor.id === 'bluesea')).toEqual(before.sensors.find(sensor => sensor.id === 'bluesea'));
    const otherFace = sanitizeConfig({ ...after, chassis: { ...after.chassis, frameMidHeights: { ...after.chassis.frameMidHeights, right: .8 } } });
    expect(otherFace.sensors.find(sensor => sensor.id === 'camera2')).toEqual(camera);
    expect(frameRails(otherFace.chassis).find(rail => rail.id === plate.id)).toEqual(plate);
  });

  it('corrects only old default mounts once, preserving the prior camera height and optical parameters', () => {
    const legacy = { chassis: { ...DEFAULT_CONFIG.chassis }, sensors: [
      { id: 'camera2', mount: oldCameraMount, enabled: false, yaw: 14, pitch: 27, hfov: 102 },
      { id: 'bluesea', mount: oldBlueMount, enabled: true, hfov: 270 },
    ] };
    const corrected = sanitizeConfig(legacy);
    const camera = corrected.sensors.find(sensor => sensor.id === 'camera2')!;
    expect(camera).toMatchObject({ enabled: false, yaw: 14, pitch: 27, hfov: 102 });
    expect(camera.mount?.rail).toBe('plate-front-right');
    expect(camera.z).toBeCloseTo(.9);
    expect(corrected.sensors.find(sensor => sensor.id === 'bluesea')).toMatchObject({ mount: { rail: 'y-bottom-front', t: .5 }, enabled: true, hfov: 270 });
    expect(corrected.sensors.find(sensor => sensor.id === 'bluesea')!.z).toBeCloseTo(.16, 12);
    expect(corrected.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(sanitizeConfig(JSON.parse(JSON.stringify(corrected)))).toEqual(corrected);
  });

  it('binds legacy front-camera mounts to the plate while retaining other custom mounts', () => {
    const config = sanitizeConfig({ sensors: [
      { id: 'camera2', mount: { rail: 'z-front-right', t: .22 } },
      { id: 'bluesea', mount: { rail: 'y-mid-front', t: .23 } },
      { id: 'camera1', mount: { rail: 'x-mid-left', t: .72 } },
    ] });
    expect(config.sensors.find(sensor => sensor.id === 'camera2')!.mount).toEqual({ rail: 'plate-front-right', t: .5 });
    expect(config.sensors.find(sensor => sensor.id === 'bluesea')!.mount).toEqual({ rail: 'y-mid-front', t: .23 });
    expect(config.sensors.find(sensor => sensor.id === 'camera1')!.mount).toEqual({ rail: 'x-mid-left', t: .72 });
  });

  it('enforces the front camera plate in current revisions without relocating other chosen members', () => {
    const chosen = sanitizeConfig({ ...DEFAULT_CONFIG, sensors: [
      { id: 'camera2', mount: oldCameraMount }, { id: 'bluesea', mount: oldBlueMount },
    ] });
    expect(chosen.sensors.find(sensor => sensor.id === 'camera2')!.mount).toEqual({ rail: 'plate-front-right', t: .5 });
    expect(chosen.sensors.find(sensor => sensor.id === 'bluesea')!.mount).toEqual(oldBlueMount);
    expect(sanitizeConfig(chosen)).toEqual(chosen);
    const comparable = baselineUnderCurrentConditions(chosen, chosen);
    expect(comparable.layoutRevision).toBe(CURRENT_LAYOUT_REVISION);
    expect(comparable.sensors).toEqual(chosen.sensors);
    const delta = compareCoverage(computeCoverage(chosen), computeBaselineCoverage(chosen, chosen));
    expect(delta.coverage!.delta).toBe(0);
    expect(delta.blind!.delta).toBe(0);
    expect(delta.overlap!.delta).toBe(0);
  });

  it('keeps the plate bounded to its two attachment levels even in a very short frame', () => {
    const config = sanitizeConfig({ chassis: { frameHeight: .1, frameMidHeights: { front: 99 } } });
    const plate = frameRails(config.chassis).find(rail => rail.id === 'plate-front-right')!;
    expect(cameraPlateGeometry(config.chassis).height).toBeCloseTo(.05);
    expect(plate.end.z).toBe(plate.start.z);
    expect(framePlacement({ x: 9, y: -9, z: -9 }, config.chassis, plate.id)).toMatchObject({ ...plate.start, mount: { rail: plate.id, t: 0 } });
    expect(framePlacement({ x: -9, y: 9, z: 9 }, config.chassis, plate.id)).toMatchObject({ ...plate.end, mount: { rail: plate.id, t: 1 } });
  });
});
