import { ConfigService } from '@nestjs/config';
export declare class ArchiveService {
    private readonly config;
    private readonly robots;
    private readonly cameraMonitor;
    constructor(config: ConfigService);
    private forRobot;
    private directory;
    private json;
    private gateEnabled;
    status(robotId?: string): Promise<{
        configured: boolean;
        enabled: boolean;
        webEnabled: boolean;
        desktopEnabled: boolean;
        captureSource: string | null;
        running: boolean;
        cameras: import("./archive-camera-status").GarageCameraStatus[];
        updatedAt: unknown;
        state: string;
        datasetPattern: string;
        recorder: {
            [x: string]: unknown;
        } | null;
    }>;
    private localStatus;
    setGarage(enabled: boolean, actorId: string, robotId?: string): Promise<{
        configured: boolean;
        enabled: boolean;
        webEnabled: boolean;
        desktopEnabled: boolean;
        captureSource: string | null;
        running: boolean;
        cameras: import("./archive-camera-status").GarageCameraStatus[];
        updatedAt: unknown;
        state: string;
        datasetPattern: string;
        recorder: {
            [x: string]: unknown;
        } | null;
    }>;
    private localSetGarage;
}
