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
exports.SearchService = void 0;
const dataset_robot_scope_1 = require("../datasets/dataset-robot-scope");
const common_1 = require("@nestjs/common");
const prisma_service_1 = require("../prisma/prisma.service");
let SearchService = class SearchService {
    prisma;
    constructor(prisma) {
        this.prisma = prisma;
    }
    async search(rawQuery, limit, scope, robotId) {
        const q = rawQuery.trim();
        const contains = { contains: q, mode: 'insensitive' };
        const tenant = !scope || scope.role === 'ADMIN' || (scope.role === 'SUPER_ADMIN' || scope.role === 'SYSTEM_ADMIN') ? {} : { tenantId: scope.tenantId ?? '__NO_TENANT__', ...(scope.role === 'TENANT_ADMIN' ? {} : { projectId: { in: scope.projectIds } }) };
        const scoped = Object.keys(tenant).length > 0;
        const [, files, datasets, versions] = await Promise.all([
            Promise.resolve([]),
            this.prisma.robotFile.findMany({ where: { ...(scoped && { robot: tenant }), ...(robotId ? { robotId } : {}), OR: [{ originalName: contains }, { objectKey: contains }, { checksum: contains }] }, select: { id: true, originalName: true, objectKey: true, checksum: true, robot: { select: { name: true, externalId: true } } }, take: limit, orderBy: { createdAt: 'desc' } }),
            this.prisma.dataset.findMany({ where: { ...tenant, deletingAt: null, AND: [(0, dataset_robot_scope_1.datasetRobotScope)(robotId)], OR: [{ name: contains }, { description: contains }] }, select: { id: true, name: true, description: true }, take: limit, orderBy: { updatedAt: 'desc' } }),
            this.prisma.datasetVersion.findMany({ where: { dataset: { ...tenant, deletingAt: null, AND: [(0, dataset_robot_scope_1.datasetRobotScope)(robotId)] }, OR: [{ gitTag: contains }, { gitCommit: contains }, { dataset: { name: contains } }] }, select: { id: true, version: true, status: true, gitTag: true, dataset: { select: { id: true, name: true } } }, take: limit, orderBy: { updatedAt: 'desc' } }),
        ]);
        return {
            query: q,
            groups: {
                robots: [],
                files: files.map((item) => ({ type: 'file', id: item.id, title: item.originalName, subtitle: `${item.robot.externalId ?? item.robot.name} · ${item.objectKey}`, match: item.checksum })),
                datasets: datasets.map((item) => ({ type: 'dataset', id: item.id, title: item.name, subtitle: item.description ?? 'Dataset' })),
                versions: versions.map((item) => ({ type: 'version', id: item.id, title: `${item.dataset.name} v${item.version}`, subtitle: item.gitTag ?? item.status, datasetId: item.dataset.id })),
            },
        };
    }
};
exports.SearchService = SearchService;
exports.SearchService = SearchService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService])
], SearchService);
//# sourceMappingURL=search.service.js.map