import { DatasetPublishService } from '../datasets/dataset-publish.service';
import { JobsService } from '../jobs/jobs.service';
import { StorageOperationsService } from '../storage/storage-operations.service';
import { UploadsService } from '../uploads/uploads.service';
import { RetentionCleanupService } from './retention-cleanup.service';
import { ArchiveRecordingsService } from '../datasets/archive-recordings.service';
type ClaimedJob = Awaited<ReturnType<JobsService['get']>>;
export declare class JobExecutorService {
    private readonly jobs;
    private readonly publisher;
    private readonly storage;
    private readonly uploads;
    private readonly retention;
    private readonly recordings;
    constructor(jobs: JobsService, publisher: DatasetPublishService, storage: StorageOperationsService, uploads: UploadsService, retention: RetentionCleanupService, recordings: ArchiveRecordingsService);
    execute(job: ClaimedJob, workerId: string): Promise<void>;
    private requireString;
    private requireStrings;
    private isInfrastructure;
    private jsonResult;
}
export {};
