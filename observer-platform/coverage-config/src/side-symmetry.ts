import type { Config } from './model';
import { supportGeometry } from './mounting';

export const SIDE_CAMERA_IDS = ['camera3', 'camera4'] as const;
export type SideAlignmentOffsets = Record<typeof SIDE_CAMERA_IDS[number], number>;

/** Initial alignment is separate from the equal translation of the aligned pair. */
export function sidePositionReference(config: Config): { commonReferenceXCm: number; alignmentOffsetsCm: SideAlignmentOffsets } {
  const centers = SIDE_CAMERA_IDS.map(id => supportGeometry(config, id).center.x);
  const center = (centers[0] + centers[1]) / 2;
  return { commonReferenceXCm: center * 100,
    alignmentOffsetsCm: { camera3: (center - centers[0]) * 100, camera4: (center - centers[1]) * 100 } };
}

/** Symmetry concerns box X and viewing angles; the user's points inside boxes stay fixed. */
export function sideLayoutIsSymmetric(config: Config): boolean {
  try {
    const left = config.sensors.find(sensor => sensor.id === 'camera3');
    const right = config.sensors.find(sensor => sensor.id === 'camera4');
    if (!left || !right || ![left.yaw, left.pitch, right.yaw, right.pitch].every(Number.isFinite)) return false;
    const boxes = SIDE_CAMERA_IDS.map(id => supportGeometry(config, id));
    const mirroredYaw = Math.abs(((left.yaw + right.yaw + 540) % 360) - 180);
    return boxes.every(box => box.rail.axis === 'x') && Math.abs(boxes[0].center.x - boxes[1].center.x) <= 1e-8 &&
      mirroredYaw <= 1e-7 && Math.abs(left.pitch - right.pitch) <= 1e-7;
  } catch {
    return false;
  }
}
