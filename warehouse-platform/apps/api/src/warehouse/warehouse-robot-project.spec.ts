import { AuthUser } from '../auth/auth.types';
import { WarehouseQueryService } from './warehouse-query.service';

describe('Fleet robot project information', () => {
  it('returns the safe project identifier for matching manual dataset creation without leaking private metadata', async () => {
    const actor: AuthUser = { id: 'operator', role: 'OPERATOR', tenantId: 'tenant-a', projectIds: ['project-a'], email: '', displayName: '', mustChangePassword: false };
    const prisma = { robot: { findMany: jest.fn().mockResolvedValue([{ id: 'car-id', name: '一号车', externalId: 'car-01', projectId: 'project-a', onlineStatus: 'ONLINE', metadata: { type: 'fleet-robot', observerId: 'car-01', privateKey: 'hidden' } }]) } };
    expect(await new WarehouseQueryService(prisma as never).robots(actor)).toEqual([{ id: 'car-id', name: '一号车', externalId: 'car-01', projectId: 'project-a', onlineStatus: 'ONLINE', observerId: 'car-01' }]);
    expect(prisma.robot.findMany).toHaveBeenCalledWith(expect.objectContaining({ select: { id: true, name: true, externalId: true, projectId: true, onlineStatus: true, metadata: true }, where: expect.objectContaining({ tenantId: 'tenant-a', projectId: { in: ['project-a'] }, lifecycleStatus: 'ACTIVE' }) }));
  });
});
