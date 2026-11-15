import { describe, expect, it } from 'vitest';
import { centimeterBounds, formatArea, formatLength, fromCentimeters, quantizeCentimeters, toCentimeters } from './units';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { computeCoverage } from './geometry';

describe('centimeter presentation and meter-valued geometry', () => {
  it('converts physical dimensions in both directions without rounding stored measurements', () => {
    expect(toCentimeters(1)).toBe(100);
    expect(toCentimeters(.008)).toBe(.8);
    expect(fromCentimeters(.8)).toBe(.008);
    expect(fromCentimeters(.1)).toBe(.001);
    const measured = .33862331;
    expect(toCentimeters(measured)).toBeCloseTo(33.862331, 8);
    expect(fromCentimeters(toCentimeters(measured))).toBeCloseTo(measured, 12);
  });

  it('shows at most one decimal for positive, negative and submillimeter positions', () => {
    expect(formatLength(.33862331)).toBe('33.9');
    expect(formatLength(-.33862331)).toBe('-33.9');
    expect(formatLength(.008)).toBe('0.8');
    expect(formatLength(3)).toBe('300');
    expect(formatLength(-.0003)).toBe('0');
    expect(quantizeCentimeters(-12.35)).toBe(-12.4);
    expect(quantizeCentimeters(12.35)).toBe(12.4);
  });

  it('keeps one-decimal edits inside exact support bounds on either side of the origin', () => {
    const min = -33.233, max = 27.866;
    const limits = centimeterBounds(min, max);
    expect(limits).toEqual({ min: -33.2, max: 27.8 });
    expect(fromCentimeters(limits.min!)).toBeGreaterThanOrEqual(fromCentimeters(min));
    expect(fromCentimeters(limits.max!)).toBeLessThanOrEqual(fromCentimeters(max));
    expect(centimeterBounds()).toEqual({ min: undefined, max: undefined });
  });

  it('scales square units by 10000 and groups readable centimeter area values', () => {
    expect(formatArea(1)).toBe('10,000');
    expect(formatArea(.123456)).toBe('1,234.6');
    expect(formatArea(.000001)).toBe('0');
  });

  it('leaves saved meter configuration, calibrated positions and coverage unchanged when displaying it', () => {
    const config = sanitizeConfig(DEFAULT_CONFIG);
    const before = JSON.stringify(config);
    const coverage = computeCoverage(config);
    for (const sensor of config.sensors) {
      [sensor.x, sensor.y, sensor.z, sensor.minRange, sensor.maxRange].forEach(formatLength);
    }
    formatArea(coverage.area);
    formatArea(coverage.blindArea);
    expect(JSON.stringify(config)).toBe(before);
    expect(computeCoverage(JSON.parse(before))).toEqual(coverage);
  });
});
