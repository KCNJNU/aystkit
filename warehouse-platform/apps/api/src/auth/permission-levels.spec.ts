import { DatasetsController } from '../datasets/datasets.controller';
import 'reflect-metadata';
import { ExecutionContext } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { UserRole } from '@prisma/client';
import { AuthGuard } from './auth.guard';
import { UsersController } from '../users/users.controller';
import { FilesController } from '../files/files.controller';
import { StorageController } from '../storage/storage.controller';
import { SettingsController } from '../settings/settings.controller';
import { AIController } from '../ai/ai.controller';
import { WarehouseController } from '../warehouse/warehouse.controller';
import { HealthController } from '../health/health.controller';
import { AIToolRegistry } from '../ai/ai-tool-registry';
import { AuthUser } from './auth.types';

const roles: UserRole[] = ['SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR', 'VIEWER', 'BASIC'];
const endpoints = [
  [DatasetsController, 'freeze', [true, true, true, true, false, false, false]],
  [DatasetsController, 'publishJob', [true, true, true, true, false, false, false]],
  [UsersController, 'create', [true, true, true, false, false, false, false]],
  [UsersController, 'remove', [true, true, true, false, false, false, false]],
  [FilesController, 'update', [true, true, true, true, true, false, false]],
  [FilesController, 'detach', [true, true, true, true, true, false, false]],
  [FilesController, 'references', [true, true, true, true, true, true, false]],
  [FilesController, 'relocate', [true, true, true, true, true, false, false]],
  [FilesController, 'recycleBin', [true, true, true, true, true, false, false]],
  [FilesController, 'restore', [true, true, true, true, true, false, false]],
  [FilesController, 'purge', [true, true, true, false, false, false, false]],
  [FilesController, 'impact', [true, true, true, true, true, true, false]],
  [FilesController, 'remove', [true, true, true, true, true, false, false]],
  [FilesController, 'download', [true, true, true, true, true, true, false]],
  [WarehouseController, 'overview', [true, true, true, true, true, true, false]],
  [StorageController, 'scan', [true, true, false, false, false, false, false]],
  [StorageController, 'ignore', [true, true, false, false, false, false, false]],
  [SettingsController, 'update', [true, true, false, false, false, false, false]],
  [AIController, 'chat', [true, true, true, true, true, true, true]],
  [HealthController, 'status', [true, true, true, true, true, true, true]],
] as const;
describe('Permission levels on real controller metadata', () => {
  for (const [controller, method, allowed] of endpoints) {
    it.each(roles.map((role, index) => [role, allowed[index]] as const))(`${controller.name}.${method} permits %s: %s`, async (role, permitted) => {
      const user = { role, mustChangePassword: false };
      const auth = { resolveSession: jest.fn().mockResolvedValue({ user }), verifyCsrf: jest.fn().mockReturnValue(true) };
      const context = { getClass: () => controller, getHandler: () => (controller.prototype as unknown as Record<string, unknown>)[method], switchToHttp: () => ({ getRequest: () => ({ cookies: { cr_session: 'test' }, method: 'POST', path: '/api/test', header: () => 'csrf' }) }) } as unknown as ExecutionContext;
      const check = new AuthGuard(new Reflector(), auth as never).canActivate(context);
      if (permitted) await expect(check).resolves.toBe(true);
      else await expect(check).rejects.toThrow('Insufficient role');
    });
  }
  it('does not allow BASIC to bypass file permissions through an AI tool', async () => {
    const warehouse = { files: jest.fn() }; const registry = new AIToolRegistry(warehouse as never, { check: jest.fn().mockResolvedValue({ status: 'healthy' }) } as never);
    const user = { role: 'BASIC' } as AuthUser;
    await expect(registry.execute('warehouse_list_recent_uploads', '{}', user)).rejects.toThrow('基础权限');
    expect(warehouse.files).not.toHaveBeenCalled();
    await expect(registry.execute('system_get_system_status', '{}', user)).resolves.toMatchObject({ data: { status: 'healthy' } });
  });
});
