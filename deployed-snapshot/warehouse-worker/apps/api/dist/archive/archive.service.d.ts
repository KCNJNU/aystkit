import { ConfigService } from '@nestjs/config';
export declare class ArchiveService {
    private readonly config;
    private readonly cameraMonitor;
    constructor(config: ConfigService);
    private directory;
    private json;
    status(): Promise<{
        configured: boolean;
        enabled: boolean;
        running: boolean;
        cameras: import("./archive-camera-status").GarageCameraStatus[];
        updatedAt: unknown;
        state: string;
        datasetPattern: string;
        recorder: {
            [x: string]: unknown;
        } | null;
    }>;
    setGarage(enabled: boolean, actorId: string): Promise<{
        configured: boolean;
        enabled: boolean;
        running: boolean;
        cameras: import("./archive-camera-status").GarageCameraStatus[];
        updatedAt: unknown;
        state: string;
        datasetPattern: string;
        recorder: {
            [x: string]: unknown;
        } | null;
    }>;
}
