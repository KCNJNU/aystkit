import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, copyFileSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const windows = process.platform === 'win32';

// Exercise the real PowerShell scripts against a fake Docker CLI and HTTP probe.
// Each case uses an isolated path with spaces and never reads the user's .env.
function runLauncher({ script = 'start-all.ps1', args = [], scenario = 'ok', withEnv = true } = {}) {
  const fixture = mkdtempSync(path.join(tmpdir(), 'warehouse launcher '));
  const scripts = path.join(fixture, 'scripts');
  mkdirSync(scripts);
  for (const file of ['start-all.ps1', 'stop-all.ps1']) copyFileSync(path.join(root, file), path.join(fixture, file));
  copyFileSync(path.join(root, 'scripts/warehouse-compose.ps1'), path.join(scripts, 'warehouse-compose.ps1'));
  writeFileSync(path.join(fixture, 'docker-compose.yml'), 'services: {}\n');
  writeFileSync(path.join(fixture, '.env.example'), 'EXAMPLE=true\n');
  if (withEnv) writeFileSync(path.join(fixture, '.env'), 'EXISTING=true\n');
  const log = path.join(fixture, 'calls.jsonl');
  writeFileSync(log, '');
  writeFileSync(path.join(fixture, 'docker.cmd'), `@echo off\r\n"${process.execPath}" "%~dp0fake-docker.cjs" %*\r\nexit /b %errorlevel%\r\n`);
  writeFileSync(path.join(fixture, 'fake-docker.cjs'), `
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.LAUNCHER_LOG, JSON.stringify(args) + '\\n');
const scenario = process.env.LAUNCHER_SCENARIO;
if (args[0] === 'info') {
  if (scenario === 'engine-down') process.exit(1);
  console.log('linux');
} else if (args.includes('version')) console.log('Docker Compose version v2.mock');
else if (args.includes('config')) console.log(JSON.stringify({ services: { migration: { environment: {
  BOOTSTRAP_ADMIN_EMAIL: 'test@example.invalid',
  BOOTSTRAP_ADMIN_PASSWORD: scenario === 'bad-credentials' ? 'replace-with-a-strong-password' : 'a-test-only-password'
} } } }));
else if (args.includes('up') && scenario === 'up-failure') process.exit(19);
else if (args.includes('run') && scenario === 'seed-failure') process.exit(23);
else if (args.includes('ps') && args.includes('--services')) {
  console.log(['postgres','redis','minio','backend','worker','web'].filter(s => scenario !== 'worker-down' || s !== 'worker').join('\\n'));
}
`);
  const quote = (value) => `'${value.replaceAll("'", "''")}'`;
  const command = `function global:Invoke-WebRequest { [pscustomobject]@{ StatusCode = 200 } }; & ${quote(path.join(fixture, script))} ${args.join(' ')}`;
  const result = spawnSync('powershell.exe', ['-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
    cwd: tmpdir(), // Verify launchers do not depend on the shell working directory.
    encoding: 'utf8', timeout: 60000,
    env: { ...process.env, PATH: `${fixture};${process.env.PATH}`, LAUNCHER_LOG: log, LAUNCHER_SCENARIO: scenario },
  });
  assert.ifError(result.error);
  return { ...result, fixture, calls: readFileSync(log, 'utf8').split('\n').filter(Boolean).map(JSON.parse) };
}

test('Windows launcher orchestration', { skip: !windows }, async (t) => {
  await t.test('preflight never starts services or exposes credentials', () => {
    const result = runLauncher({ args: ['-CheckOnly'] });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.ok(!result.calls.some(c => c.includes('up') || c.includes('run')));
    assert.ok(!result.stdout.includes('a-test-only-password'));
    assert.equal(readFileSync(path.join(result.fixture, '.env'), 'utf8'), 'EXISTING=true\n');
  });
  await t.test('full startup orders migration stack, seed, repository and readiness', () => {
    const result = runLauncher();
    assert.equal(result.status, 0, result.stdout + result.stderr);
    const up = result.calls.findIndex(c => c.includes('up'));
    const seed = result.calls.findIndex(c => c.includes('prisma:seed'));
    const git = result.calls.findIndex(c => c.includes('git'));
    assert.ok(up >= 0 && seed > up && git > seed);
    assert.ok(result.calls[up].includes('--build'));
    assert.match(result.stdout, /Warehouse is ready/);
    for (const call of result.calls.filter(c => c.includes('--project-directory'))) {
      assert.equal(call[call.indexOf('--project-directory') + 1], result.fixture);
    }
  });
  await t.test('existing deployment supports SkipSeed and NoBuild', () => {
    const result = runLauncher({ args: ['-SkipSeed', '-NoBuild'], scenario: 'bad-credentials' });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.ok(!result.calls.some(c => c.includes('prisma:seed')));
    assert.ok(result.calls.find(c => c.includes('up')).includes('--no-build'));
  });
  for (const scenario of ['engine-down', 'bad-credentials', 'up-failure', 'seed-failure', 'worker-down']) {
    await t.test(`failure is propagated: ${scenario}`, () => {
      const result = runLauncher({ scenario });
      assert.equal(result.status, 1, result.stdout + result.stderr);
      assert.doesNotMatch(result.stdout, /Warehouse is ready/);
      if (scenario === 'bad-credentials' || scenario === 'engine-down') assert.ok(!result.calls.some(c => c.includes('up')));
      if (scenario === 'up-failure') {
        assert.ok(!result.calls.some(c => c.includes('prisma:seed')));
        assert.match(result.stdout, /registry-mirrors/);
      }
      if (scenario === 'seed-failure') assert.ok(!result.calls.some(c => c.includes('git')));
    });
  }
  await t.test('first run creates a template and stops before starting services', () => {
    const result = runLauncher({ withEnv: false });
    assert.equal(result.status, 1);
    assert.equal(readFileSync(path.join(result.fixture, '.env'), 'utf8'), 'EXAMPLE=true\n');
    assert.ok(!result.calls.some(c => c.includes('up')));
  });
  await t.test('stop targets only the configured Compose project and preserves volumes', () => {
    const result = runLauncher({ script: 'stop-all.ps1' });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    assert.ok(result.calls.some(c => c.includes('stop')));
    assert.ok(!result.calls.some(c => c.includes('down') || c.includes('-v') || c.includes('rm')));
  });
});
