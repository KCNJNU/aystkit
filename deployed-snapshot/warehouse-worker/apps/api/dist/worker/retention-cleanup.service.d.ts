export declare class RetentionCleanupService {
    execute(): Promise<{
        skipped: boolean;
        reason: string;
        completedAt: string;
    }>;
}
