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
exports.HealthService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const promises_1 = require("node:fs/promises");
const redis_1 = require("redis");
const prisma_service_1 = require("../prisma/prisma.service");
const object_storage_1 = require("../storage/object-storage");
const ai_provider_1 = require("../ai/ai-provider");
let HealthService = class HealthService {
    prisma;
    storage;
    config;
    ai;
    cached;
    pending;
    constructor(prisma, storage, config, ai) {
        this.prisma = prisma;
        this.storage = storage;
        this.config = config;
        this.ai = ai;
    }
    check() {
        if (this.cached && Date.now() - Date.parse(this.cached.timestamp) < 15000)
            return Promise.resolve(this.cached);
        this.pending ??= this.probe().then(value => this.cached = value).finally(() => { this.pending = undefined; });
        return this.pending;
    }
    async probe() {
        const [postgresql, redis, objectStorage, qwen, disk] = await Promise.all([
            this.measure(async () => { await this.prisma.$queryRaw `SELECT 1`; }, 'PostgreSQL'),
            this.config.get('REDIS_HOST') ? this.measure(() => this.redis(), 'Redis') : Promise.resolve({ status: 'not_configured', message: 'Redis not configured', latency_ms: 0, checked_at: new Date().toISOString() }),
            this.measure(async () => { const result = await this.storage.checkHealth(); if (!result.reachable)
                throw new Error(); }, 'Object Storage'),
            this.ai.healthCheck(),
            (0, promises_1.statfs)(process.cwd()).then(value => ({ total_bytes: String(BigInt(value.blocks) * BigInt(value.bsize)), available_bytes: String(BigInt(value.bavail) * BigInt(value.bsize)) })).catch(() => null),
        ]);
        const timestamp = new Date().toISOString();
        const services = { api: { status: 'healthy', message: 'NestJS API responding', latency_ms: 0, checked_at: timestamp }, postgresql, redis, objectStorage, qwen };
        return { status: Object.values(services).every(value => value.status === 'healthy') ? 'healthy' : 'degraded', timestamp, services, version: this.config.get('APP_VERSION') || '0.1.0', environment: this.config.get('DEPLOYMENT_ENVIRONMENT') || 'development', uptime_seconds: Math.floor(process.uptime()), disk };
    }
    async measure(operation, label) {
        const start = Date.now();
        let timer;
        try {
            await Promise.race([operation(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error()), 3000); })]);
            return { status: Date.now() - start > 1500 ? 'slow' : 'healthy', message: `${label} connected`, latency_ms: Date.now() - start, checked_at: new Date().toISOString() };
        }
        catch {
            return { status: 'connection_failed', message: `${label} connection failed or timed out`, latency_ms: Date.now() - start, checked_at: new Date().toISOString() };
        }
        finally {
            clearTimeout(timer);
        }
    }
    async redis() {
        const client = (0, redis_1.createClient)({ socket: { host: this.config.getOrThrow('REDIS_HOST'), port: Number(this.config.getOrThrow('REDIS_PORT')), connectTimeout: 2500, reconnectStrategy: false } });
        client.on('error', () => undefined);
        const deadline = setTimeout(() => client.destroy(), 2800);
        try {
            await client.connect();
            await client.ping();
        }
        finally {
            clearTimeout(deadline);
            if (client.isOpen)
                client.destroy();
        }
    }
};
exports.HealthService = HealthService;
exports.HealthService = HealthService = __decorate([
    (0, common_1.Injectable)(),
    __param(1, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, Object, config_1.ConfigService, ai_provider_1.AIProvider])
], HealthService);
//# sourceMappingURL=health.service.js.map