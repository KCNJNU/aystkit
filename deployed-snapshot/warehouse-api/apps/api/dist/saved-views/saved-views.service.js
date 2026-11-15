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
exports.SavedViewsService = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const prisma_service_1 = require("../prisma/prisma.service");
const ALLOWED = {
    ROBOTS: new Set(['search', 'activity', 'sortBy', 'sortOrder', 'pageSize']),
    FILES: new Set(['search', 'robotId', 'fileType', 'recognitionType', 'schemaVersion', 'tagId', 'startTime', 'endTime', 'uploadedStartTime', 'uploadedEndTime', 'minSize', 'maxSize', 'sortBy', 'sortOrder', 'pageSize', 'view']),
    DATASETS: new Set(['search', 'status', 'tagId', 'sortBy', 'sortOrder', 'pageSize']),
    VERSIONS: new Set(['search', 'datasetId', 'status', 'sortBy', 'sortOrder', 'pageSize']),
    JOBS: new Set(['search', 'status', 'type', 'pageSize']),
    AUDIT: new Set(['search', 'actorUserId', 'action', 'startTime', 'endTime', 'pageSize']),
    STORAGE: new Set(['prefix', 'mapping', 'pageSize']),
};
let SavedViewsService = class SavedViewsService {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    list(userId, resource) { return this.prisma.savedView.findMany({ where: { resource, OR: [{ ownerId: userId }, { isShared: true }] }, include: { owner: { select: { id: true, displayName: true } } }, orderBy: { updatedAt: 'desc' } }); }
    create(userId, role, input) { this.assertShare(role, input.isShared); return this.prisma.savedView.create({ data: { ownerId: userId, resource: input.resource, name: input.name.trim(), query: this.validate(input.resource, input.query), isShared: input.isShared ?? false } }); }
    async update(id, userId, role, input) { const view = await this.getOwned(id, userId); this.assertShare(role, input.isShared); return this.prisma.savedView.update({ where: { id }, data: { name: input.name?.trim(), query: input.query === undefined ? undefined : this.validate(view.resource, input.query), isShared: input.isShared } }); }
    async remove(id, userId) { await this.getOwned(id, userId); await this.prisma.savedView.delete({ where: { id } }); }
    async copy(id, userId, name) { const source = await this.prisma.savedView.findFirst({ where: { id, OR: [{ ownerId: userId }, { isShared: true }] } }); if (!source)
        throw new common_1.NotFoundException('Saved view not found'); return this.prisma.savedView.create({ data: { ownerId: userId, resource: source.resource, name: name?.trim() || `${source.name} copy`, query: source.query } }); }
    validate(resource, value) { if (!value || Array.isArray(value) || typeof value !== 'object')
        throw new common_1.BadRequestException('query must be an object'); const entries = Object.entries(value); const invalid = entries.find(([key]) => !ALLOWED[resource].has(key)); if (invalid)
        throw new common_1.BadRequestException(`Unsupported saved view field: ${invalid[0]}`); return Object.fromEntries(entries.filter(([, item]) => ['string', 'number', 'boolean'].includes(typeof item) || item === null)); }
    assertShare(role, shared) { if (shared && role === client_1.UserRole.VIEWER)
        throw new common_1.ForbiddenException('Viewers cannot share saved views'); }
    async getOwned(id, ownerId) { const view = await this.prisma.savedView.findFirst({ where: { id, ownerId } }); if (!view)
        throw new common_1.NotFoundException('Saved view not found'); return view; }
};
exports.SavedViewsService = SavedViewsService;
exports.SavedViewsService = SavedViewsService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], SavedViewsService);
//# sourceMappingURL=saved-views.service.js.map