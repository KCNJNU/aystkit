import { describe, expect, it } from 'vitest';
import { constrainBoxPosition } from './drag';

describe('cuboid layout dragging', () => {
  const min = { x: -.517, y: .258, z: .743 }, max = { x: -.337, y: .378, z: .863 };
  const start = { x: -.42, y: .31, z: .80 };
  it('allows independent world XYZ movement inside the same cuboid', () => {
    expect(constrainBoxPosition(start, { x: -.411, y: .326, z: .828 }, min, max)).toEqual({ x: -.41, y: .33, z: .83 });
  });
  it('retains exact non-centimetre faces and corners when dragged outside', () => {
    expect(constrainBoxPosition(start, { x: -10, y: 10, z: -10 }, min, max)).toEqual({ x: min.x, y: max.y, z: min.z });
    expect(constrainBoxPosition(start, max, min, max)).toEqual(max);
  });
  it('cannot snap past a face even when the nearest centimetre lies outside', () => {
    expect(constrainBoxPosition(start, { x: -.338, y: .377, z: .862 }, min, max)).toEqual({ x: -.34, y: .378, z: .86 });
  });
  it('uses the original finite coordinate for invalid pointer components', () => {
    expect(constrainBoxPosition(start, { x: NaN, y: Infinity, z: -Infinity }, min, max)).toEqual(start);
  });
  it('does not round untouched axes when dragging a single gizmo handle', () => {
    const precise = { x: -.4135, y: .32575, z: .800125 };
    expect(constrainBoxPosition(precise, { ...precise, x: -.401 }, min, max)).toEqual({ x: -.40, y: precise.y, z: precise.z });
  });
});
