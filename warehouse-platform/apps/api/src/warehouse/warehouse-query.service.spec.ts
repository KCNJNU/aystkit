import { WarehouseQueryService, warehouseScope } from './warehouse-query.service';
import { AuthUser } from '../auth/auth.types';
import { ConfigService } from '@nestjs/config';

const user: AuthUser = { id: 'user', email: 'user@example.test', displayName: 'User', role: 'PROJECT_ADMIN', tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };

describe('WarehouseQueryService scope', () => {
  it('lists enrolled physical cars and the explicit legacy car within project permissions', async () => {
    const prisma = { robot: { findMany: jest.fn().mockResolvedValue([]) } };
    const service = new WarehouseQueryService(prisma as never, new ConfigService({ WAREHOUSE_ROBOT_IDS: 'legacy-car' }));
    await service.robots(user);
    expect(prisma.robot.findMany).toHaveBeenCalledWith(expect.objectContaining({where: {
      ...warehouseScope(user), lifecycleStatus: 'ACTIVE', OR: [
        { metadata: { path: ['type'], equals: 'fleet-robot' } }, { id: { in: ['legacy-car'] } },
      ],
    }}));
  });
  it('returns only enrolled Observer aliases and never leaks metadata or guesses legacy aliases', async () => {
    const prisma = { robot: { findMany: jest.fn().mockResolvedValue([
      { id: 'legacy', externalId: 'garage-native-ipc', name: 'Old', onlineStatus: 'OFFLINE', metadata: { type: 'legacy', secret: 'hidden' } },
      { id: 'new', externalId: 'car-02', name: 'New', onlineStatus: 'OFFLINE', metadata: { type: 'fleet-robot', observerId: 'car-02', secret: 'hidden' } },
      { id: 'invalid', externalId: 'wrong', name: 'Bad', onlineStatus: 'OFFLINE', metadata: { type: 'fleet-robot', observerId: '../bad' } },
    ]) } };
    expect(await new WarehouseQueryService(prisma as never).robots(user)).toEqual([
      { id: 'legacy', externalId: 'garage-native-ipc', name: 'Old', onlineStatus: 'OFFLINE' },
      { id: 'new', externalId: 'car-02', name: 'New', onlineStatus: 'OFFLINE', observerId: 'car-02' },
      { id: 'invalid', externalId: 'wrong', name: 'Bad', onlineStatus: 'OFFLINE' },
    ]);
  });
  it('refuses registration by project users and conflicting identities', async () => {
    const prisma = { project: { findFirst: jest.fn().mockResolvedValue({id:'project',tenantId:'tenant'}) },
      robot: { findFirst: jest.fn().mockResolvedValue({id:'other',externalId:'car-02',projectId:'project'}),create:jest.fn() } };
    const service = new WarehouseQueryService(prisma as never);
    const input = {id:'new',projectId:'project',externalId:'car-02',name:'二号车'};
    await expect(service.registerRobot(input,user)).rejects.toThrow();
    expect(prisma.project.findFirst).not.toHaveBeenCalled();
    await expect(service.registerRobot(input,{...user,role:'ADMIN'})).rejects.toThrow('已绑定');
    expect(prisma.robot.create).not.toHaveBeenCalled();
  });
  it('keeps registration retries minimal and never silently promotes a virtual source', async () => {
    const existing = {id:'new',projectId:'project',externalId:'car-02',name:'二号车',onlineStatus:'OFFLINE',lifecycleStatus:'ACTIVE',metadata:{type:'fleet-robot'}};
    const prisma = {project:{findFirst:jest.fn().mockResolvedValue({id:'project',tenantId:'tenant'})},
      robot:{findFirst:jest.fn().mockResolvedValue(existing),create:jest.fn()}};
    const service = new WarehouseQueryService(prisma as never);
    const input = {id:'new',projectId:'project',externalId:'car-02',name:'二号车'};
    expect(await service.registerRobot(input,{...user,role:'ADMIN'})).toEqual({id:'new',externalId:'car-02',name:'二号车',onlineStatus:'OFFLINE'});
    existing.metadata.type='warehouse-dataset';
    await expect(service.registerRobot(input,{...user,role:'ADMIN'})).rejects.toThrow('已绑定');
    expect(prisma.robot.create).not.toHaveBeenCalled();
  });
  it('fails closed without an authenticated principal or project access', () => {
    expect(() => warehouseScope(undefined as never)).toThrow();
    expect(warehouseScope({ ...user, tenantId: null, projectIds: [] })).toEqual({ tenantId: '00000000-0000-0000-0000-000000000000', projectId: { in: [] } });
  });

  it('excludes deleting datasets and scopes warehouse file statistics', async () => {
    const prisma = {
      dataset: { count: jest.fn().mockResolvedValue(2), findMany: jest.fn().mockResolvedValue([]) },
      datasetVersion: { count: jest.fn().mockResolvedValue(3) },
      robotFile: { count: jest.fn().mockResolvedValue(4), aggregate: jest.fn().mockResolvedValue({ _sum: { fileSize: 10n } }), findMany: jest.fn().mockResolvedValue([]) },
    };
    const result = await new WarehouseQueryService(prisma as never).overview(user);
    expect(result.summary).toMatchObject({ dataset_count: 2, version_count: 3, file_count: 4, managed_bytes: '10' });
    expect(prisma.dataset.count).toHaveBeenCalledWith({ where: { deletingAt: null, tenantId: 'tenant', projectId: { in: ['project'] } } });
    expect(prisma.robotFile.count).toHaveBeenCalledWith({ where: { robot: warehouseScope(user) } });
    expect(prisma.datasetVersion.count).toHaveBeenCalledWith({ where: { dataset: { deletingAt: null, ...warehouseScope(user) } } });
  });
});
