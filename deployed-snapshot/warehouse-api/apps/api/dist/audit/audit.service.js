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
exports.AuditService = void 0;
const common_1 = require("@nestjs/common");
const prisma_service_1 = require("../prisma/prisma.service");
let AuditService = class AuditService {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    record(input) {
        return this.prisma.auditLog.create({ data: input });
    }
    async page(query, scope) {
        const where = { action: query.action, actorUserId: query.actorUserId, ...(!scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN') ? {} : { tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) }), ...(query.search && { OR: [{ action: { contains: query.search, mode: 'insensitive' } }, { resourceType: { contains: query.search, mode: 'insensitive' } }, { resourceId: { contains: query.search, mode: 'insensitive' } }] }) };
        const [items, total] = await this.prisma.$transaction([this.prisma.auditLog.findMany({ where, include: { actor: { select: { id: true, email: true, displayName: true, role: true } } }, orderBy: { createdAt: 'desc' }, skip: (query.page - 1) * query.pageSize, take: query.pageSize }), this.prisma.auditLog.count({ where })]);
        return { items, total, page: query.page, pageSize: query.pageSize };
    }
};
exports.AuditService = AuditService;
exports.AuditService = AuditService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], AuditService);
//# sourceMappingURL=audit.service.js.map