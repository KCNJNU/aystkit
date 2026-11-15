import { describe, expect, it } from 'vitest';
import { computeCoverage } from './geometry';
import type { CoverageResult } from './geometry';
import { DEFAULT_CONFIG, sanitizeConfig } from './model';
import { buildCoverageSurface } from './surface';

function result(cells: CoverageResult['cells'], cellLength = 1, cellWidth = 1): CoverageResult {
  const covered = cells.filter(cell => cell.count > 0).length;
  const overlap = cells.filter(cell => cell.count > 1).length;
  return { cells, total: cells.length, covered, overlap, blind: cells.length - covered,
    area: cells.length * cellLength * cellWidth, blindArea: (cells.length - covered) * cellLength * cellWidth,
    percent: cells.length === 0 ? 0 : covered / cells.length * 100,
    perSensor: {}, step: .123, cellLength, cellWidth };
}

function triangleArea(positions: Float32Array): number {
  let area = 0;
  for (let index = 0; index < positions.length; index += 9) {
    const ax = positions[index + 3] - positions[index], ay = positions[index + 4] - positions[index + 1];
    const bx = positions[index + 6] - positions[index], by = positions[index + 7] - positions[index + 1];
    const cross = ax * by - ay * bx;
    expect(cross).toBeGreaterThan(0);
    area += cross / 2;
  }
  return area;
}

function segments(positions: Float32Array, categories: Float32Array): string[] {
  const values: string[] = [];
  for (let index = 0; index < positions.length; index += 6) {
    const ends = [Array.from(positions.subarray(index, index + 3)).join(','),
      Array.from(positions.subarray(index + 3, index + 6)).join(',')].sort();
    expect(categories[index / 3]).toBe(categories[index / 3 + 1]);
    values.push(`${ends[0]}|${ends[1]}:${categories[index / 3]}`);
  }
  return values.sort();
}

