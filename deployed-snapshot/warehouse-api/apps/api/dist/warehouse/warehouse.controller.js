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
exports.WarehouseController = void 0;
const class_validator_1 = require("class-validator");
const client_1 = require("@prisma/client");
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const auth_decorators_1 = require("../auth/auth.decorators");
const permissions_1 = require("../auth/permissions");
const warehouse_query_service_1 = require("./warehouse-query.service");
class RegisterRobotDto {
    id;
    projectId;
    name;
    externalId;
}
__decorate([
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], RegisterRobotDto.prototype, "id", void 0);
__decorate([
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], RegisterRobotDto.prototype, "projectId", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(1),
    (0, class_validator_1.MaxLength)(60),
    __metadata("design:type", String)
], RegisterRobotDto.prototype, "name", void 0);
__decorate([
    (0, class_validator_1.IsString)(),
    (0, class_validator_1.MinLength)(1),
    (0, class_validator_1.MaxLength)(64),
    __metadata("design:type", String)
], RegisterRobotDto.prototype, "externalId", void 0);
let WarehouseController = class WarehouseController {
    warehouse;
    constructor(warehouse) {
        this.warehouse = warehouse;
    }
    registerRobot(body, user) { return this.warehouse.registerRobot(body, user); }
    robots(user) { return this.warehouse.robots(user); }
    projects(user) { return this.warehouse.projects(user); }
    sources(user) { return this.warehouse.sources(user); }
    overview(user) { return this.warehouse.overview(user); }
};
exports.WarehouseController = WarehouseController;
__decorate([
    (0, common_1.Post)('robots'),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN),
    (0, swagger_1.ApiOperation)({ summary: 'Register a physical robot in an active project' }),
    __param(0, (0, common_1.Body)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [RegisterRobotDto, Object]),
    __metadata("design:returntype", void 0)
], WarehouseController.prototype, "registerRobot", null);
__decorate([
    (0, common_1.Get)('robots'),
    (0, swagger_1.ApiOperation)({ summary: 'List accessible physical robot sources for selection' }),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], WarehouseController.prototype, "robots", null);
__decorate([
    (0, common_1.Get)('projects'),
    (0, swagger_1.ApiOperation)({ summary: 'List accessible warehouse projects' }),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], WarehouseController.prototype, "projects", null);
__decorate([
    (0, common_1.Get)('sources'),
    (0, swagger_1.ApiOperation)({ summary: 'List accessible file sources' }),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], WarehouseController.prototype, "sources", null);
__decorate([
    (0, common_1.Get)('overview'),
    (0, swagger_1.ApiOperation)({ summary: 'Get the current user warehouse overview' }),
    __param(0, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", void 0)
], WarehouseController.prototype, "overview", null);
exports.WarehouseController = WarehouseController = __decorate([
    (0, swagger_1.ApiTags)('warehouse'),
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('warehouse'),
    __metadata("design:paramtypes", [warehouse_query_service_1.WarehouseQueryService])
], WarehouseController);
//# sourceMappingURL=warehouse.controller.js.map