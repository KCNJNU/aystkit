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
exports.UploadsController = exports.CreateUploadDto = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const uploads_service_1 = require("./uploads.service");
class UploadDefaultsDto {
    datasetId;
}
__decorate([
    (0, swagger_1.ApiProperty)({ description: "目标数据集 ID" }),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], UploadDefaultsDto.prototype, "datasetId", void 0);
class CreateUploadDto {
    folderPath;
    datasetId;
    robotId;
    originalName;
    mimeType;
    fileSize;
    checksum;
    recognitionType;
    schemaVersion;
    capturedAt;
    metadata;
    lastModified;
}
exports.CreateUploadDto = CreateUploadDto;
__decorate([
    (0, swagger_1.ApiProperty)({ required: false, description: "数据集内目标文件夹路径，空字符串表示根目录" }),
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "folderPath", void 0);
__decorate([
    (0, swagger_1.ApiProperty)({ description: "目标数据集 ID" }),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "datasetId", void 0);
__decorate([
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "robotId", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "originalName", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "mimeType", void 0);
__decorate([
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(Number.MAX_SAFE_INTEGER),
    __metadata("design:type", Number)
], CreateUploadDto.prototype, "fileSize", void 0);
__decorate([
    (0, class_validator_1.Matches)(/^[a-f0-9]{64}$/i),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "checksum", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "recognitionType", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "schemaVersion", void 0);
__decorate([
    (0, class_validator_1.IsDateString)(),
    __metadata("design:type", String)
], CreateUploadDto.prototype, "capturedAt", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsObject)(),
    __metadata("design:type", Object)
], CreateUploadDto.prototype, "metadata", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsInt)(),
    __metadata("design:type", Number)
], CreateUploadDto.prototype, "lastModified", void 0);
let UploadsController = class UploadsController {
    uploads;
    constructor(uploads) {
        this.uploads = uploads;
    }
    defaults(user, body) { return this.uploads.defaults(user, body.datasetId); }
    create(user, body) { return this.uploads.create(user, body); }
    get(id, user) { return this.uploads.get(id, user.id, user.role); }
    putPart(id, part, range, checksum, content, user) { if (!Buffer.isBuffer(content))
        throw new Error('Part body must use application/octet-stream'); return this.uploads.putPart(id, part, range, checksum, content, user.id, user.role); }
    complete(id, user) { return this.uploads.complete(id, user.id, user.role); }
    abort(id, user) { return this.uploads.abort(id, user.id, user.role); }
};
exports.UploadsController = UploadsController;
__decorate([
    (0, common_1.Post)('defaults'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: "获取目标数据集的上传配置" }),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, UploadDefaultsDto]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "defaults", null);
__decorate([
    (0, common_1.Post)(),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, CreateUploadDto]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "create", null);
__decorate([
    (0, common_1.Get)(':id'),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "get", null);
__decorate([
    (0, common_1.Put)(':id/parts/:partNumber'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Param)('partNumber', common_1.ParseIntPipe)),
    __param(2, (0, common_1.Headers)('content-range')),
    __param(3, (0, common_1.Headers)('x-part-sha256')),
    __param(4, (0, common_1.Body)()),
    __param(5, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Number, Object, Object, Buffer, Object]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "putPart", null);
__decorate([
    (0, common_1.Post)(':id/complete'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "complete", null);
__decorate([
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(204),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], UploadsController.prototype, "abort", null);
exports.UploadsController = UploadsController = __decorate([
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/uploads'),
    __metadata("design:paramtypes", [uploads_service_1.UploadsService])
], UploadsController);
//# sourceMappingURL=uploads.controller.js.map