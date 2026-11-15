import { ConfigService } from '@nestjs/config';
export type BuildInfo = {
    service: string;
    version: string;
    gitCommit: string;
    buildTime: string;
};
export declare class BuildInfoService {
    private readonly config;
    constructor(config: ConfigService);
    get(): BuildInfo;
}
