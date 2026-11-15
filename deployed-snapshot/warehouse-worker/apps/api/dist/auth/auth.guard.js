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
Object.defineProperty(exports, "__esModule", { value: true });
exports.AuthGuard = void 0;
const common_1 = require("@nestjs/common");
const core_1 = require("@nestjs/core");
const client_1 = require("@prisma/client");
const auth_service_1 = require("./auth.service");
const auth_decorators_1 = require("./auth.decorators");
let AuthGuard = class AuthGuard {
    reflector;
    auth;
    constructor(reflector, auth) {
        this.reflector = reflector;
        this.auth = auth;
    }
    async canActivate(context) {
        if (this.reflector.getAllAndOverride(auth_decorators_1.PUBLIC_ROUTE, [context.getHandler(), context.getClass()]))
            return true;
        const request = context.switchToHttp().getRequest();
        const token = request.cookies?.cr_session;
        if (!token)
            throw new common_1.UnauthorizedException('Authentication required');
        const resolved = await this.auth.resolveSession(token);
        if (!resolved)
            throw new common_1.UnauthorizedException('Session is invalid or expired');
        request.user = resolved.user;
        request.sessionId = resolved.sessionId;
        request.csrfTokenHash = resolved.csrfTokenHash;
        const method = request.method.toUpperCase();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && !this.auth.verifyCsrf(request.header('x-csrf-token'), resolved.csrfTokenHash))
            throw new common_1.ForbiddenException('Invalid CSRF token');
        if (resolved.user.mustChangePassword && !request.path.endsWith('/auth/change-password') && !request.path.endsWith('/auth/logout') && !request.path.endsWith('/auth/session'))
            throw new common_1.ForbiddenException('PASSWORD_CHANGE_REQUIRED');
        const roles = this.reflector.getAllAndOverride(auth_decorators_1.ROLES, [context.getHandler(), context.getClass()]);
        const hasRequiredRole = !roles?.length
            || roles.includes(resolved.user.role)
            || ([client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN].includes(resolved.user.role) && roles.includes(client_1.UserRole.ADMIN));
        if (!hasRequiredRole)
            throw new common_1.ForbiddenException('Insufficient role');
        return true;
    }
};
exports.AuthGuard = AuthGuard;
exports.AuthGuard = AuthGuard = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [core_1.Reflector, auth_service_1.AuthService])
], AuthGuard);
//# sourceMappingURL=auth.guard.js.map