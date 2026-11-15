#!/usr/bin/env node
// One-time, local-only archive scope. Never prints credentials or connection URLs.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const root = process.argv[2] || '/home/user/DBProg/RobotHygienism';
const require = createRequire(path.join(root, 'package.json'));
const { PrismaClient } = require('@prisma/client');
const dotenv = require('dotenv');
let prisma;
try {
  const environment = dotenv.parse(readFileSync(path.join(root, '.env')));
  const database = new URL(environment.DATABASE_URL || '');
  if (!['localhost', '127.0.0.1', '[::1]', '::1'].includes(database.hostname)) throw new Error('local scope only');
  prisma = new PrismaClient({ datasources: { db: { url: environment.DATABASE_URL } }, log: [] });
} catch {
  process.stdout.write(JSON.stringify({ configured: false, error: 'ARCHIVE_SCOPE_LOCAL_CONFIG_INVALID' }) + '\n');
  process.exit(1);
}
const account = 'garage-archive@robot.local';

try {
  const result = await prisma.$transaction(async tx => {
    await tx.$queryRaw`SELECT pg_advisory_xact_lock(182736450)::text AS locked`;
    let tenant = await tx.tenant.findUnique({ where: { code: 'garage-native-archive' } });
    if (tenant) {
      if (tenant.name !== '地库原生数据' || tenant.status !== 'ACTIVE') throw new Error('scope ownership');
      const foreignUsers = await tx.user.count({ where: { tenantId: tenant.id,
        OR: [{ email: { not: account } }, { role: { not: 'OPERATOR' } }] } });
      const foreignProjects = await tx.project.count({ where: { tenantId: tenant.id,
        OR: [{ code: { not: 'garage-native-ipc' } }, { name: { not: '小车地库采集' } }] } });
      if (foreignUsers || foreignProjects) throw new Error('scope ownership');
    } else {
      tenant = await tx.tenant.create({ data: { code: 'garage-native-archive', name: '地库原生数据' } });
    }
    let project = await tx.project.findUnique({ where: { tenantId_code: { tenantId: tenant.id, code: 'garage-native-ipc' } } });
    if (project) {
      if (project.name !== '小车地库采集' || project.status !== 'ACTIVE') throw new Error('scope ownership');
      const foreignAccess = await tx.userProjectAccess.count({ where: { projectId: project.id,
        user: { OR: [{ email: { not: account } }, { role: { not: 'OPERATOR' } }, { tenantId: { not: tenant.id } }] } } });
      const datasets = await tx.dataset.findMany({ where: { projectId: project.id }, select: { metadata: true } });
      if (foreignAccess || datasets.some(item => item.metadata?.type !== 'garage-native-archive')) throw new Error('scope ownership');
    } else {
      project = await tx.project.create({ data: { tenantId: tenant.id, code: 'garage-native-ipc', name: '小车地库采集' } });
    }
    const existingAccount = await tx.user.findUnique({ where: { email: account },
      select: { tenantId: true, role: true, projectAccess: { select: { projectId: true } } } });
    if (existingAccount && (existingAccount.tenantId !== tenant.id || existingAccount.role !== 'OPERATOR'
        || existingAccount.projectAccess.length !== 1 || existingAccount.projectAccess[0].projectId !== project.id)) {
      throw new Error('scope ownership');
    }
    let robot = await tx.robot.findUnique({ where: { externalId: 'garage-native-ipc' } });
    if (robot) {
      if (robot.tenantId !== tenant.id || robot.projectId !== project.id
          || robot.metadata?.type !== 'garage-native-source' || robot.metadata?.host !== 'IPC') {
        throw new Error('scope ownership');
      }
    } else {
      robot = await tx.robot.create({ data: { externalId: 'garage-native-ipc', name: 'IPC 地库原生采集源',
        tenantId: tenant.id, projectId: project.id, metadata: { type: 'garage-native-source', host: 'IPC' } } });
    }
    const foreignRobot = await tx.robot.count({ where: { projectId: project.id, id: { not: robot.id } } });
    if (foreignRobot) throw new Error('scope ownership');
    return { configured: true, tenantId: tenant.id, projectId: project.id, robotId: robot.id };
  }, { timeout: 15000 });
  process.stdout.write(JSON.stringify(result) + '\n');
} catch {
  process.stdout.write(JSON.stringify({ configured: false, error: 'ARCHIVE_SCOPE_PROVISION_FAILED' }) + '\n');
  process.exitCode = 1;
} finally {
  await prisma.$disconnect();
}
