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
exports.AuditController = void 0;
const common_1 = require("@nestjs/common");
const class_transformer_1 = require("class-transformer");
const class_validator_1 = require("class-validator");
const client_1 = require("@prisma/client");
const auth_decorators_1 = require("../auth/auth.decorators");
const audit_service_1 = require("./audit.service");
class AuditQuery {
    page = 1;
    pageSize = 20;
    search;
    action;
    actorUserId;
}
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    __metadata("design:type", Object)
], AuditQuery.prototype, "page", void 0);
__decorate([
    (0, class_transformer_1.Type)(() => Number),
    (0, class_validator_1.IsInt)(),
    (0, class_validator_1.Min)(1),
    (0, class_validator_1.Max)(100),
    __metadata("design:type", Object)
], AuditQuery.prototype, "pageSize", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], AuditQuery.prototype, "search", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsString)(),
    __metadata("design:type", String)
], AuditQuery.prototype, "action", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], AuditQuery.prototype, "actorUserId", void 0);
let AuditController = class AuditController {
    audit;
    constructor(audit) {
        this.audit = audit;
    }
    page(query, user) { return this.audit.page(query, user); }
    async export(query, user) { const page = await this.audit.page({ ...query, page: 1, pageSize: 100 }, user); return ['createdAt,actor,action,resourceType,resourceId,statusCode', ...page.items.map((item) => [item.createdAt.toISOString(), item.actor?.email ?? 'system', item.action, item.resourceType ?? '', item.resourceId ?? '', item.statusCode ?? ''].map(csv).join(','))].join('\n'); }
};
exports.AuditController = AuditController;
__decorate([
    (0, common_1.Get)('page'),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [AuditQuery, Object]),
    __metadata("design:returntype", void 0)
], AuditController.prototype, "page", null);
__decorate([
    (0, common_1.Get)('export'),
    (0, common_1.Header)('Content-Type', 'text/csv; charset=utf-8'),
    __param(0, (0, common_1.Query)()),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [AuditQuery, Object]),
    __metadata("design:returntype", Promise)
], AuditController.prototype, "export", null);
exports.AuditController = AuditController = __decorate([
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.TENANT_ADMIN),
    (0, common_1.Controller)('v1/audit-logs'),
    __metadata("design:paramtypes", [audit_service_1.AuditService])
], AuditController);
const csv = (value) => `"${String(value).replace(/"/g, '""')}"`;
//# sourceMappingURL=audit.controller.js.map