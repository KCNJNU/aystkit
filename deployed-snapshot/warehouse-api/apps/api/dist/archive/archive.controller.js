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
exports.ArchiveController = void 0;
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const class_validator_1 = require("class-validator");
const client_1 = require("@prisma/client");
const auth_decorators_1 = require("../auth/auth.decorators");
const archive_service_1 = require("./archive.service");
class RobotQueryDto {
    robotId;
}
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], RobotQueryDto.prototype, "robotId", void 0);
class GarageDto extends RobotQueryDto {
    enabled;
}
__decorate([
    (0, class_validator_1.IsBoolean)(),
    __metadata("design:type", Boolean)
], GarageDto.prototype, "enabled", void 0);
let ArchiveController = class ArchiveController {
    archive;
    constructor(archive) {
        this.archive = archive;
    }
    status(query) { return this.archive.status(query.robotId); }
    garage(body, user) { return this.archive.setGarage(body.enabled, user.id, body.robotId); }
};
exports.ArchiveController = ArchiveController;
__decorate([
    (0, common_1.Get)('status'),
    (0, swagger_1.ApiOperation)({ summary: '地库原生采集状态、缓存和缺流信息' }),
    __param(0, (0, common_1.Query)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [RobotQueryDto]),
    __metadata("design:returntype", void 0)
], ArchiveController.prototype, "status", null);
__decorate([
    (0, common_1.Post)('garage'),
    (0, swagger_1.ApiOperation)({ summary: '进入地库时开启持久采集标记，离开时关闭；不启动任何传感器或运动控制' }),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [GarageDto, Object]),
    __metadata("design:returntype", void 0)
], ArchiveController.prototype, "garage", null);
exports.ArchiveController = ArchiveController = __decorate([
    (0, swagger_1.ApiTags)('garage-archive'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN),
    (0, common_1.Controller)('v1/archive'),
    __metadata("design:paramtypes", [archive_service_1.ArchiveService])
], ArchiveController);
//# sourceMappingURL=archive.controller.js.map