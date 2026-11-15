import { datasetRobotScope } from './dataset-robot-scope';
import { DatasetsService } from './datasets.service';
import { SearchService } from '../search/search.service';
const robotId='00000000-0000-4000-8000-000000000002';
const actor={id:'actor',role:'OPERATOR',tenantId:'tenant-a',projectIds:['project-a']} as never;
describe('robot dataset classification retains tenant/project ACL',()=>{
  it('finds metadata-only empty archive datasets and files/versions from a robot',()=>{
    expect(datasetRobotScope(robotId)).toEqual({OR:[{metadata:{path:['robotId'],equals:robotId}},{files:{some:{robotId}}},{versions:{some:{files:{some:{robotFile:{robotId}}}}}}]});
    expect(datasetRobotScope()).toEqual({});
  });
  it('combines keyword OR, project scope and robot constraint without overwriting any',async()=>{
    const dataset={findMany:jest.fn().mockResolvedValue([]),count:jest.fn().mockResolvedValue(0)};
    const p={dataset,$transaction:jest.fn(values=>Promise.all(values))};
    await new DatasetsService(p as never,{} as never).page({page:1,pageSize:20,sortBy:'updatedAt',sortOrder:'desc',search:'车库',robotId},actor);
    expect(dataset.findMany).toHaveBeenCalledWith(expect.objectContaining({where:expect.objectContaining({tenantId:'tenant-a',projectId:{in:['project-a']},AND:[datasetRobotScope(robotId)],OR:expect.any(Array)})}));
  });
  it('also scopes global search files, datasets and versions',async()=>{
    const p={robotFile:{findMany:jest.fn().mockResolvedValue([])},dataset:{findMany:jest.fn().mockResolvedValue([])},datasetVersion:{findMany:jest.fn().mockResolvedValue([])}};
    await new SearchService(p as never).search('test',5,actor,robotId);
    expect(p.robotFile.findMany).toHaveBeenCalledWith(expect.objectContaining({where:expect.objectContaining({robotId,robot:expect.objectContaining({tenantId:'tenant-a'})})}));
    expect(p.dataset.findMany).toHaveBeenCalledWith(expect.objectContaining({where:expect.objectContaining({AND:[datasetRobotScope(robotId)],tenantId:'tenant-a'})}));
    expect(p.datasetVersion.findMany).toHaveBeenCalledWith(expect.objectContaining({where:expect.objectContaining({dataset:expect.objectContaining({AND:[datasetRobotScope(robotId)],tenantId:'tenant-a'})})}));
  });
});
