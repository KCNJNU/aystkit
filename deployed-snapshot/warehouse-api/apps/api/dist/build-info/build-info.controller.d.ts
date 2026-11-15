import { BuildInfoService } from './build-info.service';
export declare class BuildInfoController {
    private readonly build;
    constructor(build: BuildInfoService);
    get(): import("./build-info.service").BuildInfo;
}
