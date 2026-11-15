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
exports.SettingsService = exports.SETTING_DEFAULTS = void 0;
const common_1 = require("@nestjs/common");
const prisma_service_1 = require("../prisma/prisma.service");
exports.SETTING_DEFAULTS = {
    'upload.jsonMaxBytes': 0,
    'upload.imageMaxBytes': 0,
    'upload.pcdMaxBytes': 0,
    'upload.mapMaxBytes': 0,
    'upload.yamlMaxBytes': 0,
    'upload.logMaxBytes': 0,
    'download.presignedTtlSeconds': 900,
    'jobs.workerConcurrency': 2,
    'notifications.jobSuccessEnabled': true,
};
let SettingsService = class SettingsService {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    async all() { const rows = await this.prisma.systemSetting.findMany(); const stored = new Map(rows.map((row) => [row.key, row.value])); return Object.fromEntries(Object.entries(exports.SETTING_DEFAULTS).map(([key, value]) => [key, stored.get(key) ?? value])); }
    async get(key) { const row = await this.prisma.systemSetting.findUnique({ where: { key } }); return (row?.value ?? exports.SETTING_DEFAULTS[key]); }
    async update(values, userId) {
        const entries = Object.entries(values);
        for (const [key, value] of entries)
            this.validate(key, value);
        await this.prisma.$transaction(entries.map(([key, value]) => this.prisma.systemSetting.upsert({ where: { key }, update: { value: value, updatedById: userId }, create: { key, value: value, updatedById: userId } })));
        return this.all();
    }
    validate(key, value) {
        if (!(key in exports.SETTING_DEFAULTS))
            throw new common_1.BadRequestException(`Unsupported setting: ${key}`);
        const expected = exports.SETTING_DEFAULTS[key];
        if (typeof value !== typeof expected)
            throw new common_1.BadRequestException(`Invalid setting type: ${key}`);
        if (typeof value === 'number') {
            if (!Number.isSafeInteger(value) || (key.startsWith('upload.') ? value < 0 : value <= 0))
                throw new common_1.BadRequestException(`Invalid setting value: ${key}`);
            if (key === 'jobs.workerConcurrency' && value > 8)
                throw new common_1.BadRequestException('Worker concurrency must be between 1 and 8');
            if (key === 'download.presignedTtlSeconds' && (value < 60 || value > 3600))
                throw new common_1.BadRequestException('Download TTL must be between 60 and 3600 seconds');
        }
    }
};
exports.SettingsService = SettingsService;
exports.SettingsService = SettingsService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], SettingsService);
//# sourceMappingURL=settings.service.js.map