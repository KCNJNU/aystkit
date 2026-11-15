import { beforeEach,describe,expect,it,vi } from 'vitest';
describe('per-tab robot context',()=>{
 beforeEach(()=>{sessionStorage.clear();vi.resetModules();window.history.replaceState({},'', '/datasets');});
 it('retains the observer handoff across navigation and scopes every list',async()=>{
  const id='00000000-0000-4000-8000-000000000002';window.history.replaceState({},'','/datasets?robotId='+id);
  const s=await import('./robot-selection');expect(s.selectedRobot()).toBe(id);
  window.history.replaceState({},'','/files');expect(s.selectedRobot()).toBe(id);
  for(const path of ['/v1/datasets/page?page=1','/v1/datasets/versions/page','/v1/files','/v1/search?q=a'])expect(s.robotQuery(path)).toContain('robotId='+id);
  expect(s.robotQuery('/v1/auth/session')).toBe('/v1/auth/session');expect(s.robotQuery('/v1/files/file-id/download')).toBe('/v1/files/file-id/download');
  s.selectRobot('');expect(s.robotQuery('/v1/files')).toBe('/v1/files');
 });
 it('does not accept invalid IDs or switch a tab through localStorage',async()=>{
  window.history.replaceState({},'','/datasets?robotId=foreign/path');localStorage.setItem('warehouse-robot-id','another');
  const s=await import('./robot-selection');expect(s.selectedRobot()).toBe('');s.selectRobot('invalid');expect(s.selectedRobot()).toBe('');
 });
});


describe('Observer return context', () => {
 beforeEach(()=>{sessionStorage.clear();vi.resetModules();window.history.replaceState({},'', '/datasets');});
 it('retains the explicit Observer alias across routes and never substitutes a legacy external ID', async()=>{
  const id='00000000-0000-4000-8000-000000000001';
  window.history.replaceState({},'', '/datasets?robotId='+id+'&observerRobot=rk3588-robot');
  const s=await import('./robot-selection');
  expect(s.selectedRobot()).toBe(id);
  window.history.replaceState({},'', '/files');
  expect(s.observerRobotUrl(id)).toBe('/?robot=rk3588-robot');
  expect(s.robotPath('/files',id)).toContain('observerRobot=rk3588-robot');
  expect(s.observerRobotUrl('00000000-0000-4000-8000-000000000002',[])).toBeUndefined();
 });
 it('uses only the explicit registered Observer alias when selecting a new car', async()=>{
  const id='00000000-0000-4000-8000-000000000002';const s=await import('./robot-selection');
  expect(s.observerRobotUrl(id,[{id,observerId:'car-02'}])).toBe('/?robot=car-02');
  expect(s.observerRobotUrl(id,[{id,observerId:'../wrong'}])).toBeUndefined();
  expect(s.robotFromSearch('?robotId=all')).toBe('');
  expect(s.robotPath('/datasets','')).toContain('robotId=all');
 });
});
