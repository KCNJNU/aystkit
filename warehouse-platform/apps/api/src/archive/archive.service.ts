import { Injectable, ServiceUnavailableException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { mkdir, open, rename, unlink } from 'node:fs/promises';
import { join, isAbsolute, normalize } from 'node:path';
import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { ArchiveCameraMonitor } from './archive-camera-status';

/** A host-local read-only recorder shares just its gate and bounded status files.
 * No device handles, robot controls, storage credentials or paths reach clients.
 */
@Injectable()
export class ArchiveService {
  private readonly robots = new Map<string, ArchiveService>();
  private readonly cameraMonitor = new ArchiveCameraMonitor();
  constructor(private readonly config: ConfigService) {}
  private forRobot(robotId?: string) {
    const raw = this.config.get<string>('GARAGE_ARCHIVE_ROBOTS_JSON');
    let directories: Record<string, string> = {};
    try {
      directories = raw ? JSON.parse(raw) : {};
      if (!directories || typeof directories !== 'object' || Array.isArray(directories)
          || Object.keys(directories).length > 10 || Object.values(directories).some(path =>
            typeof path !== 'string' || !isAbsolute(path) || normalize(path) !== path)
          || new Set(Object.values(directories)).size !== Object.values(directories).length) throw new Error();
      const defaultRobot = this.config.get<string>('GARAGE_ARCHIVE_DEFAULT_ROBOT_ID');
      const defaultDirectory = this.config.get<string>('GARAGE_ARCHIVE_CONTROL_DIR');
      if (defaultRobot && defaultDirectory && Object.entries(directories).some(([id, path]) =>
        id !== defaultRobot && path === defaultDirectory)) throw new Error();
    } catch { throw new ServiceUnavailableException('多机器人采集配置无效'); }
    // An unselected fleet view cannot read or change the legacy car's gate.
    if (!robotId && !Object.keys(directories).length) return this;
    const directory = (robotId ? directories[robotId] : undefined) ?? (robotId && robotId === this.config.get<string>('GARAGE_ARCHIVE_DEFAULT_ROBOT_ID')
      ? this.config.get<string>('GARAGE_ARCHIVE_CONTROL_DIR') : undefined);
    const key = robotId + ':' + (directory ?? '');
    if (!this.robots.has(key)) {
      if (this.robots.size >= 20) this.robots.clear();
      this.robots.set(key, new ArchiveService(new ConfigService({ GARAGE_ARCHIVE_CONTROL_DIR: directory })));
    }
    return this.robots.get(key)!;
  }
  private directory() {
    const value = this.config.get<string>('GARAGE_ARCHIVE_CONTROL_DIR');
    if (!value || !value.startsWith('/')) throw new ServiceUnavailableException('地库采集服务尚未配置');
    return value;
  }
  private async json(name: string) {
    try {
      const file = await open(join(this.directory(), name), constants.O_RDONLY | constants.O_NOFOLLOW);
      try {
        if ((await file.stat()).size > 512 * 1024) return null;
        const value: unknown = JSON.parse(await file.readFile('utf8'));
        return value && typeof value === 'object' && !Array.isArray(value)
          ? value as Record<string, unknown> : null;
      } finally { await file.close(); }
    } catch { return null; }
  }
  private gateEnabled(gate: Record<string, unknown> | null, now: number) {
    const raw = gate?.expiresAt;
    const expiry = typeof raw === 'number' || (typeof raw === 'string' && raw.trim()) ? Number(raw) : NaN;
    return gate?.scope === 'garage' && gate?.schemaVersion === 1 && gate?.enabled === true
      && (raw === undefined || raw === null || (Number.isFinite(expiry) && expiry > now / 1000));
  }
  status(robotId?: string) { return this.forRobot(robotId).localStatus(); }
  private async localStatus() {
    const configured = Boolean(this.config.get<string>('GARAGE_ARCHIVE_CONTROL_DIR'));
    const [webGate, desktopLease, original, source] = await Promise.all([
      this.json('garage.json'), this.json('desktop-lease.json'), this.json('status.json'), this.json('source.json')]);
    const now = Date.now();
    const webEnabled = this.gateEnabled(webGate, now);
    const desktopEnabled = this.gateEnabled(desktopLease, now)
      && typeof desktopLease?.expiresAt === 'number' && desktopLease.expiresAt <= now / 1000 + 60
      && typeof desktopLease?.desktopSession === 'string' && /^[a-f0-9]{32}$/.test(desktopLease.desktopSession);
    // Match the recorder's GarageGate selection without modifying either
    // owner's declaration. An expired desktop heartbeat must fail closed.
    const gate = webEnabled ? webGate : desktopEnabled ? desktopLease : { ...webGate, enabled: false };
    const enabled = webEnabled || desktopEnabled;
    const captureSource = webEnabled ? 'web' : desktopEnabled ? 'desktop' : null;
    const recorder = original ? { ...original } : null;
    const stopped = recorder?.state === 'stopped' || recorder?.state === 'stop_failed';
    const stopFailed = recorder?.state === 'stop_failed';
    if (recorder && !stopped) {
      const sourceTime = typeof source?.updatedAt === 'string' ? Date.parse(source.updatedAt) : NaN;
      const sourceFresh = Number.isFinite(sourceTime) && Date.now() - sourceTime >= -5_000 && Date.now() - sourceTime < 25_000;
      const camera = source?.camera as { updatedAt?: string; store?: { active?: Record<string, unknown> } } | undefined;
      const cameraTime = typeof camera?.updatedAt === 'string' ? Date.parse(camera.updatedAt) : NaN;
      const cameraFresh = sourceFresh && source?.connected === true && Number.isFinite(cameraTime)
        && Date.now() - cameraTime >= -5_000 && Date.now() - cameraTime < 20_000;
      const acknowledged = sourceFresh && source?.gateAcknowledged === true
        && (source?.gateRevision ?? null) === (gate?.updatedAt ?? null) && source?.gateEnabled === gate?.enabled;
      recorder.camera = { state: cameraFresh && Object.keys(camera?.store?.active ?? {}).length ? 'recording' : 'waiting_source',
        gateAcknowledged: acknowledged,
        sourceOnline: sourceFresh && source?.connected === true,
        report: cameraFresh ? camera : null };
      // A disconnected source can keep recording its last garage declaration.
      // Surface an unacknowledged off-switch instead of claiming it stopped.
      if (source && !acknowledged) {
        const store = (recorder.store ?? {}) as { errors?: Record<string, string> };
        recorder.store = { ...store, errors: { ...store.errors,
          sourceSync: '相机源端尚未确认当前地库开关，请检查源机连接；源端离线时按上次地库设置本地保存' } };
      }
    }
    const updated = typeof recorder?.updatedAt === 'string' ? Date.parse(recorder.updatedAt) : NaN;
    const running = !stopped && Number.isFinite(updated) && Date.now() - updated >= -5_000 && Date.now() - updated < 20_000;
    const store = recorder?.store as { active?: Record<string, unknown> } | undefined;
    const hasData = Boolean((store?.active && Object.keys(store.active).length)
      || (recorder?.camera as { state?: string } | undefined)?.state === 'recording');
    return { configured, enabled, webEnabled, desktopEnabled, captureSource, running, cameras: this.cameraMonitor.snapshot(source, gate), updatedAt: (running || stopFailed) && Number.isFinite(updated) ? recorder?.updatedAt : null,
      state: stopFailed ? 'stop_failed' : !running ? 'offline' : !enabled ? 'disabled' : hasData ? 'collecting' : 'waiting_sources',
      datasetPattern: '地库_YYYY-MM-DD / 传感器 / 传感器_小时:分钟-小时:分钟',
      recorder: running || stopFailed ? recorder : null };
  }
  setGarage(enabled: boolean, actorId: string, robotId?: string) { return this.forRobot(robotId).localSetGarage(enabled, actorId); }
  private async localSetGarage(enabled: boolean, actorId: string) {
    const directory = this.directory();
    await mkdir(directory, { recursive: true, mode: 0o700 });
    const temporary = join(directory, `.garage-${randomUUID()}.tmp`);
    try {
      const file = await open(temporary, 'wx', 0o600);
      try {
        await file.writeFile(JSON.stringify({ schemaVersion: 1, scope: 'garage', enabled,
          updatedAt: new Date().toISOString(), updatedById: actorId, expiresAt: null }) + '\n');
        await file.sync();
      } finally { await file.close(); }
      await rename(temporary, join(directory, 'garage.json'));
    } catch {
      throw new ServiceUnavailableException('地库采集开关保存失败');
    } finally { await unlink(temporary).catch(() => undefined); }
    return this.status();
  }
}
