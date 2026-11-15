import { validate } from 'class-validator';
import { AuthUser } from '../auth/auth.types';
import { DatasetsService } from './datasets.service';
import { datasetRobotScope } from './dataset-robot-scope';
import { CreateDatasetDto } from './dto/create-dataset.dto';

const robotId = '00000000-0000-4000-8000-000000000001';
const otherRobotId = '00000000-0000-4000-8000-000000000002';
const actor: AuthUser = { id: 'operator', role: 'OPERATOR', tenantId: 'tenant-a', projectIds: ['project-a', 'project-b'], email: '', displayName: '', mustChangePassword: false };

function setup() {
  let created: Record<string, unknown> | undefined;
  const prisma = {
    robot: { findFirst: jest.fn().mockResolvedValue({ id: robotId, tenantId: 'tenant-a', projectId: 'project-a' }) },
    project: { findFirst: jest.fn().mockResolvedValue({ id: 'project-a', tenantId: 'tenant-a' }) },
    dataset: {
      create: jest.fn().mockImplementation(async ({ data }) => (created = { id: 'dataset', ...data })),
      findMany: jest.fn().mockImplementation(async () => [{ ...created, versions: [] }]),
      count: jest.fn().mockResolvedValue(1),
    },
    $transaction: jest.fn(),
  };
  prisma.$transaction.mockImplementation(async input => typeof input === 'function' ? input(prisma) : Promise.all(input));
  const storage = { putObject: jest.fn().mockResolvedValue(undefined) };
  return { prisma, storage, service: new DatasetsService(prisma as never, storage as never) };
}

describe('Dataset robot ownership on creation', () => {
  it('derives the selected robot project and keeps an empty dataset classified before any uploads', async () => {
    const { prisma, storage, service } = setup();
    const dataset = await service.create({ name: '新车采集', robotId, metadata: { purpose: 'calibration' } }, actor);
    expect(dataset).toEqual(expect.objectContaining({ tenantId: 'tenant-a', projectId: 'project-a', metadata: { purpose: 'calibration', robotId } }));
    expect(prisma.robot.findFirst).toHaveBeenCalledWith({ where: { id: robotId, lifecycleStatus: 'ACTIVE', tenantId: 'tenant-a', projectId: { in: ['project-a', 'project-b'] } }, select: { id: true, tenantId: true, projectId: true } });
    expect(storage.putObject).toHaveBeenCalledWith(expect.objectContaining({ objectKey: '新车采集/.dataset' }));
    const page = await service.page({ robotId, page: 1, pageSize: 20, sortBy: 'updatedAt', sortOrder: 'desc' }, actor);
    expect(page.items[0].metadata).toEqual({ purpose: 'calibration', robotId });
    expect(page.items[0].versionCount).toBe(0);
    expect(prisma.dataset.findMany).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ tenantId: 'tenant-a', AND: [datasetRobotScope(robotId)] }) }));
  });

  it('rejects another project even when the user can access both projects', async () => {
    const { service, prisma } = setup();
    await expect(service.create({ name: '错误项目', robotId, projectId: 'project-b' }, actor)).rejects.toThrow('同一个项目');
    expect(prisma.dataset.create).not.toHaveBeenCalled();
  });

  it('rejects unknown, inaccessible or inactive selected robots without writing storage', async () => {
    const { service, prisma, storage } = setup();
    prisma.robot.findFirst.mockResolvedValue(null);
    await expect(service.create({ name: '无权限', robotId }, actor)).rejects.toThrow('无访问权限');
    expect(prisma.dataset.create).not.toHaveBeenCalled();
    expect(storage.putObject).not.toHaveBeenCalled();
  });

  it('requires the selected project and tenant to remain active', async () => {
    const { service, prisma } = setup();
    prisma.project.findFirst.mockResolvedValue(null);
    await expect(service.create({ name: '停用项目', robotId }, actor)).rejects.toThrow('数据项目');
    expect(prisma.project.findFirst).toHaveBeenCalledWith(expect.objectContaining({ where: expect.objectContaining({ status: 'ACTIVE', tenant: { status: 'ACTIVE' }, tenantId: 'tenant-a', id: { equals: 'project-a', in: actor.projectIds } }) }));
    expect(prisma.dataset.create).not.toHaveBeenCalled();
  });

  it('does not let a global admin bind a robot with inconsistent tenant/project data', async () => {
    const { service, prisma } = setup();
    prisma.project.findFirst.mockResolvedValue({ id: 'project-a', tenantId: 'tenant-b' });
    await expect(service.create({ name: '错误租户', robotId }, { ...actor, role: 'SYSTEM_ADMIN' })).rejects.toThrow('数据空间不一致');
    expect(prisma.dataset.create).not.toHaveBeenCalled();
  });

  it('rejects an unassigned robot', async () => {
    const { service, prisma } = setup();
    prisma.robot.findFirst.mockResolvedValue({ id: robotId, tenantId: 'tenant-a', projectId: null });
    await expect(service.create({ name: '未分配项目', robotId }, actor)).rejects.toThrow('尚未分配');
    expect(prisma.dataset.create).not.toHaveBeenCalled();
  });

  it('rejects contradictory metadata rather than silently replacing ownership', async () => {
    const { service, prisma } = setup();
    await expect(service.create({ name: '冲突', robotId, metadata: { robotId: otherRobotId } }, actor)).rejects.toThrow('元数据不一致');
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.dataset.create).not.toHaveBeenCalled();
  });

  it('preserves the existing archive metadata-only create contract', async () => {
    const { service, prisma } = setup();
    const metadata = { type: 'garage-native-archive', robotId, day: '2026-11-09', sensors: ['原相机前'] };
    await service.create({ name: '地库_2026-11-09', projectId: 'project-a', metadata }, actor);
    expect(prisma.robot.findFirst).not.toHaveBeenCalled();
    expect(prisma.dataset.create).toHaveBeenCalledWith({ data: expect.objectContaining({ metadata, projectId: 'project-a', tenantId: 'tenant-a' }) });
  });

  it('validates the optional public robot identifier as a UUID', async () => {
    const valid = Object.assign(new CreateDatasetDto(), { name: '数据集', robotId });
    expect(await validate(valid)).toEqual([]);
    const invalid = Object.assign(new CreateDatasetDto(), { name: '数据集', robotId: 'old-car-alias' });
    expect((await validate(invalid)).map(error => error.property)).toContain('robotId');
  });
});
