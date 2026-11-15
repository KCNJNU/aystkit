import { ConflictException, Injectable, ServiceUnavailableException } from '@nestjs/common';
import { JobType, Prisma } from '@prisma/client';
import { randomUUID } from 'node:crypto';
import { DatasetPublishService } from '../datasets/dataset-publish.service';
import { JobCancellationRequestedError, JobOwnershipLostError, JobsService } from '../jobs/jobs.service';
import { StorageOperationsService } from '../storage/storage-operations.service';
import { UploadsService } from '../uploads/uploads.service';
import { RetentionCleanupService } from './retention-cleanup.service';
import { ArchiveAssemblyBusyError, ArchiveAssemblyCancelledError, ArchiveRecordingsService } from '../datasets/archive-recordings.service';

type ClaimedJob = Awaited<ReturnType<JobsService['get']>>;

@Injectable()
export class JobExecutorService {
  constructor(private readonly jobs: JobsService, private readonly publisher: DatasetPublishService, private readonly storage: StorageOperationsService, private readonly uploads: UploadsService, private readonly retention: RetentionCleanupService, private readonly recordings: ArchiveRecordingsService) {}

  async execute(job: ClaimedJob, workerId: string) {
    let activeStep: string | undefined;
    const leaseRequired = job.type === JobType.DATASET_PUBLISH || job.type === JobType.RECOVERY_VERIFY;
    const owner = job.type === JobType.ARCHIVE_VIDEO_ASSEMBLE || leaseRequired ? workerId : undefined;
    // One worker can run multiple jobs and can reclaim an old attempt. Each
    // execution must own a distinct lease, including two attempts of one job.
    const leaseOwner = `${workerId}:${job.id}:${randomUUID()}`;
    const cancellation = new AbortController();
    let leaseAcquired = false;
    let leaseTimer: NodeJS.Timeout | undefined;
    let renewing: Promise<void> | undefined;
    const loseLease = () => cancellation.abort(new ServiceUnavailableException('Git/DVC publication lease was lost'));
    const renewLease = async () => {
      cancellation.signal.throwIfAborted();
      if (!renewing) {
        renewing = this.jobs.heartbeatLease('git-dvc-publication', leaseOwner)
          .then(result => { if (result.count !== 1) loseLease(); })
          .catch(() => { loseLease(); })
          .finally(() => { renewing = undefined; });
      }
      await renewing;
      cancellation.signal.throwIfAborted();
    };
    const runStep = async <T>(name: string, action: () => Promise<T>) => {
      if (leaseRequired) await renewLease();
      activeStep = name;
      if (await this.jobs.isCancelRequested(job.id)) throw new JobCancelledError();
      const shouldRun = await this.jobs.beginStep(job.id, name, owner);
      const result = await action();
      if (leaseRequired) await renewLease();
      if (shouldRun) await this.jobs.completeStep(job.id, name, undefined, owner);
      activeStep = undefined;
      return result;
    };
    const jobHeartbeat = setInterval(() => void this.jobs.heartbeat(job.id, workerId)
      .then(result => { if (leaseRequired && result.count !== 1) loseLease(); })
      .catch(() => { if (leaseRequired) loseLease(); }), 10_000);
    try {
      if (leaseRequired) {
        leaseAcquired = await this.jobs.acquireLease('git-dvc-publication', leaseOwner);
        if (!leaseAcquired) { await this.jobs.defer(job.id, workerId); return; }
        await renewLease();
        leaseTimer = setInterval(() => void renewLease().catch(() => undefined), 10_000);
      }
      let result: unknown;
      const payload = job.payload as Record<string, unknown>;
      if (job.type === JobType.DATASET_PUBLISH) result = await this.publisher.publish(this.requireString(payload.versionId), runStep, cancellation.signal);
      else if (job.type === JobType.RECOVERY_VERIFY) result = await this.publisher.recoveryVerify(this.requireString(payload.versionId), runStep, cancellation.signal);
      else if (job.type === JobType.STORAGE_SCAN) {
        await runStep('Load Database Index', async () => undefined); await runStep('List ObjectStorage', async () => undefined);
        result = await runStep('Compare Objects', () => this.storage.executeScan(this.requireString(payload.runId)));
        await runStep('Persist Issues', async () => undefined);
      } else if (job.type === JobType.STORAGE_ORPHAN_DELETE) {
        const issueIds = this.requireStrings(payload.issueIds); await runStep('Validate Issues', async () => undefined);
        result = await runStep('Delete Objects', () => this.storage.executeOrphanDelete(issueIds)); await runStep('Resolve Issues', async () => undefined);
      } else if (job.type === JobType.DATASET_DELETE) {
        result = await runStep('Delete Dataset Objects', () => this.storage.executeDatasetDelete(this.requireString(payload.datasetId), this.requireStrings(payload.objectKeys)));
      } else if (job.type === JobType.UPLOAD_FINALIZE) {
        await runStep('Validate Parts', async () => undefined); await runStep('Compose Object', async () => undefined);
        result = await runStep('Verify Format and Checksum', () => this.uploads.finalize(this.requireString(payload.uploadSessionId)));
        await runStep('Create RobotFile', async () => undefined); await runStep('Cleanup Parts', async () => undefined);
      } else if (job.type === JobType.RETENTION_CLEANUP) {
        result = await this.retention.execute();
      } else if (job.type === JobType.ARCHIVE_VIDEO_ASSEMBLE) {
        result = await runStep('Assemble Continuous Recording', () => this.recordings.assemble(job, workerId));
      }
      if (leaseRequired) await renewLease();
      if (await this.jobs.isCancelRequested(job.id)) await this.jobs.markCancelled(job.id, owner); else await this.jobs.finish(job.id, this.jsonResult(result), owner);
    } catch (error) {
      if (error instanceof JobOwnershipLostError) return;
      try {
        if (error instanceof ArchiveAssemblyBusyError) await this.jobs.defer(job.id, workerId);
        else if (error instanceof JobCancelledError || error instanceof ArchiveAssemblyCancelledError || error instanceof JobCancellationRequestedError) await this.jobs.markCancelled(job.id, owner);
        else await this.jobs.fail(job.id, error, activeStep, this.isInfrastructure(error), owner);
      } catch (ownershipError) { if (!(ownershipError instanceof JobOwnershipLostError)) throw ownershipError; }
    } finally {
      if (leaseTimer) clearInterval(leaseTimer);
      clearInterval(jobHeartbeat);
      if (leaseAcquired) await this.jobs.releaseLease('git-dvc-publication', leaseOwner).catch(() => undefined);
    }
  }

