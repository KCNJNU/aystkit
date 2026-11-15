import type { Config } from './model';

export interface RecommendationPreviewPoint {
  id: string;
  x: number;
  y: number;
  z: number;
  yaw: number;
  pitch: number;
  translated: boolean;
  rotated: boolean;
  current: { x: number; y: number; z: number; yaw: number; pitch: number };
}

const angularDistance = (a: number, b: number) => Math.abs(((a - b + 540) % 360) - 180);

/** Preview metadata includes pure rotation and never promotes other devices. */
export function sideRecommendationPreview(config: Config, recommendation?: Config | null): RecommendationPreviewPoint[] {
  if (!recommendation) return [];
  const result: RecommendationPreviewPoint[] = [];
  for (const id of ['camera3', 'camera4']) {
    const actual = config.sensors.find(sensor => sensor.id === id);
    const proposed = recommendation.sensors.find(sensor => sensor.id === id);
    if (!actual || !proposed) continue;
    const translated = Math.hypot(proposed.x - actual.x, proposed.y - actual.y, proposed.z - actual.z) > 1e-9;
    const rotated = angularDistance(proposed.yaw, actual.yaw) > 1e-8 || Math.abs(proposed.pitch - actual.pitch) > 1e-8;
    if (!translated && !rotated) continue;
    result.push({ id, x: proposed.x, y: proposed.y, z: proposed.z, yaw: proposed.yaw, pitch: proposed.pitch,
      translated, rotated, current: { x: actual.x, y: actual.y, z: actual.z, yaw: actual.yaw, pitch: actual.pitch } });
  }
  return result;
}
