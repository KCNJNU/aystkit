import { AuthUser } from '../auth/auth.types';
import { ArchiveRecordingsService } from './archive-recordings.service';
export declare class ArchiveRecordingsController {
    private readonly recordings;
    constructor(recordings: ArchiveRecordingsService);
    list(id: string, path: string | undefined, user: AuthUser): Promise<{
        datasetId: string;
        path: string;
        recordings: {
            state: string;
            file: import("@prisma/client/runtime/library").InputJsonValue | null;
            jobId: string | null;
            error: string | null;
            id: string;
            datasetId: string;
            path: string;
            sensor: string;
            robotId: string;
            startedAt: string;
            endAt: string;
            partCount: number;
            sourceBytes: string;
            frameCount: number;
            durationSeconds: number;
            closed: boolean;
            gapBeforeSeconds: number | null;
        }[];
    }>;
    assemble(id: string, recordingId: string, user: AuthUser): Promise<{
        jobId: string | null;
    }>;
}
