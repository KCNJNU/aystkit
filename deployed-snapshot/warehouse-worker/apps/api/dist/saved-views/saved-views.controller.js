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
exports.SavedViewsController = void 0;
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const saved_views_service_1 = require("./saved-views.service");
class CreateViewDto {
    resource;
    name;
    query;
    isShared;
}
__decorate([
    (0, class_validator_1.IsEnum)(client_1.SavedViewResource),
    __metadata("design:type", String)
], CreateViewDto.prototype, "resource", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], CreateViewDto.prototype, "name", void 0);
__decorate([
    (0, class_validator_1.IsObject)(),
    __metadata("design:type", Object)
], CreateViewDto.prototype, "query", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsBoolean)(),
    __metadata("design:type", Boolean)
], CreateViewDto.prototype, "isShared", void 0);
class UpdateViewDto {
    name;
    query;
    isShared;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.IsNotEmpty)(),
    __metadata("design:type", String)
], UpdateViewDto.prototype, "name", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsObject)(),
    __metadata("design:type", Object)
], UpdateViewDto.prototype, "query", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsBoolean)(),
    __metadata("design:type", Boolean)
], UpdateViewDto.prototype, "isShared", void 0);
class CopyViewDto {
    name;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], CopyViewDto.prototype, "name", void 0);
let SavedViewsController = class SavedViewsController {
    views;
    constructor(views) {
        this.views = views;
    }
    list(user, resource) { return this.views.list(user.id, resource); }
    create(user, body) { return this.views.create(user.id, user.role, body); }
    update(id, user, body) { return this.views.update(id, user.id, user.role, body); }
    remove(id, user) { return this.views.remove(id, user.id); }
    copy(id, user, body) { return this.views.copy(id, user.id, body.name); }
};
exports.SavedViewsController = SavedViewsController;
__decorate([
    (0, common_1.Get)(),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Query)('resource')),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, String]),
    __metadata("design:returntype", void 0)
], SavedViewsController.prototype, "list", null);
__decorate([
    (0, common_1.Post)(),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __param(1, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object, CreateViewDto]),
    __metadata("design:returntype", void 0)
], SavedViewsController.prototype, "create", null);
__decorate([
    (0, common_1.Patch)(':id'),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __param(2, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object, UpdateViewDto]),
    __metadata("design:returntype", void 0)
], SavedViewsController.prototype, "update", null);
__decorate([
    (0, common_1.Delete)(':id'),
    (0, common_1.HttpCode)(204),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], SavedViewsController.prototype, "remove", null);
__decorate([
    (0, common_1.Post)(':id/copy'),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __param(2, (0, common_1.Body)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object, CopyViewDto]),
    __metadata("design:returntype", void 0)
], SavedViewsController.prototype, "copy", null);
exports.SavedViewsController = SavedViewsController = __decorate([
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/saved-views'),
    __metadata("design:paramtypes", [saved_views_service_1.SavedViewsService])
], SavedViewsController);
//# sourceMappingURL=saved-views.controller.js.map