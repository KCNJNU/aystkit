"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
var __param = (this && this.__param) || function (paramIndex, decorator) {
    return function (target, key) { decorator(target, key, paramIndex); }
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DatasetsController = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const dataset_folder_dto_1 = require("./dto/dataset-folder.dto");
const delete_dataset_dto_1 = require("./dto/delete-dataset.dto");
const create_dataset_dto_1 = require("./dto/create-dataset.dto");
const create_dataset_version_dto_1 = require("./dto/create-dataset-version.dto");
const compare_versions_query_dto_1 = require("./dto/compare-versions-query.dto");
const list_datasets_query_dto_1 = require("./dto/list-datasets-query.dto");
const list_version_files_query_dto_1 = require("./dto/list-version-files-query.dto");
const list_versions_query_dto_1 = require("./dto/list-versions-query.dto");
const replace_version_files_dto_1 = require("./dto/replace-version-files.dto");
const update_version_files_dto_1 = require("./dto/update-version-files.dto");
const datasets_service_1 = require("./datasets.service");
const dataset_publish_service_1 = require("./dataset-publish.service");
const client_1 = require("@prisma/client");
const auth_decorators_1 = require("../auth/auth.decorators");
const jobs_service_1 = require("../jobs/jobs.service");
let DatasetsController = class DatasetsController {
    datasets;
    publisher;
    jobs;
    constructor(datasets, publisher, jobs) {
        this.datasets = datasets;
        this.publisher = publisher;
        this.jobs = jobs;
    }
    create(body, user) { return this.datasets.create(body, user); }
    delete(id, body, user) { return this.datasets.delete(id, body.confirmationName, user); }
    list(user) { return this.datasets.list(user); }
    page(query, user) { return this.datasets.page(query, user); }
    pageVersions(query, user) { return this.datasets.pageVersions(query, user); }
    compare(datasetId, query, user) { return this.datasets.compareVersions(datasetId, query.baseVersionId, query.targetVersionId, user); }
    contents(id, query, user) { return this.datasets.contents(id, query, user); }
    folders(id, user) { return this.datasets.folders(id, user); }
    createFolder(id, body, user) { return this.datasets.createFolder(id, body.parentPath, body.name, user); }
    get(id, user) { return this.datasets.get(id, user); }
    createVersion(datasetId, body, user) { return this.datasets.createVersion(datasetId, body.fileIds ?? [], body.sourceVersionId, user); }
    getVersion(id, user) { return this.datasets.getVersion(id, user); }
    files(id, query, user) { return this.datasets.listVersionFiles(id, query, false, user); }
    candidates(id, query, user) { return this.datasets.listVersionFiles(id, query, true, user); }
    addFiles(id, body, user) { return this.datasets.addFiles(id, body.fileIds, user); }
    replaceFiles(id, body, user) { return this.datasets.replaceFiles(id, body.fileIds, user); }
    removeFile(versionId, fileId, user) { return this.datasets.removeFile(versionId, fileId, user); }
    freeze(id, user) { return this.datasets.freeze(id, user); }
    validate(id, user) { return this.datasets.validateVersion(id, user); }
    async publish(id, user) { await this.datasets.getVersion(id, user); return this.publisher.publish(id); }
    async publishJob(id, user) { const version = await this.datasets.getVersion(id, user); return this.jobs.create(client_1.JobType.DATASET_PUBLISH, { payload: { versionId: id }, resourceType: 'DatasetVersion', resourceId: id, requestedById: user.id, tenantId: version.dataset.tenantId, projectId: version.dataset.projectId }); }
    async recoveryJob(id, user) { const version = await this.datasets.getVersion(id, user); return this.jobs.create(client_1.JobType.RECOVERY_VERIFY, { payload: { versionId: id }, resourceType: 'DatasetVersion', resourceId: id, requestedById: user.id, tenantId: version.dataset.tenantId, projectId: version.dataset.projectId }); }
};
exports.DatasetsController = DatasetsController;
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)(),
    (0, swagger_1.ApiOperation)({ summary: 'Create a dataset' }),
    (0, swagger_1.ApiCreatedResponse)(),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [create_dataset_dto_1.CreateDatasetDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "create", null);