describe('exact sampled coverage rendering surface', () => {
  it('preserves existing rectangular area and noninteger cell dimensions instead of using the sampling step', () => {
    const config = sanitizeConfig({ ...DEFAULT_CONFIG,
      analysis: { ...DEFAULT_CONFIG.analysis, length: 2.03, width: 1.27, resolution: .1 },
      sensors: DEFAULT_CONFIG.sensors.map(sensor => ({ ...sensor, enabled: false })),
    });
    const coverage = computeCoverage(config), z = .273;
    const surface = buildCoverageSurface(coverage, z, true);
    expect(surface.positions).toBeInstanceOf(Float32Array);
    expect(surface.categories).toBeInstanceOf(Float32Array);
    expect(surface.positions.length).toBe(coverage.cells.length * 18);
    expect(triangleArea(surface.positions)).toBeCloseTo(coverage.area, 5);
    for (let index = 0; index < coverage.cells.length; index += 1) {
      const cell = coverage.cells[index], offset = index * 18;
      expect(surface.positions[offset]).toBeCloseTo(cell.x - coverage.cellLength / 2, 6);
      expect(surface.positions[offset + 1]).toBeCloseTo(cell.y - coverage.cellWidth / 2, 6);
      expect(surface.positions[offset + 3]).toBeCloseTo(cell.x + coverage.cellLength / 2, 6);
      expect(surface.positions[offset + 7]).toBeCloseTo(cell.y + coverage.cellWidth / 2, 6);
      for (let vertex = 0; vertex < 6; vertex += 1) expect(surface.positions[offset + vertex * 3 + 2]).toBeCloseTo(z, 7);
    }
    expect(surface.classCounts).toEqual({ blind: coverage.blind, covered: 0, overlap: 0 });
  });

  it('leaves the chassis hole empty and traces both its exact edges and the exterior', () => {
    const cells: CoverageResult['cells'] = [];
    for (let y = -1; y <= 1; y += 1) for (let x = -1; x <= 1; x += 1) {
      if (x !== 0 || y !== 0) cells.push({ x, y, count: 1 });
    }
    const surface = buildCoverageSurface(result(cells), .2, true);
    expect(surface.positions.length).toBe(8 * 18);
    expect(triangleArea(surface.positions)).toBe(8);
    expect(surface.boundaryPositions.length / 6).toBe(16); // 12 outer + 4 hole edges.
    const lines = segments(surface.boundaryPositions, surface.boundaryCategories);
    expect(new Set(lines).size).toBe(lines.length);
    for (const line of [
      '-0.5,-0.5', '-0.5,0.5', '0.5,-0.5', '0.5,0.5',
    ]) expect(lines.some(segment => segment.includes(line))).toBe(true);
    for (let index = 0; index < surface.positions.length; index += 18) {
      const xs = [surface.positions[index], surface.positions[index + 3], surface.positions[index + 6]];
      const ys = [surface.positions[index + 1], surface.positions[index + 4], surface.positions[index + 7]];
      expect(Math.min(...xs) >= .5 || Math.max(...xs) <= -.5 || Math.min(...ys) >= .5 || Math.max(...ys) <= -.5).toBe(true);
    }
  });

  it('omits same-category interior edges and emits a shared class boundary once with its higher class', () => {
    const cells = [{ x: .5, y: .5, count: 1 }, { x: 1.5, y: .5, count: 2 }];
    const source = result(cells), shown = buildCoverageSurface(source, 0, true);
    expect(shown.classCounts).toEqual({ blind: 0, covered: 1, overlap: 1 });
    expect(shown.categories).toEqual(new Float32Array([1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2]));
    const boundaries = segments(shown.boundaryPositions, shown.boundaryCategories);
    expect(boundaries).toHaveLength(7);
    expect(boundaries.filter(line => line.startsWith('1,0,0|1,1,0:'))).toEqual(['1,0,0|1,1,0:2']);
    const hidden = buildCoverageSurface(source, 0, false);
    expect(hidden.classCounts).toEqual({ blind: 0, covered: 2, overlap: 0 });
    expect(hidden.categories.every(category => category === 1)).toBe(true);
    expect(hidden.boundaryPositions.length / 6).toBe(6);
    expect(segments(hidden.boundaryPositions, hidden.boundaryCategories).some(line => line.startsWith('1,0,0|1,1,0:'))).toBe(false);
    expect(hidden.positions).toEqual(shown.positions);
    expect(source).toEqual(result(cells));
  });

  it('keeps blind/covered transitions and sparse row boundaries stable when cell order changes', () => {
    const cells = [{ x: .5, y: .5, count: 0 }, { x: 1.5, y: .5, count: 1 },
      { x: 1.5, y: 1.5, count: 2 }];
    const source = result(cells), surface = buildCoverageSurface(source, .4, true);
    const reversed = buildCoverageSurface(result([...cells].reverse()), .4, true);
    const lines = segments(surface.boundaryPositions, surface.boundaryCategories);
    expect(lines).toHaveLength(10); // Eight exterior edges plus two class interfaces.
    expect(new Set(lines).size).toBe(lines.length);
    expect(segments(reversed.boundaryPositions, reversed.boundaryCategories)).toEqual(lines);
    const diagonal = buildCoverageSurface(result([cells[0], cells[2]]), 0, false);
    expect(diagonal.boundaryPositions.length / 6).toBe(8);
    expect(buildCoverageSurface(source, .4, false).boundaryPositions.length / 6).toBe(9);
  });

  it('does not change frozen input cells or numerical coverage statistics when overlap display changes', () => {
    const source = result([{ x: .17, y: .23, count: 0 }, { x: .27, y: .23, count: 3 }], .1, .14);
    const snapshot = JSON.stringify(source);
    source.cells.forEach(Object.freeze);
    Object.freeze(source.cells);
    Object.freeze(source.perSensor);
    Object.freeze(source);
    const shown = buildCoverageSurface(source, .3125, true), hidden = buildCoverageSurface(source, .3125, false);
    expect(shown.classCounts).toEqual({ blind: 1, covered: 0, overlap: 1 });
    expect(hidden.classCounts).toEqual({ blind: 1, covered: 1, overlap: 0 });
    expect(JSON.stringify(source)).toBe(snapshot);
    expect(triangleArea(shown.positions)).toBeCloseTo(source.area, 7);
    expect(shown.positions).toEqual(hidden.positions);
  });

  it('returns finite empty typed arrays for an entirely excluded analysis area', () => {
    const empty = buildCoverageSurface(result([], 0, 0), .2, true);
    expect(empty.classCounts).toEqual({ blind: 0, covered: 0, overlap: 0 });
    for (const array of [empty.positions, empty.categories, empty.boundaryPositions, empty.boundaryCategories]) {
      expect(array).toBeInstanceOf(Float32Array);
      expect(array.length).toBe(0);
    }
  });

  it('handles the maximum 300 by 300 grid with bounded arrays and one copy of every visible edge', () => {
    const cells: CoverageResult['cells'] = [];
    for (let row = 0; row < 300; row += 1) for (let column = 0; column < 300; column += 1) {
      cells.push({ x: -6 + (column + .5) * .04, y: -6 + (row + .5) * .04, count: (row + column) % 3 });
    }
    const surface = buildCoverageSurface(result(cells, .04, .04), .2, true);
    expect(surface.classCounts).toEqual({ blind: 30_000, covered: 30_000, overlap: 30_000 });
    expect(surface.positions.length).toBe(90_000 * 18);
    expect(surface.categories.length).toBe(90_000 * 6);
    // Every adjacent cell differs: two grid orientations plus the exterior.
    expect(surface.boundaryPositions.length / 6).toBe(2 * 300 * 299 + 4 * 300);
    expect(surface.boundaryCategories.length).toBe(surface.boundaryPositions.length / 3);
    expect(surface.boundaryPositions.length).toBeLessThanOrEqual(90_000 * 4 * 6);
    for (const array of [surface.positions, surface.categories, surface.boundaryPositions, surface.boundaryCategories]) {
      expect(array.every(Number.isFinite)).toBe(true);
    }
  });
});
