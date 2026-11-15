#!/usr/bin/env node
// Explicit one-time service identity. Never resets an existing password.
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const root = process.argv[2] || '/home/user/DBProg/RobotHygienism';
const destination = process.argv[3] || '/home/user/.config/robot-cloud/archive-upload.json';
const output = '/home/user/.local/state/robot-cloud/garage-archive-20261027/provision/scopeinfo.json';
const email = 'garage-archive@robot.local';
let prisma, createdId, createdFile = false, completed = false;

async function verify(credentials, expected) {
  let token, csrf;
  const response = await fetch('http://127.0.0.1:3000/api/v1/auth/login', { method: 'POST', redirect: 'manual',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: credentials.password }) });
  if (response.status !== 200 && response.status !== 201) throw new Error('login failed');
  const data = await response.json();
  token = /(?:^|[,\s])cr_session=([A-Za-z0-9_-]{20,256})(?:;|$)/.exec(response.headers.get('set-cookie') || '')?.[1];
  csrf = data.csrfToken;
  if (!token || typeof csrf !== 'string') throw new Error('session invalid');
  try {
    const user = data.user;
    if (user.role !== 'OPERATOR' || user.tenantId !== expected.tenantId || user.mustChangePassword
        || user.projectIds.length !== 1 || user.projectIds[0] !== expected.projectId) throw new Error('scope invalid');
    const sources = await fetch('http://127.0.0.1:3000/api/warehouse/sources', {
      redirect: 'manual', headers: { Cookie: 'cr_session=' + token } });
    if (sources.status !== 200 || !(await sources.json()).some(item => item.id === expected.robotId)) {
      throw new Error('source scope invalid');
    }
    const projects = await fetch('http://127.0.0.1:3000/api/warehouse/projects', {
      redirect: 'manual', headers: { Cookie: 'cr_session=' + token } });
    const visible = await projects.json();
    if (projects.status !== 200 || visible.length !== 1 || visible[0].id !== expected.projectId) {
      throw new Error('project scope invalid');
    }
  } finally {
    const logout = await fetch('http://127.0.0.1:3000/api/v1/auth/logout', { method: 'POST', redirect: 'manual',
      headers: { Cookie: 'cr_session=' + token, 'X-CSRF-Token': csrf } });
    if (logout.status !== 200 && logout.status !== 201) throw new Error('logout failed');
  }
}

try {
  const require = createRequire(path.join(root, 'package.json'));
  const environment = require('dotenv').parse(fs.readFileSync(path.join(root, '.env')));
  const database = new URL(environment.DATABASE_URL || '');
  if (!['localhost', '127.0.0.1', '[::1]', '::1'].includes(database.hostname)) throw new Error('local only');
  prisma = new (require('@prisma/client').PrismaClient)({ datasources: { db: { url: environment.DATABASE_URL } }, log: [] });
  const tenant = await prisma.tenant.findUniqueOrThrow({ where: { code: 'garage-native-archive' } });
  const project = await prisma.project.findUniqueOrThrow({ where: {
    tenantId_code: { tenantId: tenant.id, code: 'garage-native-ipc' } } });
  const robot = await prisma.robot.findUniqueOrThrow({ where: { externalId: 'garage-native-ipc' } });
  if (tenant.name !== '地库原生数据' || project.name !== '小车地库采集'
      || robot.tenantId !== tenant.id || robot.projectId !== project.id
      || robot.metadata?.type !== 'garage-native-source' || robot.metadata?.host !== 'IPC') throw new Error('scope invalid');
  let credentials;
  const existing = await prisma.user.findUnique({ where: { email }, include: { projectAccess: true } });
  if (fs.existsSync(destination)) {
    const info = fs.lstatSync(destination);
    if (!info.isFile() || (info.mode & 0o077) || info.size > 16384) throw new Error('private config required');
    credentials = JSON.parse(fs.readFileSync(destination, 'utf8'));
    if (!existing || credentials.email !== email || credentials.tenantId !== tenant.id
        || credentials.projectId !== project.id || credentials.robotId !== robot.id
        || existing.role !== 'OPERATOR' || existing.tenantId !== tenant.id || existing.projectAccess.length !== 1
        || existing.projectAccess[0].projectId !== project.id) throw new Error('existing identity invalid');
  } else {
    if (existing) throw new Error('existing account has no private config; refuse password reset');
    const password = randomBytes(48).toString('base64url');
    const argon2 = require('argon2');
    const user = await prisma.user.create({ data: { email, displayName: '地库原始数据归档服务',
      role: 'OPERATOR', tenantId: tenant.id, isActive: true, mustChangePassword: false,
      passwordHash: await argon2.hash(password, { type: argon2.argon2id }),
      projectAccess: { create: { projectId: project.id } } } });
    createdId = user.id;
    credentials = { apiUrl: 'http://127.0.0.1:3000/api', email, password,
      tenantId: tenant.id, projectId: project.id, robotId: robot.id, datasetPrefix: '地库_' };
  }
  await verify(credentials, { tenantId: tenant.id, projectId: project.id, robotId: robot.id });
  if (!fs.existsSync(destination)) {
    fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
    const descriptor = fs.openSync(destination, 'wx', 0o600);
    createdFile = true;
    try { fs.writeFileSync(descriptor, JSON.stringify(credentials, null, 2) + '\n'); fs.fsyncSync(descriptor); }
    finally { fs.closeSync(descriptor); }
    const parent = fs.openSync(path.dirname(destination), 'r');
    try { fs.fsyncSync(parent); } finally { fs.closeSync(parent); }
  }
  const user = await prisma.user.findUniqueOrThrow({ where: { email }, select: { id: true } });
  const report = { configured: true, verifiedScopedOperator: true, tenantId: tenant.id,
    projectId: project.id, robotId: robot.id, operatorUserId: user.id, configPath: destination };
  fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
  fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  completed = true;
  process.stdout.write(JSON.stringify(report) + '\n');
} catch {
  process.stdout.write(JSON.stringify({ configured: false, error: 'ARCHIVE_OPERATOR_PROVISION_FAILED' }) + '\n');
  process.exitCode = 1;
} finally {
  if (!completed) {
    if (createdFile) { try { fs.unlinkSync(destination); } catch {} }
    if (createdId && prisma) { try { await prisma.user.delete({ where: { id: createdId } }); } catch {} }
  }
  if (prisma) await prisma.$disconnect();
}
