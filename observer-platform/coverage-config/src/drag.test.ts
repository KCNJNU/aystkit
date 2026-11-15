import { describe, expect, it } from 'vitest';
import { constrainDragPosition } from './drag';
import { framePosition, frameRails } from './frame';
import { DEFAULT_CONFIG } from './model';

describe('sensor translation on frame members', () => {
  it('only moves along the selected member and preserves both fixed axes', () => {
    for (const rail of frameRails(DEFAULT_CONFIG.chassis)) {
      const start = framePosition(rail, .3), candidate = { x: .1234, y: -.1234, z: .8765 };
      const result = constrainDragPosition(start, candidate, rail);
      for (const axis of ['x', 'y', 'z'] as const) {
        if (axis !== rail.axis) expect(result[axis]).toBe(rail.start[axis]);
        else {
          const snapped = Number((Math.round(candidate[axis] * 100) / 100).toFixed(2));
          expect(result[axis]).toBe(Math.max(rail.start[axis], Math.min(rail.end[axis], snapped)));
        }
      }
    }
  });
  it('clamps to exact member endpoints including dimensions between centimetres', () => {
    const chassis = { ...DEFAULT_CONFIG.chassis, length: 1.073, frameWidth: .537, height: .257, frameHeight: .913 };
    for (const rail of frameRails(chassis)) {
      const start = framePosition(rail, .5);
      expect(constrainDragPosition(start, { x: -100, y: -100, z: -100 }, rail)).toEqual(rail.start);
      expect(constrainDragPosition(start, { x: 100, y: 100, z: 100 }, rail)).toEqual(rail.end);
      expect(constrainDragPosition(start, rail.start, rail)).toEqual(rail.start);
      expect(constrainDragPosition(start, rail.end, rail)).toEqual(rail.end);
    }
  });
  it('ignores a nonfinite pointer result instead of losing the current placement', () => {
    for (const rail of frameRails(DEFAULT_CONFIG.chassis)) {
      const start = framePosition(rail, .31721);
      const result = constrainDragPosition(start, { x: NaN, y: Infinity, z: -Infinity }, rail);
      expect(Number.isFinite(result[rail.axis])).toBe(true);
      expect(result[rail.axis]).toBe(start[rail.axis]);
      for (const axis of ['x', 'y', 'z'] as const) if (axis !== rail.axis) expect(result[axis]).toBe(rail.start[axis]);
    }
  });
});
