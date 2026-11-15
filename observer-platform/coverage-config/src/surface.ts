import type { CoverageResult } from './geometry';

export interface CoverageSurface {
  /** Two triangles per sampled cell, in the existing vehicle XY coordinates. */
  positions: Float32Array;
  /** Display categories: 0 blind, 1 covered, 2 overlapping coverage. */
  categories: Float32Array;
  /** Unique exact grid edges, ready for Three.js LineSegments. */
  boundaryPositions: Float32Array;
  boundaryCategories: Float32Array;
  /** Disjoint display-category cell counts; their sum is the source cell count. */
  classCounts: { blind: number; covered: number; overlap: number };
}

type Side = 0 | 1 | 2 | 3; // left, right, bottom, top

/**
 * Render the existing sampled rectangles without smoothing or filling holes.
 * Integer grid adjacency avoids floating-point corner keys and quadratic scans.
 * The source result and its numerical coverage statistics are never changed.
 */
export function buildCoverageSurface(result: CoverageResult, z: number, overlap: boolean): CoverageSurface {
  const { cells, cellLength, cellWidth } = result;
  const count = cells.length;
  const positions = new Float32Array(count * 18);
  const categories = new Float32Array(count * 6);
  const classCounts = { blind: 0, covered: 0, overlap: 0 };
  if (count === 0) return { positions, categories, boundaryPositions: new Float32Array(0),
    boundaryCategories: new Float32Array(0), classCounts };

  let minX = Infinity, minY = Infinity, maxX = -Infinity;
  for (const cell of cells) {
    minX = Math.min(minX, cell.x);
    minY = Math.min(minY, cell.y);
    maxX = Math.max(maxX, cell.x);
  }
  const columns = Math.round((maxX - minX) / cellLength) + 1;
  const grid = new Map<number, number>();
  const keys = new Float64Array(count);
  const display = new Uint8Array(count);
  const halfLength = cellLength / 2, halfWidth = cellWidth / 2;
  for (let index = 0; index < count; index += 1) {
    const cell = cells[index];
    const column = Math.round((cell.x - minX) / cellLength);
    const row = Math.round((cell.y - minY) / cellWidth);
    const key = row * columns + column;
    grid.set(key, index);
    keys[index] = key;
    const category = cell.count <= 0 ? 0 : overlap && cell.count > 1 ? 2 : 1;
    display[index] = category;
    if (category === 0) classCounts.blind += 1;
    else if (category === 1) classCounts.covered += 1;
    else classCounts.overlap += 1;
    categories.fill(category, index * 6, index * 6 + 6);
    const x0 = cell.x - halfLength, x1 = cell.x + halfLength;
    const y0 = cell.y - halfWidth, y1 = cell.y + halfWidth;
    // Counterclockwise triangles preserve exactly this sampled cell footprint.
    positions.set([x0, y0, z, x1, y0, z, x1, y1, z,
      x0, y0, z, x1, y1, z, x0, y1, z], index * 18);
  }

  const boundaries = (visit: (index: number, side: Side, category: number) => void) => {
    for (let index = 0; index < count; index += 1) {
      const key = keys[index], column = key % columns, category = display[index];
      const left = column > 0 ? grid.get(key - 1) : undefined;
      const right = column + 1 < columns ? grid.get(key + 1) : undefined;
      const bottom = grid.get(key - columns), top = grid.get(key + columns);
      // Missing neighbours include both the exterior and chassis cutout. Only
      // right/top own a shared classification boundary, so it is emitted once.
      if (left === undefined) visit(index, 0, category);
      if (right === undefined || display[right] !== category) visit(index, 1,
        right === undefined ? category : Math.max(category, display[right]));
      if (bottom === undefined) visit(index, 2, category);
      if (top === undefined || display[top] !== category) visit(index, 3,
        top === undefined ? category : Math.max(category, display[top]));
    }
  };
  let edgeCount = 0;
  boundaries(() => { edgeCount += 1; });
  const boundaryPositions = new Float32Array(edgeCount * 6);
  const boundaryCategories = new Float32Array(edgeCount * 2);
  let edge = 0;
  boundaries((index, side, category) => {
    const cell = cells[index];
    const x0 = cell.x - halfLength, x1 = cell.x + halfLength;
    const y0 = cell.y - halfWidth, y1 = cell.y + halfWidth;
    const coordinates = side === 0 ? [x0, y0, z, x0, y1, z]
      : side === 1 ? [x1, y0, z, x1, y1, z]
        : side === 2 ? [x0, y0, z, x1, y0, z]
          : [x0, y1, z, x1, y1, z];
    boundaryPositions.set(coordinates, edge * 6);
    boundaryCategories.fill(category, edge * 2, edge * 2 + 2);
    edge += 1;
  });
  return { positions, categories, boundaryPositions, boundaryCategories, classCounts };
}
