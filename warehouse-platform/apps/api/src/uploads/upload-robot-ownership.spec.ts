import { AuthUser } from '../auth/auth.types';
import { UploadsService } from './uploads.service';

const robotId = '00000000-0000-4000-8000-000000000001';
const actor: AuthUser = { id: 'operator', role: 'OPERATOR', tenantId: 'tenant-a', projectIds: ['project-a'], email: '', displayName: '', mustChangePassword: false };
function setup() {
  const dataset = { id: 'dataset', name: '相机采集', tenantId: 'tenant-a', projectId: 'project-a', metadata: { robotId, purpose: 'calibration' } };
  const prisma = {
    dataset: { findFirst: jest.fn().mockResolvedValue(dataset) },
    robot: { findFirst: jest.fn().mockResolvedValue({ id: robotId, tenantId: 'tenant-a', projectId: 'project-a' }), upsert: jest.fn().mockResolvedValue({ id: 'virtual-source' }) },
    project: { findFirst: jest.fn().mockResolvedValue({ id: 'project-a' }) },
    robotFile: { findFirst: jest.fn() },
    uploadSession: { create: jest.fn() },
  };
  return { prisma, dataset, service: new UploadsService(prisma as never, {} as never, {} as never, {} as never) };
}

describe('Dataset upload robot ownership', () => {
  it('rejects another robot before checksum deduplication or session creation even in the same project', async () => {
    const { service, prisma } = setup();
    await expect(service.create(actor, { datasetId: 'dataset', robotId: '00000000-0000-4000-8000-000000000002', originalName: 'video.mkv', mimeType: 'video/x-matroska', fileSize: 10, checksum: 'a'.repeat(64), recognitionType: 'unclassified', schemaVersion: '1.0', capturedAt: '2026-11-09T00:00:00Z' })).rejects.toThrow('所属机器人不一致');
    expect(prisma.robotFile.findFirst).not.toHaveBeenCalled();
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.uploadSession.create).not.toHaveBeenCalled();
  });

  it('uses the bound actual robot without creating a virtual upload source', async () => {
    const { service, prisma } = setup();
    expect(await service.defaults(actor, 'dataset')).toEqual({ robotId, recognitionType: 'unclassified', schemaVersion: '1.0', capturedAtStrategy: 'LAST_MODIFIED' });
    expect(prisma.robot.findFirst).toHaveBeenCalledWith({ where: { id: robotId, lifecycleStatus: 'ACTIVE', tenantId: 'tenant-a', projectId: { in: ['project-a'] } }, select: { id: true, tenantId: true, projectId: true } });
    expect(prisma.project.findFirst).toHaveBeenCalledWith({ where: { id: 'project-a', tenantId: 'tenant-a', status: 'ACTIVE', tenant: { status: 'ACTIVE' } }, select: { id: true } });
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });

  it('refuses unknown, inaccessible and inactive bindings instead of falling back', async () => {
    const { service, prisma } = setup();
    prisma.robot.findFirst.mockResolvedValue(null);
    await expect(service.defaults(actor, 'dataset')).rejects.toThrow('无访问权限');
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });

  it.each([{ tenantId: 'tenant-b', projectId: 'project-a' }, { tenantId: 'tenant-a', projectId: 'project-b' }])('refuses a mismatched binding %j even for a global admin', async assignment => {
    const { service, prisma } = setup();
    prisma.robot.findFirst.mockResolvedValue({ id: robotId, ...assignment });
    await expect(service.defaults({ ...actor, role: 'SUPER_ADMIN' }, 'dataset')).rejects.toThrow('同一个项目');
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });

  it.each(['not-a-uuid', null, 12])('refuses a malformed explicit binding %j', async binding => {
    const { service, prisma, dataset } = setup();
    prisma.dataset.findFirst.mockResolvedValue({ ...dataset, metadata: { robotId: binding } });
    await expect(service.defaults(actor, 'dataset')).rejects.toThrow('所属机器人无效');
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });

  it('refuses an inactive project or tenant', async () => {
    const { service, prisma } = setup();
    prisma.project.findFirst.mockResolvedValue(null);
    await expect(service.defaults(actor, 'dataset')).rejects.toThrow('项目已停用');
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });

  it('preserves virtual sources for legacy datasets without an explicit binding', async () => {
    const { service, prisma, dataset } = setup();
    prisma.dataset.findFirst.mockResolvedValue({ ...dataset, metadata: { purpose: 'legacy' } });
    expect((await service.defaults(actor, 'dataset')).robotId).toBe('virtual-source');
    expect(prisma.robot.upsert).toHaveBeenCalledWith(expect.objectContaining({ where: { externalId: 'warehouse-dataset:dataset' } }));
  });

  it('checks dataset access before consulting its robot', async () => {
    const { service, prisma } = setup();
    prisma.dataset.findFirst.mockResolvedValue(null);
    await expect(service.defaults(actor, 'dataset')).rejects.toThrow('无访问权限');
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.robot.upsert).not.toHaveBeenCalled();
  });
});
