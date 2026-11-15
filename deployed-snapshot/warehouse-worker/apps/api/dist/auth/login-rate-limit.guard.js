"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.LoginRateLimitGuard = void 0;
const common_1 = require("@nestjs/common");
const throttler_1 = require("@nestjs/throttler");
let LoginRateLimitGuard = class LoginRateLimitGuard extends throttler_1.ThrottlerGuard {
    async getTracker(request) {
        const body = typeof request.body === 'object' && request.body !== null ? request.body : {};
        const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : 'invalid-account';
        const ip = typeof request.ip === 'string' ? request.ip : 'unknown-ip';
        return `${ip}:${email}`;
    }
};
exports.LoginRateLimitGuard = LoginRateLimitGuard;
exports.LoginRateLimitGuard = LoginRateLimitGuard = __decorate([
    (0, common_1.Injectable)()
], LoginRateLimitGuard);
//# sourceMappingURL=login-rate-limit.guard.js.map