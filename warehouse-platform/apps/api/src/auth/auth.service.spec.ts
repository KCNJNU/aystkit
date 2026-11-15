import { ConfigService } from '@nestjs/config';
import * as argon2 from 'argon2';
import { AuthService } from './auth.service';
import { plainToInstance } from 'class-transformer';
import { validate } from 'class-validator';
import { LoginDto } from './auth.controller';
jest.mock('argon2', () => ({ verify: jest.fn(), hash: jest.fn(), argon2id: 2 }));
describe('Warehouse authentication regression', () => {
  function setup() {
    const user = { id: 'user', email: 'user@example.invalid', displayName: 'User', role: 'VIEWER', tenantId: 'tenant', projectAccess: [{ projectId: 'project' }], passwordHash: 'private-hash', isActive: true, failedLoginCount: 0, mustChangePassword: false };
    const prisma = { user: { findUnique: jest.fn().mockResolvedValue(user), update: jest.fn() }, session: { create: jest.fn().mockResolvedValue({ id: 'session' }), findUnique: jest.fn(), update: jest.fn(), delete: jest.fn().mockResolvedValue({}), deleteMany: jest.fn() }, $transaction: jest.fn() };
    prisma.$transaction.mockImplementation(async operation => typeof operation === 'function' ? operation(prisma) : Promise.all(operation));
    return { user, prisma, service: new AuthService(prisma as never, new ConfigService({ AUTH_SESSION_HOURS: '12', BOOTSTRAP_ADMIN_EMAIL: 'admin@example.invalid' })) };
  }
  beforeEach(() => { jest.clearAllMocks(); (argon2.verify as jest.Mock).mockResolvedValue(true); });
  it.each([
    ['production', undefined, true], ['development', undefined, false],
    ['production', 'false', false], ['production', false, false],
    ['development', 'true', true], ['development', true, true],
    ['production', 'invalid', true],
  ])('uses the configured cookie policy in %s with %s', (environment, configured, secure) => {
    const config = new ConfigService({ NODE_ENV: environment, AUTH_COOKIE_SECURE: configured });
    jest.spyOn(config, 'get').mockImplementation(key => key === 'NODE_ENV' ? environment : key === 'AUTH_COOKIE_SECURE' ? configured : undefined);
    const service = new AuthService({} as never, config);
    expect(service.cookieOptions()).toMatchObject({ secure, httpOnly: true, sameSite: 'lax', path: '/' });
  });
  it('accepts the admin alias and keeps email validation for other login names', async () => {
    expect(await validate(plainToInstance(LoginDto, { email: ' ADMIN ', password: 'test' }))).toEqual([]);
    expect(await validate(plainToInstance(LoginDto, { email: 'user@example.com', password: 'test' }))).toEqual([]);
    expect(await validate(plainToInstance(LoginDto, { email: 'not-an-email', password: 'test' }))).not.toEqual([]);
  });
  it('resolves admin to the configured administrator and still verifies the password', async () => {
    const { service, prisma, user } = setup(); user.role = 'SUPER_ADMIN';
    await service.login('ADMIN', 'test-password');
    expect(prisma.user.findUnique).toHaveBeenCalledWith(expect.objectContaining({ where: { email: 'admin@example.invalid' } }));
    expect(argon2.verify).toHaveBeenCalledWith('private-hash', 'test-password');
  });
  it('does not allow admin alias to target an ordinary user', async () => {
    const { service, prisma } = setup();
    await expect(service.login('admin', 'test-password')).rejects.toThrow('Invalid');
    expect(prisma.session.create).not.toHaveBeenCalled();
  });
  it('creates a hashed session and inherits project access without returning password hashes', async () => {
    const { service, prisma } = setup(); const result = await service.login('USER@example.invalid ', 'password');
    expect(result.user.projectIds).toEqual(['project']); expect(result.user).not.toHaveProperty('passwordHash');
    const stored = prisma.session.create.mock.calls[0][0].data; expect(stored.tokenHash).not.toBe(result.sessionToken); expect(service.verifyCsrf(result.csrfToken, stored.csrfTokenHash)).toBe(true);
  });
  it('denies an incorrect password without creating a session', async () => {
    const { service, prisma } = setup(); (argon2.verify as jest.Mock).mockResolvedValue(false);
    await expect(service.login('user@example.invalid', 'wrong')).rejects.toThrow('Invalid'); expect(prisma.session.create).not.toHaveBeenCalled();
  });
  it('expires and removes old sessions', async () => {
    const { service, prisma, user } = setup(); prisma.session.findUnique.mockResolvedValue({ id: 'old', user, expiresAt: new Date(0) });
    expect(await service.resolveSession('token')).toBeNull(); expect(prisma.session.delete).toHaveBeenCalledWith({ where: { id: 'old' } });
  });
  it('logout removes the active session and cookie policy remains HttpOnly', async () => {
    const { service, prisma } = setup(); await service.logout('session'); expect(prisma.session.delete).toHaveBeenCalledWith({ where: { id: 'session' } }); expect(service.cookieOptions()).toMatchObject({ httpOnly: true, sameSite: 'lax', path: '/' });
  });
});
