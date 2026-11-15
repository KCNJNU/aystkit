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
var WorkerRunnerService_1;
Object.defineProperty(exports, "__esModule", { value: true });
exports.WorkerRunnerService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const node_crypto_1 = require("node:crypto");
const jobs_service_1 = require("../jobs/jobs.service");
const prisma_service_1 = require("../prisma/prisma.service");
const settings_service_1 = require("../settings/settings.service");
const job_executor_service_1 = require("./job-executor.service");
const archive_recordings_service_1 = require("../datasets/archive-recordings.service");
let WorkerRunnerService = WorkerRunnerService_1 = class WorkerRunnerService {
    jobs;
    executor;
    prisma;
    settings;
    config;
    recordings;
    logger = new common_1.Logger(WorkerRunnerService_1.name);
    workerId = `${process.env.COMPUTERNAME ?? 'worker'}-${process.pid}-${(0, node_crypto_1.randomUUID)().slice(0, 8)}`;
    stopping = false;
    constructor(jobs, executor, prisma, settings, config, recordings) {
        this.jobs = jobs;
        this.executor = executor;
        this.prisma = prisma;
        this.settings = settings;
        this.config = config;
        this.recordings = recordings;
    }
    async start() {
        const configured = Number(this.config.get('WORKER_CONCURRENCY') ?? await this.settings.get('jobs.workerConcurrency'));
        const concurrency = Math.max(1, Math.min(8, Number.isFinite(configured) ? configured : 2));
        const startedAt = new Date();
        await this.prisma.workerHeartbeat.upsert({ where: { workerId: this.workerId }, update: { version: '2.1.0', concurrency, startedAt, heartbeatAt: new Date() }, create: { workerId: this.workerId, version: '2.1.0', concurrency, startedAt, heartbeatAt: new Date() } });
        this.logger.log(`Worker ${this.workerId} started with concurrency ${concurrency}`);
        const heartbeat = setInterval(() => void this.prisma.workerHeartbeat.update({ where: { workerId: this.workerId }, data: { heartbeatAt: new Date() } }).catch(() => undefined), 10_000);
        const active = new Set();
        const stop = () => { this.stopping = true; };
        process.once('SIGINT', stop);
        process.once('SIGTERM', stop);
        try {
            while (!this.stopping) {
                await this.recordings.scanClosedRecordings(this.workerId).catch(error => this.logger.warn(`Archive recording scan deferred: ${error instanceof Error ? error.message : 'scan failed'}`));
                while (active.size < concurrency && !this.stopping) {
                    const job = await this.jobs.claim(this.workerId);
                    if (!job)
                        break;
                    const task = this.executor.execute(job, this.workerId).finally(() => active.delete(task));
                    active.add(task);
                }
                if (active.size)
                    await Promise.race([...active, delay(1_000)]);
                else
                    await delay(1_000);
            }
            await Promise.allSettled(active);
        }
        finally {
            clearInterval(heartbeat);
            await this.prisma.workerHeartbeat.deleteMany({ where: { workerId: this.workerId } });
        }
    }
};
exports.WorkerRunnerService = WorkerRunnerService;
exports.WorkerRunnerService = WorkerRunnerService = WorkerRunnerService_1 = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [jobs_service_1.JobsService, job_executor_service_1.JobExecutorService, prisma_service_1.PrismaService, settings_service_1.SettingsService, config_1.ConfigService, archive_recordings_service_1.ArchiveRecordingsService])
], WorkerRunnerService);
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
//# sourceMappingURL=worker-runner.service.js.map