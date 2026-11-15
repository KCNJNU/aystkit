import { ConfigService } from '@nestjs/config';
export declare function parseDvcHash(pointer: string): string | undefined;
export declare class DvcService {
    private readonly config;
    private readonly repositoryRoot;
    constructor(config: ConfigService);
    resolveWorkspace(workspacePath: string): string;
    checkRemoteHealth(): Promise<{
        configured: false;
    } | {
        configured: true;
        provider: string;
        bucket: string;
        reachable: boolean;
    }>;
    publish(workspacePath: string, datasetName: string, version: number, runStep?: <T>(name: string, action: () => Promise<T>) => Promise<T>, datasetId?: string, signal?: AbortSignal): Promise<{
        dvcPath: string;
        dvcHash: string;
        gitCommit: string;
        gitTag: string;
    }>;
    recoveryVerify(gitTag: string, dvcPath: string, verify: (workspaceRoot: string) => Promise<void>, runStep: <T>(name: string, action: () => Promise<T>) => Promise<T>, signal?: AbortSignal): Promise<{
        verified: boolean;
        gitTag: string;
        dvcPath: string;
    }>;
    private ensureDvcRepository;
    private readDvcHash;
    private remoteConfig;
    private datasetTag;
    private runDvc;
    private runGit;
    private dvcEnvironment;
    private run;
}
