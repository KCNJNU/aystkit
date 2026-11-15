import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from './api';
import { selectRobot } from './robot-selection';
const a='00000000-0000-4000-8000-000000000001',b='00000000-0000-4000-8000-000000000002';
beforeEach(()=>vi.stubGlobal('fetch',vi.fn().mockImplementation(async()=>new Response('{}',{status:201,headers:{'Content-Type':'application/json'}}))));
afterEach(()=>{vi.unstubAllGlobals();selectRobot('');});
function body(){return JSON.parse(vi.mocked(fetch).mock.calls[0][1]!.body as string);}
it('uses current category for creations from every entry point and keeps explicit selection authoritative',async()=>{
 selectRobot(a);await api.createDataset({name:'new'});expect(body()).toEqual({name:'new',robotId:a});
 vi.mocked(fetch).mockClear();await api.createDataset({name:'other',robotId:b});expect(body()).toEqual({name:'other',robotId:b});
});
it('retains the unbound legacy API shape when viewing all robots',async()=>{
 selectRobot('');await api.createDataset({name:'shared'});expect(body()).toEqual({name:'shared'});
});
