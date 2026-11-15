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
exports.HealthController = void 0;
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const auth_decorators_1 = require("../auth/auth.decorators");
const health_service_1 = require("./health.service");
let HealthController = class HealthController {
    health;
    constructor(health) {
        this.health = health;
    }
    liveness() { return { status: 'healthy', timestamp: new Date().toISOString() }; }
    async ready(response) {
        const report = await this.health.check();
        const ready = ['postgresql', 'redis', 'objectStorage'].every(key => ['healthy', 'slow'].includes(report.services[key].status));
        response.status(ready ? 200 : 503);
        return { status: ready ? 'healthy' : 'unhealthy', timestamp: report.timestamp };
    }
    status() { return this.health.check(); }
};
exports.HealthController = HealthController;
__decorate([
    (0, auth_decorators_1.Public)(),
    (0, common_1.Get)('health'),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", []),
    __metadata("design:returntype", void 0)
], HealthController.prototype, "liveness", null);
__decorate([
    (0, auth_decorators_1.Public)(),
    (0, common_1.Get)('ready'),
    __param(0, (0, common_1.Res)({ passthrough: true })),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [Object]),
    __metadata("design:returntype", Promise)
], HealthController.prototype, "ready", null);
__decorate([
    (0, common_1.Get)('system/status'),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", []),
    __metadata("design:returntype", void 0)
], HealthController.prototype, "status", null);
exports.HealthController = HealthController = __decorate([
    (0, swagger_1.ApiTags)('system'),
    (0, common_1.Controller)(),
    __metadata("design:paramtypes", [health_service_1.HealthService])
], HealthController);
//# sourceMappingURL=health.controller.js.map