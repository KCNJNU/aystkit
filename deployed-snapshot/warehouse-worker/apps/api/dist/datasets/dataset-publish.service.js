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
exports.DatasetPublishService = void 0;
const common_1 = require("@nestjs/common");
const client_1 = require("@prisma/client");
const node_crypto_1 = require("node:crypto");
const promises_1 = require("node:fs/promises");
const node_path_1 = require("node:path");
const dvc_service_1 = require("../dvc/dvc.service");
const prisma_service_1 = require("../prisma/prisma.service");
const object_storage_1 = require("../storage/object-storage");
const object_stream_1 = require("../storage/object-stream");
let DatasetPublishService = class DatasetPublishService {
    prisma;
    storage;
    dvc;
    constructor(prisma, storage, dvc) {
        this.prisma = prisma;
        this.storage = storage;
        this.dvc = dvc;
    }
    async publish(id, runStep = (_name, action) => action(), signal) {
        signal?.throwIfAborted();
        const { version, manifest } = await runStep('Validation', async () => {
            const value = await this.prisma.datasetVersion.findUnique({ where: { id }, include: { dataset: true, files: { include: { robotFile: true }, orderBy: { robotFileId: 'asc' } } } });
            if (!value)
                throw new common_1.NotFoundException('Dataset version not found');
            const publishable = value.status === client_1.DatasetVersionStatus.FROZEN
                || value.status === client_1.DatasetVersionStatus.PUBLISHED && value.gitTag && value.dvcPath;
            if (!publishable)
                throw new common_1.ConflictException('Only FROZEN dataset versions can be published');
            if (!value.manifestKey || !value.manifestChecksum)
                throw new common_1.ConflictException('Dataset version has no manifest');
            const content = await this.readObject(value.manifestKey);
            if ((0, node_crypto_1.createHash)('sha256').update(content).digest('hex') !== value.manifestChecksum)
                throw new common_1.ConflictException('Stored manifest checksum does not match the dataset version');
            return { version: value, manifest: content };
        });
        if (version.status === client_1.DatasetVersionStatus.PUBLISHED) {
            await runStep('Recovery Check', () => this.requireRemoteReachable());
            return version;
        }
        const workspacePath = (0, node_path_1.join)('data', 'dataset-workspace', `${this.workspaceName(version.dataset.name)}-${version.datasetId}`, String(version.version));
        const absoluteWorkspace = this.dvc.resolveWorkspace(workspacePath);
        await runStep('Workspace Snapshot', async () => {
            await (0, promises_1.mkdir)((0, node_path_1.join)(absoluteWorkspace, 'files'), { recursive: true });
            signal?.throwIfAborted();
            await (0, promises_1.writeFile)((0, node_path_1.join)(absoluteWorkspace, 'manifest.json'), manifest, { signal });
            for (const membership of version.files) {
                signal?.throwIfAborted();
                const robotFile = membership.robotFile;
                const destination = (0, node_path_1.join)(absoluteWorkspace, 'files', `${robotFile.id}${(0, node_path_1.extname)(robotFile.objectKey) || (0, node_path_1.extname)(robotFile.originalName)}`);
                const temporary = (0, node_path_1.join)(absoluteWorkspace, '..', '.dataset-snapshot-' + (0, node_crypto_1.randomUUID)() + '.partial');
                let file;
                try {
                    file = await (0, promises_1.open)(temporary, 'wx', 0o600);
                    const inspection = await (0, object_stream_1.inspectStoredObject)(this.storage, robotFile.objectKey, async (chunk) => { signal?.throwIfAborted(); await file.writeFile(chunk); }, { signal });
                    if (inspection.checksum !== robotFile.checksum)
                        throw new common_1.ConflictException(`Checksum mismatch for RobotFile ${robotFile.id}`);
                    if (inspection.size !== robotFile.fileSize)
                        throw new common_1.ConflictException(`Size mismatch for RobotFile ${robotFile.id}`);
                    await file.sync();
                    await file.close();
                    file = undefined;
                    signal?.throwIfAborted();
                    await (0, promises_1.rename)(temporary, destination);
                }
                finally {
                    if (file)
                        await file.close().catch(() => undefined);
                    await (0, promises_1.unlink)(temporary).catch(() => undefined);
                }
            }
            return { workspacePath };
        });
        const publication = await this.dvc.publish(workspacePath, version.dataset.name, version.version, runStep, version.datasetId, signal);
        await runStep('Recovery Check', () => this.requireRemoteReachable());
        return runStep('Persist Version', () => this.prisma.datasetVersion.update({ where: { id }, data: { status: client_1.DatasetVersionStatus.PUBLISHED, ...publication } }));
    }
    async recoveryVerify(id, runStep = (_name, action) => action(), signal) {
        signal?.throwIfAborted();
        const version = await this.prisma.datasetVersion.findUnique({ where: { id }, include: { files: { include: { robotFile: true } } } });
        if (!version)
            throw new common_1.NotFoundException('Dataset version not found');
        if (version.status !== client_1.DatasetVersionStatus.PUBLISHED || !version.gitTag || !version.dvcPath || !version.manifestChecksum)
            throw new common_1.ConflictException('Only a fully published version can be recovery verified');
        return this.dvc.recoveryVerify(version.gitTag, version.dvcPath, async (workspaceRoot) => {
            await runStep('Verify Manifest and Files', async () => {
                const manifestPath = (0, node_path_1.join)(workspaceRoot, version.dvcPath.replace(/\.dvc$/, ''), 'manifest.json');
                const manifest = await Promise.resolve().then(() => require('node:fs/promises')).then((fs) => fs.readFile(manifestPath));
                if ((0, node_crypto_1.createHash)('sha256').update(manifest).digest('hex') !== version.manifestChecksum)
                    throw new common_1.ConflictException('Recovered manifest checksum mismatch');
                const parsed = JSON.parse(manifest.toString('utf8'));
                const expected = new Map(version.files.map((item) => [item.robotFileId, item.robotFile]));
                if (parsed.files?.length !== expected.size)
                    throw new common_1.ConflictException('Recovered manifest membership mismatch');
                for (const item of parsed.files ?? []) {
                    const file = expected.get(item.fileId);
                    if (!file || file.checksum !== item.checksum || file.fileSize.toString() !== item.fileSize)
                        throw new common_1.ConflictException(`Recovered member metadata mismatch for ${item.fileId}`);
                }
                return { files: expected.size };
            });
        }, runStep, signal);
    }
    async readObject(objectKey) {
        return (0, object_stream_1.readManifestObject)(this.storage, objectKey);
    }
    async requireRemoteReachable() {
        const health = await this.dvc.checkRemoteHealth();
        if (!health.configured || !health.reachable)
            throw new common_1.ConflictException('DVC remote is not reachable after publish');
        return health;
    }
    workspaceName(name) { return (0, node_path_1.basename)(name.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'dataset'); }
};
exports.DatasetPublishService = DatasetPublishService;
exports.DatasetPublishService = DatasetPublishService = __decorate([
    (0, common_1.Injectable)(),
    __param(1, (0, common_1.Inject)(object_storage_1.OBJECT_STORAGE)),
    __metadata("design:paramtypes", [prisma_service_1.PrismaService, Object, dvc_service_1.DvcService])
], DatasetPublishService);
//# sourceMappingURL=dataset-publish.service.js.map