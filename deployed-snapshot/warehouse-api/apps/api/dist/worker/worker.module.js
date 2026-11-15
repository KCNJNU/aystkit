"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.WorkerModule = void 0;
const common_1 = require("@nestjs/common");
const datasets_module_1 = require("../datasets/datasets.module");
const storage_module_1 = require("../storage/storage.module");
const uploads_module_1 = require("../uploads/uploads.module");
const job_executor_service_1 = require("./job-executor.service");
const worker_runner_service_1 = require("./worker-runner.service");
const retention_cleanup_service_1 = require("./retention-cleanup.service");
let WorkerModule = class WorkerModule {
};
exports.WorkerModule = WorkerModule;
exports.WorkerModule = WorkerModule = __decorate([
    (0, common_1.Module)({ imports: [datasets_module_1.DatasetsModule, storage_module_1.StorageModule, uploads_module_1.UploadsModule], providers: [job_executor_service_1.JobExecutorService, worker_runner_service_1.WorkerRunnerService, retention_cleanup_service_1.RetentionCleanupService], exports: [worker_runner_service_1.WorkerRunnerService] })
], WorkerModule);
//# sourceMappingURL=worker.module.js.map