import { UsersController } from './users.controller';
import { AuthUser } from '../auth/auth.types';
const actor: AuthUser = { id: 'admin', role: 'TENANT_ADMIN', tenantId: 'tenant-a', projectIds: [], email: '', displayName: '', mustChangePassword: false };
describe('Internal user administration scope', () => {
  function setup() {
    const prisma = { tenant: { findMany: jest.fn().mockResolvedValue([{ id: 'tenant-a' }]) }, project: { findMany: jest.fn().mockResolvedValue([]), count: jest.fn().mockResolvedValue(0) }, user: { findFirst: jest.fn().mockResolvedValue({ id: 'user', tenantId: 'tenant-a', role: 'OPERATOR', projectAccess: [{ projectId: 'project-a' }] }), findUniqueOrThrow: jest.fn().mockResolvedValue({ id: 'user', tenantId: 'tenant-a', role: 'OPERATOR', deletedAt: null }), count: jest.fn().mockResolvedValue(1), update: jest.fn().mockResolvedValue({ id: 'user' }) }, session: { deleteMany: jest.fn() }, $queryRaw: jest.fn().mockResolvedValue([]), $transaction: jest.fn() };
    prisma.$transaction.mockImplementation(async fn => fn(prisma)); return { prisma, controller: new UsersController(prisma as never) };
  }
  it('never lists another tenant in access options', async () => {
    const { prisma, controller } = setup(); await controller.scopeOptions(actor); expect(prisma.tenant.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: { id: 'tenant-a', status: 'ACTIVE' } }));
  });
  it('rejects assigning a project from a different tenant', async () => {
    const { prisma, controller } = setup(); await expect(controller.update('user', { projectIds: ['other-project'] }, actor)).rejects.toThrow('项目必须'); expect(prisma.user.update).not.toHaveBeenCalled();
  });
  it('revokes sessions atomically after changing project access', async () => {
    const { prisma, controller } = setup(); prisma.project.count.mockResolvedValue(1); await controller.update('user', { projectIds: ['project-a'] }, actor); expect(prisma.session.deleteMany).toHaveBeenCalledWith({ where: { userId: 'user' } });
  });
  it('prevents system administrators from editing a super administrator', async () => {
    const { controller, prisma } = setup(); prisma.user.findFirst.mockResolvedValue({ id: 'super', tenantId: null, role: 'SUPER_ADMIN', projectAccess: [] });
    await expect(controller.update('super', { displayName: 'Changed' }, { ...actor, role: 'SYSTEM_ADMIN' })).rejects.toThrow('同级或更高级');
    expect(prisma.user.update).not.toHaveBeenCalled();
  });
  it('prevents deleting the current account', async () => {
    const { controller, prisma } = setup(); prisma.user.findFirst.mockResolvedValue({ id: actor.id, tenantId: 'tenant-a', role: actor.role, projectAccess: [] });
    await expect(controller.remove(actor.id, actor)).rejects.toThrow('当前登录');
  });
  it('soft-deletes an accessible lower-role account and revokes sessions', async () => {
    const { controller, prisma } = setup(); await controller.remove('user', actor);
    expect(prisma.user.update).toHaveBeenCalledWith({ where: { id: 'user' }, data: { deletedAt: expect.any(Date), isActive: false } });
    expect(prisma.session.deleteMany).toHaveBeenCalledWith({ where: { userId: 'user' } });
  });
  it('preserves the last active super administrator', async () => {
    const { controller, prisma } = setup(); prisma.user.findFirst.mockResolvedValue({ id: 'other', tenantId: null, role: 'SUPER_ADMIN', projectAccess: [] });
    prisma.user.findUniqueOrThrow.mockResolvedValue({ id: 'other', role: 'SUPER_ADMIN', deletedAt: null }); prisma.user.count.mockResolvedValue(0);
    await expect(controller.remove('other', { ...actor, role: 'SUPER_ADMIN' })).rejects.toThrow('至少一个');
    expect(prisma.user.update).not.toHaveBeenCalled();
  });
  it('prevents tenant administrators from promoting a global administrator', async () => {
    const { controller } = setup(); await expect(controller.update('user', { role: 'SUPER_ADMIN' }, actor)).rejects.toThrow('不能授予');
  });
});
