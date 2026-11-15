import { FilesService } from './files.service';
import { AuthUser } from '../auth/auth.types';
const user = { id: 'user', role: 'OPERATOR', tenantId: 'tenant', projectIds: ['project'] } as AuthUser;
describe('Scoped file metadata edits', () => {
  function setup() {
    const prisma = { robotFile: { findFirst: jest.fn().mockResolvedValue({ id: 'file', datasetId: 'dataset' }), update: jest.fn().mockResolvedValue({ id: 'file', fileSize: 10n }) }, datasetVersionFile: { count: jest.fn().mockResolvedValue(0) }, $queryRaw: jest.fn(), $transaction: jest.fn() };
    prisma.$transaction.mockImplementation(fn => fn(prisma)); return { prisma, service: new FilesService(prisma as never, {} as never) };
  }
  it('scopes lookup to authorized projects and never modifies object keys or bytes', async () => {
    const { prisma, service } = setup(); await service.update('file', { recognitionType: 'text', metadata: { note: '备注' } }, user);
    expect(prisma.robotFile.findFirst).toHaveBeenCalledWith({ where: { id: 'file', robot: { tenantId: 'tenant', projectId: { in: ['project'] } } } });
    expect(prisma.robotFile.update).toHaveBeenCalledWith({ where: { id: 'file' }, data: { recognitionType: 'text', metadata: { note: '备注' } } });
  });
  it('rejects changes to a file referenced by a frozen version', async () => {
    const { prisma, service } = setup(); prisma.datasetVersionFile.count.mockResolvedValue(1);
    await expect(service.update('file', { recognitionType: 'changed' }, user)).rejects.toThrow('冻结'); expect(prisma.robotFile.update).not.toHaveBeenCalled();
  });
  it('rejects out-of-scope file IDs before updates', async () => {
    const { prisma, service } = setup(); prisma.robotFile.findFirst.mockResolvedValue(null);
    await expect(service.update('other-file', {}, user)).rejects.toThrow(); expect(prisma.robotFile.update).not.toHaveBeenCalled();
  });
});
