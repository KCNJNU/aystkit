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
exports.StorageController = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const class_transformer_1 = require("class-transformer");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const storage_operations_service_1 = require("./storage-operations.service");
class ObjectsQuery {
    prefix;
    cursor;
    limit = 50;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], ObjectsQuery.prototype, "prefix", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], ObjectsQuery.prototype, "cursor", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(200),
    __metadata("design:type", Object)
], ObjectsQuery.prototype, "limit", void 0);
class PageQuery {
    page = 1;
    pageSize = 20;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    __metadata("design:type", Object)
], PageQuery.prototype, "page", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(100),
    __metadata("design:type", Object)
], PageQuery.prototype, "pageSize", void 0);
class IgnoreIssueDto {
    note;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], IgnoreIssueDto.prototype, "note", void 0);
class DeleteOrphansDto {
    issueIds;
    confirmation;
}
__decorate([
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.ArrayNotEmpty)(),
    (0, class_validator_1.IsUUID)('4', { each: true }),
    __metadata("design:type", Array)
], DeleteOrphansDto.prototype, "issueIds", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], DeleteOrphansDto.prototype, "confirmation", void 0);
let StorageController = class StorageController {
    operations;
    constructor(operations) {
        this.operations = operations;
    }
    objects(query) { return this.operations.pageObjects(query.prefix, query.cursor, query.limit); }
    scan(user) { return this.operations.enqueueScan(user.id); }
    runs(query) { return this.operations.pageRuns(query.page, query.pageSize); }
    run(id) { return this.operations.getRun(id); }
    ignore(id, body) { return this.operations.ignoreIssue(id, body.note); }
    deleteOrphans(user, body) { return this.operations.enqueueOrphanDelete(user.id, user.role, body.issueIds, body.confirmation); }
};
exports.StorageController = StorageController;
__decorate([
    (0, common_1.Get)('objects'),
    __param(0, (0, common_1.Query)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [ObjectsQuery]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "objects", null);
__decorate([
    (0, common_1.Post)('reconciliation-runs'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "scan", null);
__decorate([
    (0, common_1.Get)('reconciliation-runs/page'),
    __param(0, (0, common_1.Query)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [PageQuery]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "runs", null);
__decorate([
    (0, common_1.Get)('reconciliation-runs/:id'),
    __param(0, (0, common_1.Param)('id')),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "run", null);
__decorate([
    (0, common_1.Patch)('reconciliation-issues/:id/ignore'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, IgnoreIssueDto]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "ignore", null);
__decorate([
    (0, common_1.Post)('orphan-deletion-jobs'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, DeleteOrphansDto]),
    __metadata("design:returntype", void 0)
], StorageController.prototype, "deleteOrphans", null);
exports.StorageController = StorageController = __decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN),
    (0, common_1.Controller)('v1/storage'),
    __metadata("design:paramtypes", [storage_operations_service_1.StorageOperationsService])
], StorageController);
//# sourceMappingURL=storage.controller.js.map