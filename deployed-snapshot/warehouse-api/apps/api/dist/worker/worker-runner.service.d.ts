import { ConfigService } from '@nestjs/config';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { SettingsService } from '../settings/settings.service';
import { JobExecutorService } from './job-executor.service';
import { ArchiveRecordingsService } from '../datasets/archive-recordings.service';
export declare class WorkerRunnerService {
    private readonly jobs;
    private readonly executor;
    private readonly prisma;
    private readonly settings;
    private readonly config;
    private readonly recordings;
    private readonly logger;
    private readonly workerId;
    private stopping;
    constructor(jobs: JobsService, executor: JobExecutorService, prisma: PrismaService, settings: SettingsService, config: ConfigService, recordings: ArchiveRecordingsService);
    start(): Promise<void>;
}
