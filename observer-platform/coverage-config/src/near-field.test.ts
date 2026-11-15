import { describe,expect,it } from 'vitest';
import { DEFAULT_CONFIG,sanitizeConfig } from './model';
import { computeCoverage } from './geometry';
import { computeNearFieldMetrics,nearFieldFlags } from './near-field';

describe('side/rear near-field definition',()=>{
  const config=sanitizeConfig({...DEFAULT_CONFIG,chassis:{...DEFAULT_CONFIG.chassis,length:2,width:1}});
  it('uses Euclidean distance from the footprint and excludes the forward sector',()=>{
    expect(nearFieldFlags(config,{x:-1.6,y:1.3}).near).toBe(true);
    expect(nearFieldFlags(config,{x:-1.8,y:1.3}).near).toBe(false);
    expect(nearFieldFlags(config,{x:1.1,y:.8}).near).toBe(false);
    expect(nearFieldFlags(config,{x:0,y:0}).near).toBe(false);
  });
  it('protects side strips, their rear halves, and the middle rear independently',()=>{
    expect(nearFieldFlags(config,{x:.2,y:.8})).toMatchObject({near:true,left:true,leftRear:false,rear:false});
    expect(nearFieldFlags(config,{x:-.2,y:.8})).toMatchObject({near:true,left:true,leftRear:true,rear:false});
    expect(nearFieldFlags(config,{x:-1.2,y:0})).toMatchObject({near:true,left:false,right:false,rear:true});
    expect(nearFieldFlags(config,{x:-1.2,y:-.8})).toMatchObject({near:true,right:true,rightRear:true,rear:true});
  });
  it('recomputes counts and areas from the original grid without double-counting overlaps',()=>{
    const coverage=computeCoverage(config),metrics=computeNearFieldMetrics(config,coverage);
    const cells=coverage.cells.filter(cell=>nearFieldFlags(config,cell).near);
    expect(metrics.totalCells).toBe(cells.length);
    expect(metrics.covered).toBe(cells.filter(cell=>cell.count>0).length);
    expect(metrics.blindArea).toBeCloseTo((metrics.totalCells-metrics.covered)*coverage.cellLength*coverage.cellWidth,12);
    expect(metrics.left.totalCells+metrics.right.totalCells+metrics.rear.totalCells).toBeGreaterThan(metrics.totalCells);
  });
});
