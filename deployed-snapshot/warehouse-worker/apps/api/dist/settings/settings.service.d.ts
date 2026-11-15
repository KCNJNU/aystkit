import { Prisma } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
export declare const SETTING_DEFAULTS: {
    readonly 'upload.jsonMaxBytes': 0;
    readonly 'upload.imageMaxBytes': 0;
    readonly 'upload.pcdMaxBytes': 0;
    readonly 'upload.mapMaxBytes': 0;
    readonly 'upload.yamlMaxBytes': 0;
    readonly 'upload.logMaxBytes': 0;
    readonly 'download.presignedTtlSeconds': 900;
    readonly 'jobs.workerConcurrency': 2;
    readonly 'notifications.jobSuccessEnabled': true;
};
export type SettingKey = keyof typeof SETTING_DEFAULTS;
export declare class SettingsService {
    private readonly prisma;
    constructor(prisma: PrismaService);
    all(): Promise<{
        [k: string]: string | number | boolean | Prisma.JsonObject | Prisma.JsonArray;
    }>;
    get<K extends SettingKey>(key: K): Promise<(typeof SETTING_DEFAULTS)[K]>;
    update(values: Record<string, unknown>, userId: string): Promise<{
        [k: string]: string | number | boolean | Prisma.JsonObject | Prisma.JsonArray;
    }>;
    private validate;
}