__decorate([
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: 'Delete a dataset and its exclusively owned files; enqueue durable storage cleanup' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, delete_dataset_dto_1.DeleteDatasetDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "delete", null);
__decorate([
    (0, common_1.Get)(),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "list", null);
__decorate([
    (0, common_1.Get)('page'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [list_datasets_query_dto_1.ListDatasetsQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "page", null);
__decorate([
    (0, common_1.Get)('versions/page'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [list_versions_query_dto_1.ListVersionsQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "pageVersions", null);
__decorate([
    (0, common_1.Get)(':datasetId/versions/compare'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('datasetId')),
    __param(1, (0, common_1.Query)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, compare_versions_query_dto_1.CompareVersionsQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "compare", null);
__decorate([
    (0, common_1.Get)(':id/contents'),
    (0, swagger_1.ApiOperation)({ summary: 'Browse paginated dataset files; show garage archive segment contents directly within time slots' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, dataset_folder_dto_1.DatasetContentsQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "contents", null);
__decorate([
    (0, common_1.Get)(':id/folders'),
    (0, swagger_1.ApiOperation)({ summary: 'List accessible dataset folder paths for upload selection' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "folders", null);
__decorate([
    (0, common_1.Post)(':id/folders'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: 'Create a folder and its object storage marker' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, dataset_folder_dto_1.CreateDatasetFolderDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "createFolder", null);
__decorate([
    (0, common_1.Get)(':id'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "get", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)(':datasetId/versions'),
    (0, swagger_1.ApiOperation)({ summary: 'Create the next DRAFT dataset version' }),
    (0, swagger_1.ApiCreatedResponse)(),
    __param(0, (0, common_1.Param)('datasetId')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, create_dataset_version_dto_1.CreateDatasetVersionDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "createVersion", null);
__decorate([
    (0, common_1.Get)('versions/:id'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "getVersion", null);
__decorate([
    (0, common_1.Get)('versions/:id/files'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, list_version_files_query_dto_1.ListVersionFilesQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "files", null);
__decorate([
    (0, common_1.Get)('versions/:id/candidates'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, list_version_files_query_dto_1.ListVersionFilesQueryDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "candidates", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)('versions/:id/files'),
    (0, swagger_1.ApiOperation)({ summary: 'Add RobotFiles to a DRAFT dataset version' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, update_version_files_dto_1.UpdateVersionFilesDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "addFiles", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Put)('versions/:id/files'),
    (0, swagger_1.ApiOperation)({ summary: 'Atomically replace RobotFiles in a DRAFT dataset version' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, replace_version_files_dto_1.ReplaceVersionFilesDto, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "replaceFiles", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Delete)('versions/:versionId/files/:fileId'),
    (0, common_1.HttpCode)(204),
    (0, swagger_1.ApiOperation)({ summary: 'Remove a RobotFile from a DRAFT dataset version' }),
    __param(0, (0, common_1.Param)('versionId')),
    __param(1, (0, common_1.Param)('fileId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "removeFile", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN),
    (0, common_1.Post)('versions/:id/freeze'),
    (0, swagger_1.ApiOperation)({ summary: 'Freeze a dataset version and store its manifest' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "freeze", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)('versions/:id/validate'),
    (0, swagger_1.ApiOperation)({ summary: 'Validate DatasetVersion membership and stored object integrity' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], DatasetsController.prototype, "validate", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN),
    (0, common_1.Post)('versions/:id/publish'),
    (0, swagger_1.ApiOperation)({ summary: 'Publish a FROZEN version with DVC and a Git tag' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], DatasetsController.prototype, "publish", null);
__decorate([
    (0, common_1.Post)('versions/:id/publish-jobs'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], DatasetsController.prototype, "publishJob", null);
__decorate([
    (0, common_1.Post)('versions/:id/recovery-jobs'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", Promise)
], DatasetsController.prototype, "recoveryJob", null);
exports.DatasetsController = DatasetsController = __decorate([
    (0, swagger_1.ApiTags)('datasets'),
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/datasets'),
    __metadata("design:paramtypes", [datasets_service_1.DatasetsService, dataset_publish_service_1.DatasetPublishService, jobs_service_1.JobsService])
], DatasetsController);
//# sourceMappingURL=datasets.controller.js.map