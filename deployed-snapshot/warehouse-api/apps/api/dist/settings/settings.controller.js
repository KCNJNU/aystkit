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
var __param = (this && this.__param) || function (paramIndex, decorator) {
    return function (target, key) { decorator(target, key, paramIndex); }
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.SettingsController = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const prisma_service_1 = require("../prisma/prisma.service");
const settings_service_1 = require("./settings.service");
class UpdateSettingsDto {
    values;
}
__decorate([
    (0, class_validator_1.IsObject)(),
    __metadata("design:type", Object)
], UpdateSettingsDto.prototype, "values", void 0);
let SettingsController = class SettingsController {
    settings;
    config;
    prisma;
    constructor(settings, config, prisma) {
        this.settings = settings;
        this.config = config;
        this.prisma = prisma;
    }
    async configuration() { const heartbeat = await this.prisma.workerHeartbeat.findFirst({ orderBy: { heartbeatAt: 'desc' } }); return { settings: await this.settings.all(), infrastructure: { postgresql: { configured: Boolean(this.config.get('DATABASE_URL')) }, objectStorage: { configured: Boolean(this.config.get('OBJECT_STORAGE_ENDPOINT')), provider: 'minio', bucket: this.config.get('OBJECT_STORAGE_BUCKET') ?? null }, dvcRemote: { configured: Boolean(this.config.get('DVC_REMOTE_NAME')), provider: 's3-compatible', bucket: this.config.get('DVC_REMOTE_BUCKET') ?? null } }, worker: heartbeat ? { workerId: heartbeat.workerId, version: heartbeat.version, concurrency: heartbeat.concurrency, heartbeatAt: heartbeat.heartbeatAt, status: Date.now() - heartbeat.heartbeatAt.getTime() < 60_000 ? 'healthy' : 'unhealthy' } : { status: 'not_checked' } }; }
    update(body, user) { return this.settings.update(body.values, user.id); }
};
exports.SettingsController = SettingsController;
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    (0, common_1.Get)('configuration'),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", []),
    __metadata("design:returntype", Promise)
], SettingsController.prototype, "configuration", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    (0, common_1.Patch)('settings'),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [UpdateSettingsDto, Object]),
    __metadata("design:returntype", void 0)
], SettingsController.prototype, "update", null);
exports.SettingsController = SettingsController = __decorate([
    (0, common_1.Controller)('v1/system'),
    __metadata("design:paramtypes", [settings_service_1.SettingsService, config_1.ConfigService, prisma_service_1.PrismaService])
], SettingsController);
//# sourceMappingURL=settings.controller.js.map