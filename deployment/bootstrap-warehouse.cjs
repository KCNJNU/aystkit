'use strict';

// Run after `prisma migrate deploy`, inside the warehouse migration image.
// Re-running this script never resets an existing account or grants it a role.
function configuration() {
  const email = (process.env.BOOTSTRAP_ADMIN_EMAIL || '').trim().toLowerCase();
  const password = process.env.BOOTSTRAP_ADMIN_PASSWORD;
  const databaseUrl = process.env.DATABASE_URL;
  const robotId = (process.env.BOOTSTRAP_ROBOT_ID || '').trim().toLowerCase();
  const robotExternalId = (process.env.BOOTSTRAP_ROBOT_EXTERNAL_ID || '').trim();
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    throw new Error('BOOTSTRAP_ADMIN_EMAIL must be a valid email address.');
  }
  if (typeof password !== 'string' || password.length < 12 || /replace[-_ ]?(with|me)|your[_ -]|change[-_ ]?me/i.test(password)) {
    throw new Error('BOOTSTRAP_ADMIN_PASSWORD must be a private password of at least 12 characters.');
  }
  if (!databaseUrl || !/^postgres(?:ql)?:\/\//.test(databaseUrl)) {
    throw new Error('DATABASE_URL must explicitly identify a PostgreSQL database.');
  }
  if (Boolean(robotId) !== Boolean(robotExternalId)) {
    throw new Error('BOOTSTRAP_ROBOT_ID and BOOTSTRAP_ROBOT_EXTERNAL_ID must be configured together.');
  }
  if (robotId && (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(robotId)
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(robotExternalId))) {
    throw new Error('BOOTSTRAP_ROBOT_ID must be a UUID and BOOTSTRAP_ROBOT_EXTERNAL_ID must be a valid robot identifier.');
  }
  return { email, password, robotId, robotExternalId };
}

async function main() {
  const { email, password, robotId, robotExternalId } = configuration();
  // The script may be mounted outside /workspace; resolve the installed packages
  // from the image's workspace rather than from the host checkout.
  const searchPaths = [process.cwd(), '/workspace'];
  const { PrismaClient } = require(require.resolve('@prisma/client', { paths: searchPaths }));
  const argon2 = require(require.resolve('argon2', { paths: searchPaths }));
  const prisma = new PrismaClient();
  try {
    const existing = await prisma.user.findUnique({ where: { email }, select: { id: true } });
    const passwordHash = existing ? undefined : await argon2.hash(password, { type: argon2.argon2id });
    await prisma.$transaction(async (tx) => {
      const tenant = await tx.tenant.upsert({
        where: { code: 'INTERNAL' }, update: {},
        create: { code: 'INTERNAL', name: '内部数据空间' },
      });
      const project = await tx.project.upsert({
        where: { tenantId_code: { tenantId: tenant.id, code: 'WAREHOUSE' } }, update: {},
        create: { tenantId: tenant.id, code: 'WAREHOUSE', name: '内部数仓' },
      });
      if (robotId) {
        const matchesBinding = (robot) => robot.id === robotId && robot.externalId === robotExternalId
          && robot.tenantId === tenant.id && robot.projectId === project.id;
        const bindings = await tx.robot.findMany({
          where: { OR: [{ id: robotId }, { externalId: robotExternalId }] },
          select: { id: true, externalId: true, tenantId: true, projectId: true },
        });
        if (bindings.some((robot) => !matchesBinding(robot))) {
          throw new Error('BOOTSTRAP_ROBOT_ID or BOOTSTRAP_ROBOT_EXTERNAL_ID is already bound to another robot or project.');
        }
        const robot = await tx.robot.upsert({
          where: { id: robotId }, update: {},
          create: { id: robotId, externalId: robotExternalId, name: '01号车', tenantId: tenant.id,
            projectId: project.id, metadata: { type: 'fleet-robot', observerId: robotExternalId } },
          select: { id: true, externalId: true, tenantId: true, projectId: true },
        });
        // Recheck the upsert result in case a concurrent registration won the
        // same UUID after findMany. A conflicting identity rolls back everything.
        if (!matchesBinding(robot)) {
          throw new Error('BOOTSTRAP_ROBOT_ID was concurrently bound to another robot or project.');
        }
      }
      if (passwordHash) {
        await tx.user.upsert({
          where: { email }, update: {},
          create: { email, displayName: '数仓管理员', role: 'SUPER_ADMIN', passwordHash, mustChangePassword: true },
        });
      }
    });
    console.log('Warehouse bootstrap complete. Existing accounts, passwords and permissions were preserved.');
  } finally {
    await prisma.$disconnect();
  }
}

main().catch((error) => {
  // Do not print Prisma errors: connection URLs can contain private credentials.
  const configurationError = error instanceof Error && /^(BOOTSTRAP_ADMIN_|BOOTSTRAP_ROBOT_|DATABASE_URL)/.test(error.message);
  console.error(configurationError ? error.message : 'Warehouse bootstrap failed. Check migrations, database connectivity and permissions.');
  process.exitCode = 1;
});
