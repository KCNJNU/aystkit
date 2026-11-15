import { describe, expect, it } from 'vitest';
import { Box3, Mesh, Vector3 } from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { CURRENT_LAYOUT_REVISION, DEFAULT_CONFIG, sanitizeConfig } from './model';
import type { Config, SupportId } from './model';
import { loadBaseline } from './comparison';
import { computeCoverage } from './geometry';
import { BEAM_PROFILE_HALF_HEIGHT, SUPPORT_TOP_HARDWARE_ALLOWANCE, DEFAULT_BEAM_GAP,
  belowBeamOffset, moveSensorWithinSupport, moveSupportAlongRail, supportBracketParts,
  supportClampParts, supportGeometry, supportOpeningDirection } from './mounting';

const hanging = ['camera1', 'bluesea', 'stl19p'] as const;
const vector = (p: { x: number; y: number; z: number }) => new Vector3(p.x, p.y, p.z);
const sensor = (config: Config, id: string) => config.sensors.find(item => item.id === id)!;
const legacy = (): Config => {
  const config = sanitizeConfig(DEFAULT_CONFIG); config.layoutRevision = 7;
  for (const id of hanging) {
    const layout = config.supports![id]!;
    delete layout.beamGap;
    layout.offsetZ = id === 'camera1' ? .06 : .04;
    const optical = sensor(config, id), anchorZ = supportGeometry(config, id).anchor.z;
    optical.z = anchorZ + layout.offsetZ! + (optical.inSupport!.z - .5) * layout.sizeZ!;
  }
  return config;
};

function assertBelow(config: Config, id: SupportId) {
  const box = supportGeometry(config, id), parts = supportBracketParts(box), clamp = supportClampParts(box);
  expect(box.belowBeam).toBe(true);
  expect(box.beamBottomZ).toBeCloseTo(box.anchor.z - BEAM_PROFILE_HALF_HEIGHT, 12);
  expect(box.topEnvelopeZ).toBeCloseTo(box.beamBottomZ - box.beamGap!, 12);
  expect(box.max.z + box.layout.thickness + SUPPORT_TOP_HARDWARE_ALLOWANCE).toBeCloseTo(box.topEnvelopeZ, 12);
  expect(parts.roof.center.z + parts.roof.size.z / 2).toBeLessThanOrEqual(box.beamBottomZ + 1e-12);
  expect(clamp.foot.center.z + clamp.foot.size.z / 2).toBeCloseTo(box.beamBottomZ, 12);
  expect(clamp.direction).toBe(-1);
  for (const arm of parts.arms) {
    expect(arm.start.z).toBeCloseTo(clamp.foot.center.z - clamp.foot.size.z / 2, 12);
    expect(arm.end.z).toBeCloseTo(box.max.z, 12);
  }
  return box;
}

