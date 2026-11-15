import { ConfigService } from '@nestjs/config';
import { mkdtemp,readFile,rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ArchiveService } from './archive.service';
describe('per robot archive gates fail closed',()=>{
 let root:string;
 beforeEach(async()=>{root=await mkdtemp(join(tmpdir(),'robot-gates-'));});
 afterEach(async()=>{await rm(root,{force:true,recursive:true});});
 it('writes separate gates and leaves the first car unchanged on other selection',async()=>{
   const s=new ArchiveService(new ConfigService({GARAGE_ARCHIVE_ROBOTS_JSON:JSON.stringify({a:join(root,'a'),b:join(root,'b')})}));
   await s.setGarage(true,'actor','a');await s.setGarage(false,'actor','b');
   expect((await s.status('a')).webEnabled).toBe(true);expect((await s.status('b')).webEnabled).toBe(false);
   expect(JSON.parse(await readFile(join(root,'a/garage.json'),'utf8')).enabled).toBe(true);
 });
 it('never falls back to the first car for an unknown robot',async()=>{
   const s=new ArchiveService(new ConfigService({GARAGE_ARCHIVE_CONTROL_DIR:root,GARAGE_ARCHIVE_DEFAULT_ROBOT_ID:'a'}));
   await s.setGarage(true,'actor','a');
   expect((await s.status('b')).configured).toBe(false);
   await expect(s.setGarage(false,'actor','b')).rejects.toThrow();
   expect((await s.status('a')).webEnabled).toBe(true);
 });
 it('refuses shared paths between two robot identities',async()=>{
   const s=new ArchiveService(new ConfigService({GARAGE_ARCHIVE_ROBOTS_JSON:JSON.stringify({a:root,b:root})}));
   expect(() => s.status('a')).toThrow('多机器人');
 });
 it('requires a selection before fleet gate writes, retaining the legacy gate',async()=>{
   const s=new ArchiveService(new ConfigService({GARAGE_ARCHIVE_CONTROL_DIR:root,
     GARAGE_ARCHIVE_ROBOTS_JSON:JSON.stringify({b:join(root,'b')})}));
   expect((await s.status()).configured).toBe(false);
   await expect(s.setGarage(true,'actor')).rejects.toThrow();
 });
 it('refuses assigning a second car the existing default car control directory',()=>{
   const s=new ArchiveService(new ConfigService({GARAGE_ARCHIVE_CONTROL_DIR:root,
     GARAGE_ARCHIVE_DEFAULT_ROBOT_ID:'a',GARAGE_ARCHIVE_ROBOTS_JSON:JSON.stringify({b:root})}));
   expect(()=>s.status('b')).toThrow('多机器人');
 });
});
