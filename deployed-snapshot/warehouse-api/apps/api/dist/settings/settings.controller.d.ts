import { ConfigService } from '@nestjs/config';
import { AuthUser } from '../auth/auth.types';
import { PrismaService } from '../prisma/prisma.service';
import { SettingsService } from './settings.service';
declare class UpdateSettingsDto {
    values: Record<string, unknown>;
}
export declare class SettingsController {
    private readonly settings;
    private readonly config;
    private readonly prisma;
    constructor(settings: SettingsService, config: ConfigService, prisma: PrismaService);
    configuration(): Promise<{
        settings: {
            [k: string]: string | number | boolean | import("@prisma/client/runtime/library").JsonObject | import("@prisma/client/runtime/library").JsonArray;
        };
        infrastructure: {
            postgresql: {
                configured: boolean;
            };
            objectStorage: {
                configured: boolean;
                provider: string;
                bucket: any;
            };
            dvcRemote: {
                configured: boolean;
                provider: string;
                bucket: any;
            };
        };
        worker: {
            workerId: string;
            version: string;
            concurrency: number;
            heartbeatAt: Date;
            status: string;
        } | {
            status: string;
            workerId?: undefined;
            version?: undefined;
            concurrency?: undefined;
            heartbeatAt?: undefined;
        };
    }>;
    update(body: UpdateSettingsDto, user: AuthUser): Promise<{
        [k: string]: string | number | boolean | import("@prisma/client/runtime/library").JsonObject | import("@prisma/client/runtime/library").JsonArray;
    }>;
}
export {};