describe('rear camera and planar lidar supports hang below their member', () => {
  it('places the whole default box and roof hardware below the physical 34 mm member', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    expect(CURRENT_LAYOUT_REVISION).toBe(8);
    for (const id of hanging) {
      const box = assertBelow(config, id);
      expect(box.beamGap).toBe(DEFAULT_BEAM_GAP);
      expect(sensor(config, id).z).toBeLessThan(box.beamBottomZ);
      expect(sensor(config, id)).toMatchObject({
        yaw: DEFAULT_CONFIG.sensors.find(item => item.id === id)!.yaw,
        pitch: DEFAULT_CONFIG.sensors.find(item => item.id === id)!.pitch,
      });
    }
    expect(supportGeometry(config, 'bluesea').min.z).toBeGreaterThan(0);
    expect(sensor(config, 'camera1').z).toBeCloseTo(1.05, 12);
    expect(sensor(config, 'bluesea').z).toBeCloseTo(.16, 12);
    expect(sensor(config, 'stl19p').z).toBeCloseTo(.61, 12);
  });

  it('migrates saved revision-seven top-mounted boxes once, keeping custom geometry, optics and local points', () => {
    const old = legacy();
    for (const id of hanging) {
      old.supports![id] = { ...old.supports![id], t: .78, sizeX: .217, sizeY: .139, sizeZ: .183,
        offsetX: -.097, offsetY: -.043, offsetZ: .176, thickness: .013 };
      Object.assign(sensor(old, id), { inSupport: { x: .21, y: .72, z: .35 }, yaw: 13.7,
        pitch: 21.3, minRange: .16, maxRange: 2.73 });
    }
    const saved = structuredClone(old), config = sanitizeConfig(old);
    expect(old).toEqual(saved);
    expect(config.layoutRevision).toBe(8);
    for (const id of hanging) {
      const box = assertBelow(config, id);
      expect(box.layout).toMatchObject({ t: .78, sizeX: .217, sizeY: .139, sizeZ: .183,
        offsetX: -.097, offsetY: -.043, thickness: .013, beamGap: .01 });
      expect(sensor(config, id)).toMatchObject({ inSupport: { x: .21, y: .72, z: .35 }, yaw: 13.7,
        pitch: 21.3, minRange: .16, maxRange: 2.73 });
    }
    for (const before of old.sensors.filter(item => !hanging.includes(item.id as typeof hanging[number]))) {
      expect(sensor(config, before.id)).toEqual(before);
      const key = before.id === 'camera2' || before.id === 'p100r' ? 'front' : before.id as SupportId;
      expect(config.supports![key]).toEqual(old.supports![key]);
    }
    expect(sanitizeConfig(config)).toEqual(config);
    expect(sanitizeConfig(JSON.parse(JSON.stringify(config)))).toEqual(config);
    expect(loadBaseline(JSON.stringify({ version: 1, config: old }), config)).toEqual(config);
  });

  it('retains an already-below legacy distance rather than moving it a second time', () => {
    const old = legacy();
    for (const id of hanging) old.supports![id] = { ...old.supports![id], offsetZ: belowBeamOffset(.1, .008, .037), sizeZ: .1 };
    const config = sanitizeConfig(old);
    for (const id of hanging) {
      const box = assertBelow(config, id);
      expect(box.beamGap).toBeCloseTo(.037, 12);
      expect(box.layout.offsetZ).toBeCloseTo(old.supports![id]!.offsetZ!, 12);
    }
    expect(sanitizeConfig(config)).toEqual(config);
  });

  it('keeps the net gap when changing size, thickness, member height and along-member position', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    const resized = sanitizeConfig({ ...initial, supports: { ...initial.supports,
      camera1: { ...initial.supports!.camera1, sizeZ: .343, thickness: .027, beamGap: .032 },
      bluesea: { ...initial.supports!.bluesea, sizeZ: 1.5, thickness: .1, beamGap: .5 },
      stl19p: { ...initial.supports!.stl19p, sizeZ: .141, thickness: .011, beamGap: .019 } },
      chassis: { ...initial.chassis, frameHeight: initial.chassis.frameHeight + .2,
        frameMidHeights: { ...initial.chassis.frameMidHeights, rear: .55 } } });
    for (const id of hanging) {
      const box = assertBelow(resized, id);
      expect(sensor(resized, id).inSupport).toEqual(sensor(initial, id).inSupport);
      const moved = sanitizeConfig(moveSupportAlongRail(resized, id, box.rail.end));
      expect(moved.supports![id]!.t).toBe(1);
      expect(assertBelow(moved, id).beamGap).toBe(box.beamGap);
    }
    expect(resized.supports!.bluesea!.offsetZ!).toBeLessThan(-.5);
    expect(resized.supports!.camera1!.beamGap).toBe(.032);
    expect(resized.supports!.stl19p!.beamGap).toBe(.019);
  });

  it('restores normal side openings while keeping MID360 facing inward', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    for (const [id, expected] of [
      ['camera1', [-1, 0, 0]], ['bluesea', [1, 0, 0]], ['stl19p', [-1, 0, 0]],
      ['mid360', [-1, 0, 0]], ['camera3', [0, 1, 0]], ['camera4', [0, -1, 0]],
    ] as [SupportId, number[]][]) {
      const parts = supportBracketParts(supportGeometry(config, id));
      expect(vector(parts.openingCenter).sub(vector(parts.backplate.center)).normalize().toArray()).toEqual(expected);
      expect(supportOpeningDirection(supportGeometry(config, id))).toEqual({ x: expected[0], y: expected[1], z: expected[2] });
    }
  });

  it('keeps actual thin and offset support arms below the member, including zero gap', () => {
    const config = sanitizeConfig({ ...sanitizeConfig(DEFAULT_CONFIG), supports: Object.fromEntries(hanging.map(id => [id,
      { sizeX: .04, sizeY: .04, sizeZ: .04, offsetX: .5, offsetY: -.5, thickness: .001, beamGap: 0 }])) });
    for (const id of hanging) {
      const box = assertBelow(config, id);
      for (const arm of supportBracketParts(box).arms) {
        const from = vector(arm.start), to = vector(arm.end), direction = to.clone().sub(from), length = direction.length();
        const geometry = new RoundedBoxGeometry(box.layout.thickness, length, box.layout.thickness, 1, box.layout.thickness * .2);
        const mesh = new Mesh(geometry);
        mesh.position.copy(from).add(to).multiplyScalar(.5);
        mesh.quaternion.setFromUnitVectors(new Vector3(0, 1, 0), direction.normalize());
        const actual = new Box3().setFromObject(mesh);
        expect(actual.max.z).toBeLessThanOrEqual(box.beamBottomZ + 1e-7);
        geometry.dispose();
      }
    }
  });

  it('retains custom pedestal style and clips point dragging to the hanging cuboid', () => {
    const config = sanitizeConfig({ ...sanitizeConfig(DEFAULT_CONFIG), supports: Object.fromEntries(hanging.map(id => [id,
      { style: 'pedestal', beamGap: .02 }])) });
    for (const id of hanging) {
      const box = assertBelow(config, id), before = sensor(config, id);
      expect(box.layout.style).toBe('pedestal');
      const moved = sanitizeConfig(moveSensorWithinSupport(config, id, { x: 100, y: -100, z: 100 }));
      expect(sensor(moved, id)).toMatchObject({ x: box.max.x, y: box.min.y, z: box.max.z,
        inSupport: { x: 1, y: 0, z: 1 }, yaw: before.yaw, pitch: before.pitch, hfov: before.hfov });
      expect(moved.supports).toEqual(config.supports);
    }
    const result = computeCoverage(config);
    expect(Number.isFinite(result.percent)).toBe(true);
  });

  it('bounds user gap input but never lets an explicit Z override defeat hanging', () => {
    const initial = sanitizeConfig(DEFAULT_CONFIG);
    for (const [gap, expected] of [[-1, 0], [2, .5], [.023, .023]] as const) {
      const config = sanitizeConfig({ ...initial, supports: { ...initial.supports,
        camera1: { ...initial.supports!.camera1, offsetZ: 99, beamGap: gap } } });
      expect(assertBelow(config, 'camera1').beamGap).toBe(expected);
    }
  });
});
