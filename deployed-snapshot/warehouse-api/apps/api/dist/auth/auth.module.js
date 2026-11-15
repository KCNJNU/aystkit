"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.AuthModule = void 0;
const common_1 = require("@nestjs/common");
const core_1 = require("@nestjs/core");
const throttler_1 = require("@nestjs/throttler");
const auth_controller_1 = require("./auth.controller");
const auth_guard_1 = require("./auth.guard");
const auth_service_1 = require("./auth.service");
const login_rate_limit_guard_1 = require("./login-rate-limit.guard");
const redis_throttler_storage_1 = require("./redis-throttler.storage");
const production_security_validator_1 = require("./production-security.validator");
let AuthModule = class AuthModule {
};
exports.AuthModule = AuthModule;
exports.AuthModule = AuthModule = __decorate([
    (0, common_1.Global)(),
    (0, common_1.Module)({ imports: [throttler_1.ThrottlerModule.forRoot([{ name: 'default', ttl: 60_000, limit: 100 }])], controllers: [auth_controller_1.AuthController], providers: [auth_service_1.AuthService, login_rate_limit_guard_1.LoginRateLimitGuard, production_security_validator_1.ProductionSecurityValidator, { provide: throttler_1.ThrottlerStorage, useClass: redis_throttler_storage_1.RedisThrottlerStorage }, { provide: core_1.APP_GUARD, useClass: auth_guard_1.AuthGuard }], exports: [auth_service_1.AuthService] })
], AuthModule);
//# sourceMappingURL=auth.module.js.map