  private requireString(value: unknown) { if (typeof value !== 'string') throw new ConflictException('Job payload is invalid'); return value; }
  private requireStrings(value: unknown) { if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) throw new ConflictException('Job payload is invalid'); return value as string[]; }
  private isInfrastructure(error: unknown) {
    if (error instanceof ConflictException) return false;
    if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2028') {
      // Acquisition timed out before a transaction was created; other transaction errors stay terminal.
      const timeout = 'Unable to start a transaction in the given time.';
      return error.meta?.error === timeout || error.message.includes(`Transaction API error: ${timeout}`);
    }
    // Typed Prisma errors must use connection codes, not business-error text.
    const connectionCode = error instanceof Prisma.PrismaClientKnownRequestError ? error.code
      : error instanceof Prisma.PrismaClientInitializationError ? error.errorCode : undefined;
    if (error instanceof Prisma.PrismaClientKnownRequestError || error instanceof Prisma.PrismaClientInitializationError) {
      return connectionCode !== undefined && ['P1001', 'P1002', 'P1017', 'P2024'].includes(connectionCode);
    }
    return error instanceof ServiceUnavailableException || (error instanceof Error && /ECONN|timed out|unreachable|socket|storage/i.test(error.message));
  }
  private jsonResult(value: unknown) { return JSON.parse(JSON.stringify(value, (_key, item: unknown) => typeof item === 'bigint' ? item.toString() : item)) as object; }
}

class JobCancelledError extends Error {}
