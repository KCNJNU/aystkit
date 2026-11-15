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
exports.ActivityController = void 0;
const auth_decorators_1 = require("../auth/auth.decorators");
const permissions_1 = require("../auth/permissions");
const common_1 = require("@nestjs/common");
const class_transformer_1 = require("class-transformer");
const class_validator_1 = require("class-validator");
const prisma_service_1 = require("../prisma/prisma.service");
class ActivityQuery {
    page = 1;
    pageSize = 30;
    search;
}
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    __metadata("design:type", Object)
], ActivityQuery.prototype, "page", void 0);
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(100),
    __metadata("design:type", Object)
], ActivityQuery.prototype, "pageSize", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], ActivityQuery.prototype, "search", void 0);
let ActivityController = class ActivityController {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    async page(query, actor) {
        const take = Math.min(100, query.page * query.pageSize);
        const [audits, jobs] = await Promise.all([
            this.prisma.auditLog.findMany({ where: { ...((0, permissions_1.isGlobalAdmin)(actor.role) ? {} : { tenantId: actor.tenantId ?? '__NO_TENANT__', ...(actor.role === 'TENANT_ADMIN' ? {} : { projectId: { in: actor.projectIds } }) }), ...(query.search ? { OR: [{ action: { contains: query.search, mode: 'insensitive' } }, { resourceType: { contains: query.search, mode: 'insensitive' } }] } : {}) }, include: { actor: { select: { id: true, displayName: true, email: true } } }, orderBy: { createdAt: 'desc' }, take }),
            this.prisma.job.findMany({ where: { ...((0, permissions_1.isGlobalAdmin)(actor.role) ? {} : { tenantId: actor.tenantId ?? '__NO_TENANT__', ...(actor.role === 'TENANT_ADMIN' ? {} : { projectId: { in: actor.projectIds } }) }), ...(query.search ? { OR: [{ resourceType: { contains: query.search, mode: 'insensitive' } }, { resourceId: { contains: query.search, mode: 'insensitive' } }] } : {}) }, include: { requestedBy: { select: { id: true, displayName: true, email: true } } }, orderBy: { updatedAt: 'desc' }, take }),
        ]);
        const merged = [
            ...audits.map((item) => ({ id: `audit:${item.id}`, source: 'audit', type: item.action, status: item.statusCode && item.statusCode >= 400 ? 'failed' : 'completed', actor: item.actor, resourceType: item.resourceType, resourceId: item.resourceId, timestamp: item.createdAt })),
            ...jobs.map((item) => ({ id: `job:${item.id}`, source: 'job', type: item.type, status: item.status.toLowerCase(), actor: item.requestedBy, resourceType: item.resourceType, resourceId: item.resourceId, jobId: item.id, timestamp: item.updatedAt })),
        ].sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());
        const start = (query.page - 1) * query.pageSize;
        return { items: merged.slice(start, start + query.pageSize), total: merged.length < take * 2 ? start + merged.length : start + merged.length + 1, page: query.page, pageSize: query.pageSize };
    }
};
exports.ActivityController = ActivityController;
__decorate([
    (0, common_1.Get)('page'),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [ActivityQuery, Object]),
    __metadata("design:returntype", Promise)
], ActivityController.prototype, "page", null);
exports.ActivityController = ActivityController = __decorate([
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/activity'),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], ActivityController);
//# sourceMappingURL=activity.controller.js.map