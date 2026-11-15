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
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const auth_decorators_1 = require("../auth/auth.decorators");
const permissions_1 = require("../auth/permissions");
const warehouse_query_service_1 = require("./warehouse-query.service");
let WarehouseController = class WarehouseController {
    warehouse;
    constructor(warehouse) {
        this.warehouse = warehouse;
    }
    projects(user) { return this.warehouse.projects(user); }
    sources(user) { return this.warehouse.sources(user); }
    overview(user) { return this.warehouse.overview(user); }
};
exports.WarehouseController = WarehouseController;
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