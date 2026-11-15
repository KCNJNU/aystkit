import { ConfigService } from '@nestjs/config';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Reflector } from '@nestjs/core';
import { UserRole } from '@prisma/client';
import { ArchiveService } from './archive.service';
import { ArchiveController } from './archive.controller';
import { ROLES } from '../auth/auth.decorators';

describe('garage native archive control', () => {
  let dir: string;
  let service: ArchiveService;
  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), 'garage-archive-'));
    service = new ArchiveService(new ConfigService({ GARAGE_ARCHIVE_CONTROL_DIR: dir }));
  });
  afterEach(async () => { await rm(dir, { recursive: true, force: true }); });
  it('defaults disabled, reports stopped recorders and persists the garage choice across API restarts', async () => {
    expect(await service.status()).toMatchObject({ enabled: false, running: false, state: 'offline' });
    await service.setGarage(true, 'operator-id');
    expect(JSON.parse(await readFile(join(dir, 'garage.json'), 'utf8'))).toMatchObject({ enabled: true, scope: 'garage', updatedById: 'operator-id' });
    const restarted = new ArchiveService(new ConfigService({ GARAGE_ARCHIVE_CONTROL_DIR: dir }));
    expect(await restarted.status()).toMatchObject({ enabled: true, running: false });
    await restarted.setGarage(false, 'operator-id');
    expect(await service.status()).toMatchObject({ enabled: false });
  });
  it('distinguishes waiting sources, actual recording and stale or malformed heartbeat', async () => {
    await service.setGarage(true, 'actor');
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date().toISOString(), store: { active: {} } }));
    expect(await service.status()).toMatchObject({ running: true, state: 'waiting_sources' });
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date().toISOString(), store: { active: { lidar: { messages: 1 } } } }));
    expect(await service.status()).toMatchObject({ running: true, state: 'collecting' });
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date(Date.now() - 30_000).toISOString() }));
    expect(await service.status()).toMatchObject({ running: false, recorder: null });
    await writeFile(join(dir, 'garage.json'), '{bad');
    expect(await service.status()).toMatchObject({ enabled: false });
  });
  it('requires administrator role for both state and gate control', () => {
    expect(new Reflector().get(ROLES, ArchiveController)).toEqual([UserRole.ADMIN]);
  });
  it.each(['stopped', 'stop_failed'])('does not treat a fresh %s report as a live recorder', async state => {
    await service.setGarage(true, 'actor');
    const updatedAt = new Date().toISOString();
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt, state, store: null, ros: null,
      errors: state === 'stop_failed' ? { rosClose: 'WriterShutdownIncomplete' } : {} }));
    await writeFile(join(dir, 'source.json'), JSON.stringify({ updatedAt, connected: true,
      camera: { updatedAt, store: { active: { camera1: { frames: 2 } } } } }));
    const value = await service.status();
    expect(value).toMatchObject({ enabled: true, running: false, state: state === 'stopped' ? 'offline' : 'stop_failed' });
    if (state === 'stopped') expect(value.recorder).toBeNull();
    else {
      expect(value.recorder).toMatchObject({ store: null, errors: { rosClose: 'WriterShutdownIncomplete' } });
      expect(value.recorder).not.toHaveProperty('camera');
    }
  });
  it('keeps a failed shutdown visible until a new live heartbeat replaces it', async () => {
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date(Date.now() - 60_000).toISOString(),
      state: 'stop_failed', store: null, errors: { rosClose: 'WriterShutdownIncomplete' } }));
    expect(await service.status()).toMatchObject({ running: false, state: 'stop_failed' });
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date().toISOString(), store: { active: {} } }));
    expect(await service.status()).toMatchObject({ running: true, state: 'disabled' });
  });
  it('reports real native camera activity and an unacknowledged source off-switch', async () => {
    await service.setGarage(true, 'actor');
    const gate = JSON.parse(await readFile(join(dir, 'garage.json'), 'utf8'));
    const updatedAt = new Date().toISOString();
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt, store: { active: {} } }));
    await writeFile(join(dir, 'source.json'), JSON.stringify({ updatedAt, connected: true,
      gateAcknowledged: true, gateEnabled: true, gateRevision: gate.updatedAt,
      camera: { updatedAt, store: { active: { camera1: { frames: 2 } } } } }));
    expect(await service.status()).toMatchObject({ state: 'collecting', recorder: { camera: { state: 'recording', gateAcknowledged: true } } });
    await service.setGarage(false, 'actor');
    expect(await service.status()).toMatchObject({ state: 'disabled', recorder: { camera: { gateAcknowledged: false },
      store: { errors: { sourceSync: expect.stringContaining('尚未确认') } } } });
  });
  it('rejects unconfigured control writes and ignores oversized status files', async () => {
    const unconfigured = new ArchiveService(new ConfigService({}));
    await expect(unconfigured.setGarage(true, 'actor')).rejects.toThrow('尚未配置');
    await writeFile(join(dir, 'status.json'), ' '.repeat(512 * 1024 + 1));
    expect(await service.status()).toMatchObject({ running: false, recorder: null });
  });
  it('returns all six camera rows even when the local recorder is unavailable', async () => {
    const value = await service.status();
    expect(value.cameras.map(row => row.id)).toEqual(['camera2', 'camera1', 'camera3', 'camera4', 'berxelColor', 'berxelDepth']);
    expect(value.cameras.every(row => row.acquisition === 'unknown' && row.receivedFrames === null)).toBe(true);
  });
  it('keeps capture detection visible independently of the disabled archive switch', async () => {
    await service.setGarage(false, 'actor');
    const gate = JSON.parse(await readFile(join(dir, 'garage.json'), 'utf8'));
    const updatedAt = new Date().toISOString();
    await writeFile(join(dir, 'source.json'), JSON.stringify({ updatedAt, connected: true,
      gateAcknowledged: true, gateEnabled: false, gateRevision: gate.updatedAt,
      capture: { updatedAt, cameras: { camera2: { state: 'capturing', frames: 7, ageS: 0.05 } } } }));
    const value = await service.status();
    expect(value.cameras[0]).toMatchObject({ id: 'camera2', acquisition: 'capturing', recording: 'disabled', receivedFrames: 7 });
    expect(value.recorder).toBeNull();
  });

  async function desktopLease(overrides: Record<string, unknown> = {}) {
    const value = { schemaVersion: 1, scope: 'garage', enabled: true,
      updatedAt: new Date().toISOString(), expiresAt: Date.now() / 1000 + 20,
      desktopSession: 'a'.repeat(32), ...overrides };
    await writeFile(join(dir, 'desktop-lease.json'), JSON.stringify(value));
    return value;
  }

  it('uses the independent desktop lease for effective recording and source acknowledgements without touching the web choice', async () => {
    await writeFile(join(dir, 'garage.json'), JSON.stringify({ schemaVersion: 1, scope: 'garage',
      enabled: false, updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: null }));
    const lease = await desktopLease();
    const webBefore = await readFile(join(dir, 'garage.json'), 'utf8');
    const leaseBefore = await readFile(join(dir, 'desktop-lease.json'), 'utf8');
    const updatedAt = new Date().toISOString();
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt, store: { active: {} } }));
    await writeFile(join(dir, 'source.json'), JSON.stringify({ updatedAt, connected: true,
      gateAcknowledged: true, gateEnabled: true, gateRevision: lease.updatedAt,
      capture: { updatedAt, cameras: { camera2: { state: 'capturing', frames: 7, ageS: 0.05 } } },
      camera: { updatedAt, store: { enabled: true, active: { camera2: { frames: 7 } },
        cameras: { camera2: { writtenFrames: 7 } } } } }));
    const value = await service.status();
    expect(value).toMatchObject({ enabled: true, webEnabled: false, desktopEnabled: true, captureSource: 'desktop',
      state: 'collecting', recorder: { camera: { gateAcknowledged: true, state: 'recording' } } });
    expect(value.recorder?.store).not.toHaveProperty('errors.sourceSync');
    expect(value.cameras[0]).toMatchObject({ id: 'camera2', acquisition: 'capturing', recording: 'waiting' });
    expect(value.cameras[0].detail).not.toContain('尚未确认');
    expect(JSON.stringify(value)).not.toContain(lease.desktopSession);
    expect(await readFile(join(dir, 'garage.json'), 'utf8')).toBe(webBefore);
    expect(await readFile(join(dir, 'desktop-lease.json'), 'utf8')).toBe(leaseBefore);
  });

  it('prefers an enabled web declaration and setGarage only changes that web declaration', async () => {
    const lease = await desktopLease();
    const before = await readFile(join(dir, 'desktop-lease.json'), 'utf8');
    expect(await service.status()).toMatchObject({ enabled: true, webEnabled: false, desktopEnabled: true, captureSource: 'desktop' });
    expect(await service.setGarage(true, 'actor')).toMatchObject({ enabled: true, webEnabled: true, desktopEnabled: true, captureSource: 'web' });
    expect(await service.setGarage(false, 'actor')).toMatchObject({ enabled: true, webEnabled: false, desktopEnabled: true, captureSource: 'desktop' });
    expect(JSON.parse(await readFile(join(dir, 'garage.json'), 'utf8'))).toMatchObject({ enabled: false, updatedById: 'actor' });
    expect(JSON.parse(before).desktopSession).toBe(lease.desktopSession);
    expect(await readFile(join(dir, 'desktop-lease.json'), 'utf8')).toBe(before);
  });

  it('keeps camera write-progress evidence across lease heartbeats with the same revision', async () => {
    const lease = await desktopLease();
    async function sourceSample(updatedAt: string, frames: number) {
      await writeFile(join(dir, 'source.json'), JSON.stringify({ updatedAt, connected: true,
        gateAcknowledged: true, gateEnabled: true, gateRevision: lease.updatedAt,
        capture: { updatedAt, cameras: { camera2: { state: 'capturing', frames, ageS: 0.05 } } },
        camera: { updatedAt, store: { enabled: true, active: { camera2: { frames } },
          cameras: { camera2: { writtenFrames: frames } } } } }));
    }
    await sourceSample(new Date(Date.now() - 1000).toISOString(), 7);
    expect((await service.status()).cameras[0].recording).toBe('waiting');
    await writeFile(join(dir, 'desktop-lease.json'), JSON.stringify({ ...lease,
      renewedAt: new Date().toISOString(), expiresAt: Date.now() / 1000 + 20 }));
    await sourceSample(new Date().toISOString(), 8);
    expect((await service.status()).cameras[0]).toMatchObject({ acquisition: 'capturing', recording: 'recording' });
  });

  it.each([
    ['expired', () => ({ expiresAt: Date.now() / 1000 - 1 })],
    ['unbounded', () => ({ expiresAt: null })],
    ['missing expiry', () => ({ expiresAt: undefined })],
    ['too long', () => ({ expiresAt: Date.now() / 1000 + 61 })],
    ['string expiry', () => ({ expiresAt: String(Date.now() / 1000 + 20) })],
    ['invalid session', () => ({ desktopSession: 'not-a-session' })],
    ['uppercase session', () => ({ desktopSession: 'A'.repeat(32) })],
    ['wrong scope', () => ({ scope: 'other' })],
    ['wrong schema', () => ({ schemaVersion: 2 })],
    ['disabled', () => ({ enabled: false })],
  ] as const)('ignores a %s desktop lease', async (_label, overrides) => {
    await desktopLease(overrides());
    expect(await service.status()).toMatchObject({ enabled: false, webEnabled: false, desktopEnabled: false, captureSource: null });
  });

  it('fails closed after a desktop heartbeat expires while preserving a valid web declaration', async () => {
    const lease = await desktopLease();
    await writeFile(join(dir, 'status.json'), JSON.stringify({ updatedAt: new Date().toISOString(), store: { active: {} } }));
    expect(await service.status()).toMatchObject({ enabled: true, state: 'waiting_sources' });
    await writeFile(join(dir, 'desktop-lease.json'), JSON.stringify({ ...lease, expiresAt: Date.now() / 1000 - 1 }));
    expect(await service.status()).toMatchObject({ enabled: false, state: 'disabled', captureSource: null });
    await service.setGarage(true, 'actor');
    expect(await service.status()).toMatchObject({ enabled: true, webEnabled: true, captureSource: 'web' });
  });

  it('falls back from an expired web declaration to a valid desktop lease', async () => {
    await writeFile(join(dir, 'garage.json'), JSON.stringify({ schemaVersion: 1, scope: 'garage',
      enabled: true, updatedAt: new Date().toISOString(), expiresAt: Date.now() / 1000 - 1 }));
    expect(await service.status()).toMatchObject({ enabled: false, webEnabled: false, captureSource: null });
    await desktopLease();
    expect(await service.status()).toMatchObject({ enabled: true, webEnabled: false, captureSource: 'desktop' });
  });

});
