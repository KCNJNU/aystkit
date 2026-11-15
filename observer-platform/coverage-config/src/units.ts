/** Display units only; geometry and serialized configurations keep meters. */
const centimetersPerMeter = 100;
const squareCentimetersPerSquareMeter = 10_000;

export const toCentimeters = (meters: number): number => meters * centimetersPerMeter;
export const fromCentimeters = (centimeters: number): number => centimeters / centimetersPerMeter;

/** User-entered lengths resolve to 0.1 cm. Displaying alone never changes state. */
export function quantizeCentimeters(value: number): number {
  const rounded = Math.sign(value) * Math.round((Math.abs(value) + Number.EPSILON) * 10) / 10;
  return Object.is(rounded, -0) ? 0 : rounded;
}

export function formatLength(meters: number): string {
  return quantizeCentimeters(toCentimeters(meters)).toLocaleString('zh-CN', { maximumFractionDigits: 1, useGrouping: false });
}

export function formatArea(squareMeters: number): string {
  return (squareMeters * squareCentimetersPerSquareMeter).toLocaleString('zh-CN', { maximumFractionDigits: 1 });
}

/** Keep rounded editable values inside precise support boundaries. */
export function centimeterBounds(minimum?: number, maximum?: number): { min?: number; max?: number } {
  return {
    min: minimum === undefined ? undefined : Math.ceil((minimum - 1e-8) * 10) / 10,
    max: maximum === undefined ? undefined : Math.floor((maximum + 1e-8) * 10) / 10,
  };
}
