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
exports.FilesController = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const platform_express_1 = require("@nestjs/platform-express");
const swagger_1 = require("@nestjs/swagger");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const list_files_query_dto_1 = require("./dto/list-files-query.dto");
const files_service_1 = require("./files.service");
const detach_file_references_dto_1 = require("./dto/detach-file-references.dto");
const file_lifecycle_service_1 = require("./file-lifecycle.service");
const file_lifecycle_dto_1 = require("./dto/file-lifecycle.dto");
class CreateFileDto {
    robotId;
    recognitionType;
    capturedAt;
    schemaVersion;
    metadata;
}
__decorate([
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], CreateFileDto.prototype, "robotId", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateFileDto.prototype, "recognitionType", void 0);
__decorate([
    (0, class_validator_1.IsDateString)(),
    __metadata("design:type", String)
], CreateFileDto.prototype, "capturedAt", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateFileDto.prototype, "schemaVersion", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], CreateFileDto.prototype, "metadata", void 0);
class UpdateFileDto {
    recognitionType;
    metadata;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], UpdateFileDto.prototype, "recognitionType", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsObject)(),
    __metadata("design:type", Object)
], UpdateFileDto.prototype, "metadata", void 0);
let FilesController = class FilesController {
    files;
    lifecycle;
    constructor(files, lifecycle) {
        this.files = files;
        this.lifecycle = lifecycle;
    }
    recycleBin(query, user) { return this.lifecycle.recycleList(query, user); }
    restore(id, _body, user) { return this.lifecycle.restore(id, user); }
    purge(id, _body, user) { return this.lifecycle.purge(id, user); }
    impact(id, user) { return this.lifecycle.impact(id, user); }
    relocate(id, body, user) { return this.lifecycle.relocate(id, body, user); }
    create(body, file, user) { return this.files.create({ ...body, file }, user); }
    list(query, user) { return this.files.list(query, user); }
    get(id, user) { return this.files.get(id, user); }
    update(id, body, user) { return this.files.update(id, body, user); }
    download(id, user) { return this.files.getDownloadUrl(id, user); }
    references(id, user) { return this.files.references(id, user); }
    detach(id, body, user) { return this.files.detachReferences(id, body, user); }
    remove(id, user) { return this.lifecycle.recycle(id, user); }
};
exports.FilesController = FilesController;
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Get)('recycle-bin'),
    (0, swagger_1.ApiOperation)({ summary: '分页查看授权范围内的回收文件' }),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [file_lifecycle_dto_1.RecycleQueryDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "recycleBin", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)('recycle-bin/:id/restore'),
    (0, common_1.HttpCode)(200),
    (0, swagger_1.ApiOperation)({ summary: '确认恢复回收文件，不重新附加到版本' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, file_lifecycle_dto_1.ConfirmFileActionDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "restore", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN),
    (0, common_1.Post)('recycle-bin/:id/purge'),
    (0, common_1.HttpCode)(200),
    (0, swagger_1.ApiOperation)({ summary: '管理员确认彻底清理文件及存储对象，失败可重试' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, file_lifecycle_dto_1.ConfirmFileActionDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "purge", null);
__decorate([
    (0, common_1.Get)(':id/delete-impact'),
    (0, swagger_1.ApiOperation)({ summary: '预览回收和路径整理对版本与存储的影响' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "impact", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Patch)(':id/location'),
    (0, swagger_1.ApiOperation)({ summary: '同一数据集内移动或重命名文件并同步存储路径' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, file_lifecycle_dto_1.RelocateFileDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "relocate", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)(),
    (0, common_1.UseInterceptors)((0, platform_express_1.FileInterceptor)('file', { limits: { fileSize: 10 * 1024 * 1024 } })),
    (0, swagger_1.ApiOperation)({ summary: 'Upload a robot file of any type' }),
    (0, swagger_1.ApiConsumes)('multipart/form-data'),
    (0, swagger_1.ApiBody)({ schema: { type: 'object' } }),
    (0, swagger_1.ApiCreatedResponse)(),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, common_1.UploadedFile)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [CreateFileDto, Object, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "create", null);
__decorate([
    (0, common_1.Get)(),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [list_files_query_dto_1.ListFilesQueryDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "list", null);
__decorate([
    (0, common_1.Get)(':id'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "get", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Patch)(':id'),
    (0, swagger_1.ApiOperation)({ summary: '编辑授权文件的分类和元信息，不修改原始文件或冻结版本' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, UpdateFileDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "update", null);
__decorate([
    (0, common_1.Get)(':id/download'),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "download", null);
__decorate([
    (0, common_1.Get)(':id/references'),
    (0, swagger_1.ApiOperation)({ summary: '查看文件在授权数据集中的版本引用及解除限制' }),
    (0, swagger_1.ApiOkResponse)(),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "references", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Post)(':id/references/detach'),
    (0, common_1.HttpCode)(200),
    (0, swagger_1.ApiOperation)({ summary: '确认解除选定的草稿版本引用，保留原始文件与历史快照' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, detach_file_references_dto_1.DetachFileReferencesDto, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "detach", null);
__decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(204),
    (0, swagger_1.ApiOperation)({ summary: '将未被版本引用的文件移入回收站，保留存储对象' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], FilesController.prototype, "remove", null);
exports.FilesController = FilesController = __decorate([
    (0, swagger_1.ApiTags)('files'),
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/files'),
    __metadata("design:paramtypes", [files_service_1.FilesService, file_lifecycle_service_1.FileLifecycleService])
], FilesController);
//# sourceMappingURL=files.controller.js.map