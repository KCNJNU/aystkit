"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DatasetsModule = void 0;
const common_1 = require("@nestjs/common");
const datasets_controller_1 = require("./datasets.controller");
const dataset_publish_service_1 = require("./dataset-publish.service");
const datasets_service_1 = require("./datasets.service");
const dvc_module_1 = require("../dvc/dvc.module");
const storage_module_1 = require("../storage/storage.module");
const archive_recordings_controller_1 = require("./archive-recordings.controller");
const archive_recordings_service_1 = require("./archive-recordings.service");
let DatasetsModule = class DatasetsModule {
};
exports.DatasetsModule = DatasetsModule;
exports.DatasetsModule = DatasetsModule = __decorate([
    (0, common_1.Module)({ imports: [dvc_module_1.DvcModule, storage_module_1.StorageModule], controllers: [datasets_controller_1.DatasetsController, archive_recordings_controller_1.ArchiveRecordingsController], providers: [datasets_service_1.DatasetsService, dataset_publish_service_1.DatasetPublishService, archive_recordings_service_1.ArchiveRecordingsService], exports: [datasets_service_1.DatasetsService, dataset_publish_service_1.DatasetPublishService, archive_recordings_service_1.ArchiveRecordingsService] })
], DatasetsModule);
//# sourceMappingURL=datasets.module.js.map