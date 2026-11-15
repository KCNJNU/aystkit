import { BadRequestException } from '@nestjs/common';
import { SettingsService, SETTING_DEFAULTS } from './settings.service';

describe('optional per-file upload policies', () => {
  const prisma = { systemSetting: { findUnique: jest.fn(), findMany: jest.fn(), upsert: jest.fn() },
    $transaction: jest.fn() };
  const service = new SettingsService(prisma as never);
  beforeEach(() => {
    jest.clearAllMocks();
    prisma.systemSetting.findUnique.mockResolvedValue(null);
    prisma.systemSetting.findMany.mockResolvedValue([]);
    prisma.systemSetting.upsert.mockResolvedValue({});
    prisma.$transaction.mockResolvedValue([]);
  });
  it('defaults every file type to no configured business limit', async () => {
    const values = await service.all();
    const uploadKeys = Object.keys(SETTING_DEFAULTS).filter(key => key.startsWith('upload.'));
    expect(uploadKeys).toHaveLength(6);
    expect(uploadKeys.every(key => values[key] === 0)).toBe(true);
    expect(await service.get('upload.logMaxBytes')).toBe(0);
  });
  it.each([0, 3 * 1024 ** 3, Number.MAX_SAFE_INTEGER])('accepts zero or an exact optional limit %s', async value => {
    await service.update({ 'upload.logMaxBytes': value }, 'admin');
    expect(prisma.systemSetting.upsert).toHaveBeenCalledWith(expect.objectContaining({
      create: { key: 'upload.logMaxBytes', value, updatedById: 'admin' } }));
  });
  it.each([-1, .5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, '0', null])(
    'rejects malformed upload policy %s', async value => {
      await expect(service.update({ 'upload.logMaxBytes': value }, 'admin')).rejects.toBeInstanceOf(BadRequestException);
      expect(prisma.$transaction).not.toHaveBeenCalled();
    });
  it('preserves explicitly configured positive policies and zero values in reads', async () => {
    prisma.systemSetting.findUnique.mockResolvedValueOnce({ value: 3 * 1024 ** 3 });
    expect(await service.get('upload.logMaxBytes')).toBe(3 * 1024 ** 3);
    prisma.systemSetting.findUnique.mockResolvedValueOnce({ value: 0 });
    expect(await service.get('upload.logMaxBytes')).toBe(0);
  });
  it.each(['jobs.workerConcurrency', 'download.presignedTtlSeconds'])('does not make zero valid for %s', async key => {
    await expect(service.update({ [key]: 0 }, 'admin')).rejects.toBeInstanceOf(BadRequestException);
  });
});
