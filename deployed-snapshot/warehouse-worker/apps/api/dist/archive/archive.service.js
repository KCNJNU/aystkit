"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.ArchiveService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const promises_1 = require("node:fs/promises");
const node_path_1 = require("node:path");
const node_crypto_1 = require("node:crypto");
const node_fs_1 = require("node:fs");
const archive_camera_status_1 = require("./archive-camera-status");
let ArchiveService = class ArchiveService {
    config;
    cameraMonitor = new archive_camera_status_1.ArchiveCameraMonitor();
    constructor(config) {
        this.config = config;
    }
    directory() {
        const value = this.config.get('GARAGE_ARCHIVE_CONTROL_DIR');
        if (!value || !value.startsWith('/'))
            throw new common_1.ServiceUnavailableException('地库采集服务尚未配置');
        return value;
    }
    async json(name) {
        try {
            const file = await (0, promises_1.open)((0, node_path_1.join)(this.directory(), name), node_fs_1.constants.O_RDONLY | node_fs_1.constants.O_NOFOLLOW);
            try {
                if ((await file.stat()).size > 512 * 1024)
                    return null;
                return JSON.parse(await file.readFile('utf8'));
            }
            finally {
                await file.close();
            }
        }
        catch {
            return null;
        }
    }
    async status() {
        const configured = Boolean(this.config.get('GARAGE_ARCHIVE_CONTROL_DIR'));
        const [gate, original, source] = await Promise.all([this.json('garage.json'), this.json('status.json'), this.json('source.json')]);
        const recorder = original ? { ...original } : null;
        const stopped = recorder?.state === 'stopped' || recorder?.state === 'stop_failed';
        const stopFailed = recorder?.state === 'stop_failed';
        if (recorder && !stopped) {
            const sourceTime = typeof source?.updatedAt === 'string' ? Date.parse(source.updatedAt) : NaN;
            const sourceFresh = Number.isFinite(sourceTime) && Date.now() - sourceTime >= -5_000 && Date.now() - sourceTime < 25_000;
            const camera = source?.camera;
            const cameraTime = typeof camera?.updatedAt === 'string' ? Date.parse(camera.updatedAt) : NaN;
            const cameraFresh = sourceFresh && source?.connected === true && Number.isFinite(cameraTime)
                && Date.now() - cameraTime >= -5_000 && Date.now() - cameraTime < 20_000;
            const acknowledged = sourceFresh && source?.gateAcknowledged === true
                && (source?.gateRevision ?? null) === (gate?.updatedAt ?? null) && source?.gateEnabled === gate?.enabled;
            recorder.camera = { state: cameraFresh && Object.keys(camera?.store?.active ?? {}).length ? 'recording' : 'waiting_source',
                gateAcknowledged: acknowledged,
                sourceOnline: sourceFresh && source?.connected === true,
                report: cameraFresh ? camera : null };
            if (source && !acknowledged) {
                const store = (recorder.store ?? {});
                recorder.store = { ...store, errors: { ...store.errors,
                        sourceSync: '相机源端尚未确认当前地库开关，请检查源机连接；源端离线时按上次地库设置本地保存' } };
            }
        }
        const updated = typeof recorder?.updatedAt === 'string' ? Date.parse(recorder.updatedAt) : NaN;
        const running = !stopped && Number.isFinite(updated) && Date.now() - updated >= -5_000 && Date.now() - updated < 20_000;
        const enabled = gate?.scope === 'garage' && gate?.schemaVersion === 1 && gate?.enabled === true;
        const store = recorder?.store;
        const hasData = Boolean((store?.active && Object.keys(store.active).length)
            || recorder?.camera?.state === 'recording');
        return { configured, enabled, running, cameras: this.cameraMonitor.snapshot(source, gate), updatedAt: (running || stopFailed) && Number.isFinite(updated) ? recorder?.updatedAt : null,
            state: stopFailed ? 'stop_failed' : !running ? 'offline' : !enabled ? 'disabled' : hasData ? 'collecting' : 'waiting_sources',
            datasetPattern: '地库_YYYY-MM-DD / 传感器 / 传感器_小时:分钟-小时:分钟',
            recorder: running || stopFailed ? recorder : null };
    }
    async setGarage(enabled, actorId) {
        const directory = this.directory();
        await (0, promises_1.mkdir)(directory, { recursive: true, mode: 0o700 });
        const temporary = (0, node_path_1.join)(directory, `.garage-${(0, node_crypto_1.randomUUID)()}.tmp`);
        try {
            const file = await (0, promises_1.open)(temporary, 'wx', 0o600);
            try {
                await file.writeFile(JSON.stringify({ schemaVersion: 1, scope: 'garage', enabled,
                    updatedAt: new Date().toISOString(), updatedById: actorId, expiresAt: null }) + '\n');
                await file.sync();
            }
            finally {
                await file.close();
            }
            await (0, promises_1.rename)(temporary, (0, node_path_1.join)(directory, 'garage.json'));
        }
        catch {
            throw new common_1.ServiceUnavailableException('地库采集开关保存失败');
        }
        finally {
            await (0, promises_1.unlink)(temporary).catch(() => undefined);
        }
        return this.status();
    }
};
exports.ArchiveService = ArchiveService;
exports.ArchiveService = ArchiveService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [config_1.ConfigService])
], ArchiveService);
//# sourceMappingURL=archive.service.js.map