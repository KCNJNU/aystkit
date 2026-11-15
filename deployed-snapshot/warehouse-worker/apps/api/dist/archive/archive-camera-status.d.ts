export type CameraAcquisition = 'capturing' | 'not_capturing' | 'stale' | 'unknown';
export type CameraRecording = 'recording' | 'disabled' | 'waiting' | 'stale' | 'error' | 'unknown';
export type GarageCameraStatus = {
    id: string;
    name: string;
    acquisition: CameraAcquisition;
    recording: CameraRecording;
    receivedFrames: number | null;
    writtenFrames: number | null;
    droppedFrames: number | null;
    frameAgeS: number | null;
    updatedAt: string | null;
    detail?: string;
};
export declare class ArchiveCameraMonitor {
    private readonly samples;
    private writing;
    snapshot(sourceValue: unknown, gateValue: unknown, now?: number): GarageCameraStatus[];
}
