import { PrismaClient } from '@prisma/client';
import { ConfigModule } from '@nestjs/config';
import * as argon2 from 'argon2';
import { ROOT_ENV_FILE } from '../src/root-env';

async function seed() {
  await ConfigModule.forRoot({ envFilePath: ROOT_ENV_FILE });
  const email = process.env.BOOTSTRAP_ADMIN_EMAIL?.trim().toLowerCase();
  const password = process.env.BOOTSTRAP_ADMIN_PASSWORD;
  if (!email || !password || password.length < 12 || /replace-with|replace-me/.test(password)) throw new Error('Set a real BOOTSTRAP_ADMIN_EMAIL and strong BOOTSTRAP_ADMIN_PASSWORD in the root .env');
  const prisma = new PrismaClient();
  try {
    const tenant = await prisma.tenant.upsert({ where: { code: 'INTERNAL' }, update: {}, create: { code: 'INTERNAL', name: '内部数据空间' } });
    await prisma.project.upsert({ where: { tenantId_code: { tenantId: tenant.id, code: 'WAREHOUSE' } }, update: {}, create: { tenantId: tenant.id, code: 'WAREHOUSE', name: '内部数仓' } });
    await prisma.user.upsert({ where: { email }, update: {}, create: { email, displayName: '数仓管理员', role: 'SUPER_ADMIN', passwordHash: await argon2.hash(password), mustChangePassword: true } });
    console.log('Warehouse bootstrap complete; existing users and passwords were preserved.');
  } finally { await prisma.$disconnect(); }
}
void seed().catch(() => { console.error('Warehouse bootstrap failed; check configuration and database availability.'); process.exitCode = 1; });
