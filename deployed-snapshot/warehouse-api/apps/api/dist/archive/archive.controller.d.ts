import { AuthUser } from '../auth/auth.types';
import { ArchiveService } from './archive.service';
declare class RobotQueryDto {
    robotId?: string;
}
declare class GarageDto extends RobotQueryDto {
    enabled: boolean;
}
export declare class ArchiveController {
    private readonly archive;
    constructor(archive: ArchiveService);
    status(query: RobotQueryDto): Promise<{
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
    garage(body: GarageDto, user: AuthUser): Promise<{
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
}
export {};
