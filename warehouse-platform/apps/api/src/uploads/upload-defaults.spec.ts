import { UploadsService } from './uploads.service';
import { AuthUser } from '../auth/auth.types';
const user: AuthUser = { id: 'user', email: '', displayName: '', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'], mustChangePassword: false };
describe('Warehouse upload source defaults', () => {
  function setup() {
    const prisma = { robot: { findFirst: jest.fn(), upsert: jest.fn().mockResolvedValue({ id: 'source' }) }, project: { findFirst: jest.fn().mockResolvedValue({ id: 'project' }) } };
    return { prisma, service: new UploadsService(prisma as never, {} as never, {} as never, {} as never) };
  }
  it('preserves an existing accessible source without creating a device', async () => {
    const { service, prisma } = setup(); prisma.robot.findFirst.mockResolvedValue({ id: 'old-source' });
    expect((await service.defaults(user)).robotId).toBe('old-source'); expect(prisma.robot.upsert).not.toHaveBeenCalled();
    expect(prisma.robot.findFirst).toHaveBeenCalledWith(expect.objectContaining({ where: { tenantId: 'tenant', projectId: { in: ['project'] } } }));
  });
  it('creates a scoped internal source so a new warehouse can upload without device management', async () => {
    const { service, prisma } = setup(); expect((await service.defaults(user)).robotId).toBe('source');
    expect(prisma.robot.upsert).toHaveBeenCalledWith(expect.objectContaining({ create: expect.objectContaining({ tenantId: 'tenant', projectId: 'project', metadata: { type: 'warehouse-upload-source' } }) }));
  });
  it('fails closed when no project is accessible', async () => {
    const { service, prisma } = setup(); prisma.project.findFirst.mockResolvedValue(null);
    await expect(service.defaults(user)).rejects.toThrow('分配'); expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });
});
