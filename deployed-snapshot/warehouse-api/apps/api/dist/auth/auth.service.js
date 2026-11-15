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
exports.AuthService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const argon2 = require("argon2");
const node_crypto_1 = require("node:crypto");
const prisma_service_1 = require("../prisma/prisma.service");
const hashToken = (value) => (0, node_crypto_1.createHash)('sha256').update(value).digest('hex');
const safeUser = (user) => ({ id: user.id, email: user.email, displayName: user.displayName, role: user.role === 'ADMIN' ? 'SUPER_ADMIN' : user.role, tenantId: user.tenantId, projectIds: user.projectAccess?.map((access) => access.projectId) ?? [], mustChangePassword: user.mustChangePassword, isActive: user.isActive });
let AuthService = class AuthService {
    prisma;
    config;
    constructor(prisma, config) {
        this.prisma = prisma;
        this.config = config;
    }
    async login(emailInput, password) {
        const account = emailInput.trim().toLowerCase();
        const email = account === 'admin' ? this.config.get('BOOTSTRAP_ADMIN_EMAIL')?.trim().toLowerCase() : account;
        if (!email)
            throw new common_1.UnauthorizedException('Invalid account or password');
        const user = await this.prisma.user.findUnique({ where: { email }, include: { projectAccess: { select: { projectId: true } } } });
        if (!user || !user.isActive || (account === 'admin' && !['ADMIN', 'SUPER_ADMIN'].includes(user.role)))
            throw new common_1.UnauthorizedException('Invalid account or password');
        if (user.lockedUntil && user.lockedUntil > new Date())
            throw new common_1.UnauthorizedException('Account is temporarily locked');
        if (!await argon2.verify(user.passwordHash, password).catch(() => false)) {
            const failures = user.failedLoginCount + 1;
            await this.prisma.user.update({ where: { id: user.id }, data: { failedLoginCount: failures >= 5 ? 0 : failures, lockedUntil: failures >= 5 ? new Date(Date.now() + 15 * 60_000) : null } });
            throw new common_1.UnauthorizedException('Invalid email or password');
        }
        const sessionToken = (0, node_crypto_1.randomBytes)(32).toString('base64url');
        const csrfToken = (0, node_crypto_1.randomBytes)(32).toString('base64url');
        const expiresAt = new Date(Date.now() + this.sessionHours() * 60 * 60_000);
        const session = await this.prisma.$transaction(async (tx) => {
            await tx.user.update({ where: { id: user.id }, data: { failedLoginCount: 0, lockedUntil: null, lastLoginAt: new Date() } });
            return tx.session.create({ data: { userId: user.id, tokenHash: hashToken(sessionToken), csrfTokenHash: hashToken(csrfToken), expiresAt } });
        });
        return { user: safeUser(user), sessionToken, csrfToken, sessionId: session.id, expiresAt };
    }
    async resolveSession(token) {
        const session = await this.prisma.session.findUnique({ where: { tokenHash: hashToken(token) }, include: { user: { include: { projectAccess: { select: { projectId: true } } } } } });
        if (!session || session.expiresAt <= new Date() || !session.user.isActive) {
            if (session)
                await this.prisma.session.delete({ where: { id: session.id } }).catch(() => undefined);
            return null;
        }
        if (Date.now() - session.lastSeenAt.getTime() > 5 * 60_000)
            await this.prisma.session.update({ where: { id: session.id }, data: { lastSeenAt: new Date() } });
        return { sessionId: session.id, csrfTokenHash: session.csrfTokenHash, user: safeUser(session.user) };
    }
    async rotateCsrf(sessionId) {
        const csrfToken = (0, node_crypto_1.randomBytes)(32).toString('base64url');
        await this.prisma.session.update({ where: { id: sessionId }, data: { csrfTokenHash: hashToken(csrfToken) } });
        return csrfToken;
    }
    verifyCsrf(value, expectedHash) { return Boolean(value && expectedHash && hashToken(value) === expectedHash); }
    async logout(sessionId) { await this.prisma.session.delete({ where: { id: sessionId } }).catch(() => undefined); }
    async changePassword(userId, currentPassword, newPassword) {
        if (newPassword.length < 12)
            throw new common_1.BadRequestException('Password must contain at least 12 characters');
        const user = await this.prisma.user.findUnique({ where: { id: userId } });
        if (!user || !await argon2.verify(user.passwordHash, currentPassword).catch(() => false))
            throw new common_1.ForbiddenException('Current password is incorrect');
        const passwordHash = await argon2.hash(newPassword, { type: argon2.argon2id });
        await this.prisma.$transaction([this.prisma.user.update({ where: { id: userId }, data: { passwordHash, mustChangePassword: false } }), this.prisma.session.deleteMany({ where: { userId } })]);
    }
    cookieOptions() {
        const configured = this.config.get('AUTH_COOKIE_SECURE');
        const secure = configured === 'true' || configured === true ? true
            : configured === 'false' || configured === false ? false
                : this.config.get('NODE_ENV') === 'production';
        return { httpOnly: true, sameSite: 'lax', secure, maxAge: this.sessionHours() * 60 * 60_000, path: '/' };
    }
    sessionHours() { const value = Number(this.config.get('AUTH_SESSION_HOURS') ?? 12); return Number.isFinite(value) && value > 0 && value <= 168 ? value : 12; }
};
exports.AuthService = AuthService;
exports.AuthService = AuthService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, config_1.ConfigService])
], AuthService);
//# sourceMappingURL=auth.service.js.map