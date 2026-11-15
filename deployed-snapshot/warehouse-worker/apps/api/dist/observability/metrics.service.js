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
exports.MetricsService = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const prom_client_1 = require("prom-client");
const prisma_service_1 = require("../prisma/prisma.service");
let MetricsService = class MetricsService {
    prisma;
    registry = new prom_client_1.Registry();
    httpRequests = new prom_client_1.Counter({ name: 'platform_http_requests_total', help: 'HTTP requests processed', labelNames: ['method', 'route', 'status_code'], registers: [this.registry] });
    httpDuration = new prom_client_1.Histogram({ name: 'platform_http_request_duration_seconds', help: 'HTTP request latency', labelNames: ['method', 'route', 'status_code'], buckets: [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5], registers: [this.registry] });
    jobBacklog = new prom_client_1.Gauge({ name: 'platform_job_backlog', help: 'Queued and running PostgreSQL jobs', labelNames: ['status'], registers: [this.registry] });
    refreshSuccessful = new prom_client_1.Gauge({ name: 'platform_metrics_refresh_success', help: 'Whether the latest database-backed metric refresh succeeded', registers: [this.registry] });
    constructor(prisma) {
        this.prisma = prisma;
        this.registry.setDefaultLabels({ service: process.env.RUNTIME_SERVICE_NAME ?? 'unknown' });
        (0, prom_client_1.collectDefaultMetrics)({ register: this.registry, prefix: 'platform_process_' });
    }
    observeHttp(method, route, statusCode, seconds) {
        const labels = { method, route, status_code: String(statusCode) };
        this.httpRequests.inc(labels);
        this.httpDuration.observe(labels, seconds);
    }
    async render() {
        try {
            await this.refreshPlatformGauges();
            this.refreshSuccessful.set(1);
        }
        catch {
            this.refreshSuccessful.set(0);
        }
        return this.registry.metrics();
    }
    async refreshPlatformGauges() {
        const jobs = await this.prisma.$queryRaw(client_1.Prisma.sql `SELECT status, COUNT(*) AS count FROM "Job" WHERE status IN ('QUEUED', 'RUNNING') GROUP BY status`);
        this.jobBacklog.reset();
        jobs.forEach((item) => this.jobBacklog.set({ status: item.status }, Number(item.count)));
    }
};
exports.MetricsService = MetricsService;
exports.MetricsService = MetricsService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], MetricsService);
//# sourceMappingURL=metrics.service.js.map