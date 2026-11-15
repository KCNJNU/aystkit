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
exports.TagsController = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const tags_service_1 = require("./tags.service");
class CreateTagDto {
    name;
    color;
}
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateTagDto.prototype, "name", void 0);
__decorate([
    (0, class_validator_1.Matches)(/^#[0-9a-f]{6}$/i),
    __metadata("design:type", String)
], CreateTagDto.prototype, "color", void 0);
class UpdateTagDto {
    name;
    color;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], UpdateTagDto.prototype, "name", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.Matches)(/^#[0-9a-f]{6}$/i),
    __metadata("design:type", String)
], UpdateTagDto.prototype, "color", void 0);
class BulkTagDto {
    fileIds;
    tagIds;
}
__decorate([
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.IsUUID)('4', { each: true }),
    __metadata("design:type", Array)
], BulkTagDto.prototype, "fileIds", void 0);
__decorate([
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.IsUUID)('4', { each: true }),
    __metadata("design:type", Array)
], BulkTagDto.prototype, "tagIds", void 0);
let TagsController = class TagsController {
    tags;
    constructor(tags) {
        this.tags = tags;
    }
    list(user) { return this.tags.list(user); }
    create(body, user) { return this.tags.create(body.name, body.color, user); }
    update(id, body, user) { return this.tags.update(id, body, user); }
    remove(id, user) { return this.tags.remove(id, user); }
    bulk(body, user) { return this.tags.bulkAttachFiles(body.fileIds, body.tagIds, user); }
    attachFile(fileId, tagId, user) { return this.tags.attachFile(fileId, tagId, user); }
    detachFile(fileId, tagId, user) { return this.tags.detachFile(fileId, tagId, user); }
    attachDataset(datasetId, tagId, user) { return this.tags.attachDataset(datasetId, tagId, user); }
    detachDataset(datasetId, tagId, user) { return this.tags.detachDataset(datasetId, tagId, user); }
};
exports.TagsController = TagsController;
__decorate([
    (0, common_1.Get)(),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "list", null);
__decorate([
    (0, common_1.Post)(),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [CreateTagDto, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "create", null);
__decorate([
    (0, common_1.Patch)(':id'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, UpdateTagDto, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "update", null);
__decorate([
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(204),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "remove", null);
__decorate([
    (0, common_1.Post)('files/bulk'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [BulkTagDto, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "bulk", null);
__decorate([
    (0, common_1.Post)('files/:fileId/:tagId'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('fileId')),
    __param(1, (0, common_1.Param)('tagId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "attachFile", null);
__decorate([
    (0, common_1.Delete)('files/:fileId/:tagId'),
    (0, common_1.HttpCode)(204),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('fileId')),
    __param(1, (0, common_1.Param)('tagId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "detachFile", null);
__decorate([
    (0, common_1.Post)('datasets/:datasetId/:tagId'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('datasetId')),
    __param(1, (0, common_1.Param)('tagId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "attachDataset", null);
__decorate([
    (0, common_1.Delete)('datasets/:datasetId/:tagId'),
    (0, common_1.HttpCode)(204),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    __param(0, (0, common_1.Param)('datasetId')),
    __param(1, (0, common_1.Param)('tagId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], TagsController.prototype, "detachDataset", null);
exports.TagsController = TagsController = __decorate([
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/tags'),
    __metadata("design:paramtypes", [tags_service_1.TagsService])
], TagsController);
//# sourceMappingURL=tags.controller.js.map