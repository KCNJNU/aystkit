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
exports.JobExecutorService = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const node_crypto_1 = require("node:crypto");
const dataset_publish_service_1 = require("../datasets/dataset-publish.service");
const jobs_service_1 = require("../jobs/jobs.service");
const storage_operations_service_1 = require("../storage/storage-operations.service");
const uploads_service_1 = require("../uploads/uploads.service");
const retention_cleanup_service_1 = require("./retention-cleanup.service");
const archive_recordings_service_1 = require("../datasets/archive-recordings.service");
let JobExecutorService = class JobExecutorService {
    jobs;
    publisher;
    storage;
    uploads;
    retention;
    recordings;
    constructor(jobs, publisher, storage, uploads, retention, recordings) {
        this.jobs = jobs;
        this.publisher = publisher;
        this.storage = storage;
        this.uploads = uploads;
        this.retention = retention;
        this.recordings = recordings;
    }
    async execute(job, workerId) {
        let activeStep;
        const leaseRequired = job.type === client_1.JobType.DATASET_PUBLISH || job.type === client_1.JobType.RECOVERY_VERIFY;
        const owner = job.type === client_1.JobType.ARCHIVE_VIDEO_ASSEMBLE || leaseRequired ? workerId : undefined;
        const leaseOwner = `${workerId}:${job.id}:${(0, node_crypto_1.randomUUID)()}`;
        const cancellation = new AbortController();
        let leaseAcquired = false;
        let leaseTimer;
        let renewing;
        const loseLease = () => cancellation.abort(new common_1.ServiceUnavailableException('Git/DVC publication lease was lost'));
        const renewLease = async () => {
            cancellation.signal.throwIfAborted();
            if (!renewing) {
                renewing = this.jobs.heartbeatLease('git-dvc-publication', leaseOwner)
                    .then(result => { if (result.count !== 1)
                    loseLease(); })
                    .catch(() => { loseLease(); })
                    .finally(() => { renewing = undefined; });
            }
            await renewing;
            cancellation.signal.throwIfAborted();
        };
        const runStep = async (name, action) => {
            if (leaseRequired)
                await renewLease();
            activeStep = name;
            if (await this.jobs.isCancelRequested(job.id))
                throw new JobCancelledError();
            const shouldRun = await this.jobs.beginStep(job.id, name, owner);
            const result = await action();
            if (leaseRequired)
                await renewLease();
            if (shouldRun)
                await this.jobs.completeStep(job.id, name, undefined, owner);
            activeStep = undefined;
            return result;
        };
        const jobHeartbeat = setInterval(() => void this.jobs.heartbeat(job.id, workerId)
            .then(result => { if (leaseRequired && result.count !== 1)
            loseLease(); })
            .catch(() => { if (leaseRequired)
            loseLease(); }), 10_000);
        try {
            if (leaseRequired) {
                leaseAcquired = await this.jobs.acquireLease('git-dvc-publication', leaseOwner);
                if (!leaseAcquired) {
                    await this.jobs.defer(job.id, workerId);
                    return;
                }
                await renewLease();
                leaseTimer = setInterval(() => void renewLease().catch(() => undefined), 10_000);
            }
            let result;
            const payload = job.payload;
            if (job.type === client_1.JobType.DATASET_PUBLISH)
                result = await this.publisher.publish(this.requireString(payload.versionId), runStep, cancellation.signal);
            else if (job.type === client_1.JobType.RECOVERY_VERIFY)
                result = await this.publisher.recoveryVerify(this.requireString(payload.versionId), runStep, cancellation.signal);
            else if (job.type === client_1.JobType.STORAGE_SCAN) {
                await runStep('Load Database Index', async () => undefined);
                await runStep('List ObjectStorage', async () => undefined);
                result = await runStep('Compare Objects', () => this.storage.executeScan(this.requireString(payload.runId)));
                await runStep('Persist Issues', async () => undefined);
            }
            else if (job.type === client_1.JobType.STORAGE_ORPHAN_DELETE) {
                const issueIds = this.requireStrings(payload.issueIds);
                await runStep('Validate Issues', async () => undefined);
                result = await runStep('Delete Objects', () => this.storage.executeOrphanDelete(issueIds));
                await runStep('Resolve Issues', async () => undefined);
            }
            else if (job.type === client_1.JobType.DATASET_DELETE) {
                result = await runStep('Delete Dataset Objects', () => this.storage.executeDatasetDelete(this.requireString(payload.datasetId), this.requireStrings(payload.objectKeys)));
            }
            else if (job.type === client_1.JobType.UPLOAD_FINALIZE) {
                await runStep('Validate Parts', async () => undefined);
                await runStep('Compose Object', async () => undefined);
                result = await runStep('Verify Format and Checksum', () => this.uploads.finalize(this.requireString(payload.uploadSessionId)));
                await runStep('Create RobotFile', async () => undefined);
                await runStep('Cleanup Parts', async () => undefined);
            }
            else if (job.type === client_1.JobType.RETENTION_CLEANUP) {
                result = await this.retention.execute();
            }
            else if (job.type === client_1.JobType.ARCHIVE_VIDEO_ASSEMBLE) {
                result = await runStep('Assemble Continuous Recording', () => this.recordings.assemble(job, workerId));
            }
            if (leaseRequired)
                await renewLease();
            if (await this.jobs.isCancelRequested(job.id))
                await this.jobs.markCancelled(job.id, owner);
            else
                await this.jobs.finish(job.id, this.jsonResult(result), owner);
        }
        catch (error) {
            if (error instanceof jobs_service_1.JobOwnershipLostError)
                return;
            try {
                if (error instanceof archive_recordings_service_1.ArchiveAssemblyBusyError)
                    await this.jobs.defer(job.id, workerId);
                else if (error instanceof JobCancelledError || error instanceof archive_recordings_service_1.ArchiveAssemblyCancelledError || error instanceof jobs_service_1.JobCancellationRequestedError)
                    await this.jobs.markCancelled(job.id, owner);
                else
                    await this.jobs.fail(job.id, error, activeStep, this.isInfrastructure(error), owner);
            }
            catch (ownershipError) {
                if (!(ownershipError instanceof jobs_service_1.JobOwnershipLostError))
                    throw ownershipError;
            }
        }
        finally {
            if (leaseTimer)
                clearInterval(leaseTimer);
            clearInterval(jobHeartbeat);
            if (leaseAcquired)
                await this.jobs.releaseLease('git-dvc-publication', leaseOwner).catch(() => undefined);
        }
    }
    requireString(value) { if (typeof value !== 'string')
        throw new common_1.ConflictException('Job payload is invalid'); return value; }
    requireStrings(value) { if (!Array.isArray(value) || value.some((item) => typeof item !== 'string'))
        throw new common_1.ConflictException('Job payload is invalid'); return value; }
    isInfrastructure(error) {
        if (error instanceof common_1.ConflictException)
            return false;
        if (error instanceof client_1.Prisma.PrismaClientKnownRequestError && error.code === 'P2028') {
            const timeout = 'Unable to start a transaction in the given time.';
            return error.meta?.error === timeout || error.message.includes(`Transaction API error: ${timeout}`);
        }
        const connectionCode = error instanceof client_1.Prisma.PrismaClientKnownRequestError ? error.code
            : error instanceof client_1.Prisma.PrismaClientInitializationError ? error.errorCode : undefined;
        if (error instanceof client_1.Prisma.PrismaClientKnownRequestError || error instanceof client_1.Prisma.PrismaClientInitializationError) {
            return connectionCode !== undefined && ['P1001', 'P1002', 'P1017', 'P2024'].includes(connectionCode);
        }
        return error instanceof common_1.ServiceUnavailableException || (error instanceof Error && /ECONN|timed out|unreachable|socket|storage/i.test(error.message));
    }
    jsonResult(value) { return JSON.parse(JSON.stringify(value, (_key, item) => typeof item === 'bigint' ? item.toString() : item)); }
};
exports.JobExecutorService = JobExecutorService;
exports.JobExecutorService = JobExecutorService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [jobs_service_1.JobsService, dataset_publish_service_1.DatasetPublishService, storage_operations_service_1.StorageOperationsService, uploads_service_1.UploadsService, retention_cleanup_service_1.RetentionCleanupService, archive_recordings_service_1.ArchiveRecordingsService])
], JobExecutorService);
class JobCancelledError extends Error {
}
