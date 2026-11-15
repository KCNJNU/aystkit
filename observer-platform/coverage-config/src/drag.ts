import type { FrameRail } from './frame';

/** Keep a sensor on its selected member; motion along that member snaps to a centimetre. */
export interface DragPosition { x: number; y: number; z: number; }
/** Snap layout points in world space, retaining exact cuboid faces at limits. */
export function constrainBoxPosition(start: DragPosition, candidate: DragPosition, min: DragPosition, max: DragPosition): DragPosition {
  const result = { ...start };
  for (const axis of ['x', 'y', 'z'] as const) {
    const minimum = Math.min(min[axis], max[axis]), maximum = Math.max(min[axis], max[axis]);
    const value = Number.isFinite(candidate[axis]) ? candidate[axis] : Number.isFinite(start[axis]) ? start[axis] : minimum;
    if (Number.isFinite(start[axis]) && Math.abs(value - start[axis]) < 1e-12) {
      result[axis] = Math.max(minimum, Math.min(maximum, start[axis]));
      continue;
    }
    result[axis] = value <= minimum ? minimum : value >= maximum ? maximum
      : Math.max(minimum, Math.min(maximum, Number((Math.round(value * 100) / 100).toFixed(2))));
  }
  return result;
}
export function constrainDragPosition(start: DragPosition, candidate: DragPosition, rail: FrameRail): DragPosition {
  const result = { ...rail.start }, axis = rail.axis;
  const minimum = Math.min(rail.start[axis], rail.end[axis]), maximum = Math.max(rail.start[axis], rail.end[axis]);
  if (!Number.isFinite(candidate[axis])) {
    result[axis] = Math.max(minimum, Math.min(maximum, Number.isFinite(start[axis]) ? start[axis] : minimum));
  } else if (candidate[axis] <= minimum) result[axis] = minimum;
  else if (candidate[axis] >= maximum) result[axis] = maximum;
  else result[axis] = Math.max(minimum, Math.min(maximum, Number((Math.round(candidate[axis] * 100) / 100).toFixed(2))));
  return result;
}
