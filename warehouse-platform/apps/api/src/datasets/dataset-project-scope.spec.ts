import { DatasetsService } from './datasets.service';
import { AuthUser } from '../auth/auth.types';
const actor: AuthUser = { id: 'user', role: 'OPERATOR', tenantId: 'tenant-a', projectIds: ['project-a'], email: '', displayName: '', mustChangePassword: false };
describe('Dataset project assignment', () => {
  it('rejects a project outside the current user scope before creating a dataset', async () => {
    const prisma = { project: { findFirst: jest.fn().mockResolvedValue(null) }, dataset: { create: jest.fn() } };
    const service = new DatasetsService(prisma as never, {} as never);
    await expect(service.create({ name: 'Test', projectId: 'project-b' }, actor)).rejects.toThrow('无访问权限');
    expect(prisma.dataset.create).not.toHaveBeenCalled();
    expect(prisma.project.findFirst).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ tenantId: 'tenant-a', id: { in: ['project-a'], equals: 'project-b' } }) }));
  });
  it('assigns a global administrator-created dataset to the selected project so authorized users can query it', async () => {
    const prisma = { project: { findFirst: jest.fn().mockResolvedValue({ id: 'project-a', tenantId: 'tenant-a' }) }, dataset: { create: jest.fn().mockResolvedValue({ id: 'dataset' }) } };
    const database = { ...prisma, $transaction: jest.fn(async (fn) => fn(prisma)) };
    const storage = { putObject: jest.fn().mockResolvedValue(undefined) };
    const service = new DatasetsService(database as never, storage as never);
    await service.create({ name: 'Test', projectId: 'project-a' }, { ...actor, role: 'SUPER_ADMIN' });
    expect(storage.putObject).toHaveBeenCalledWith(expect.objectContaining({ objectKey: 'Test/.dataset' }));
    expect(prisma.dataset.create).toHaveBeenCalledWith({ data: expect.objectContaining({ projectId: 'project-a', tenantId: 'tenant-a' }) });
  });
});
