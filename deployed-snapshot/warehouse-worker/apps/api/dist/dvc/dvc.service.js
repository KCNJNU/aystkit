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
exports.DvcService = void 0;
exports.parseDvcHash = parseDvcHash;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const Minio = require("minio");
const node_child_process_1 = require("node:child_process");
const promises_1 = require("node:fs/promises");
const node_os_1 = require("node:os");
const node_path_1 = require("node:path");
const node_util_1 = require("node:util");
const execFileAsync = (0, node_util_1.promisify)(node_child_process_1.execFile);
function parseDvcHash(pointer) {
    return /^\s*(?:-\s*)?md5:\s*([^\s]+)$/m.exec(pointer)?.[1];
}
let DvcService = class DvcService {
    config;
    repositoryRoot;
    constructor(config) {
        this.config = config;
        this.repositoryRoot = (0, node_path_1.resolve)(this.config.get('DVC_REPOSITORY_ROOT')?.trim() || (0, node_path_1.resolve)(__dirname, '..', '..', '..', '..'));
    }
    resolveWorkspace(workspacePath) {
        const target = (0, node_path_1.resolve)(this.repositoryRoot, workspacePath);
        const child = (0, node_path_1.relative)(this.repositoryRoot, target);
        if (child.startsWith('..') || target === this.repositoryRoot)
            throw new common_1.ServiceUnavailableException('DVC workspace path is outside the configured repository');
        return target;
    }
    async checkRemoteHealth() {
        const endPoint = this.config.get('DVC_REMOTE_ENDPOINT');
        const port = this.config.get('DVC_REMOTE_PORT');
        const useSsl = this.config.get('DVC_REMOTE_USE_SSL');
        const bucket = this.config.get('DVC_REMOTE_BUCKET');
        const accessKey = this.config.get('DVC_REMOTE_ACCESS_KEY');
        const secretKey = this.config.get('DVC_REMOTE_SECRET_KEY');
        const remoteName = this.config.get('DVC_REMOTE_NAME');
        if (!endPoint || !port || !useSsl || !bucket || !accessKey || !secretKey || !remoteName)
            return { configured: false };
        const client = new Minio.Client({ endPoint, port: Number(port), useSSL: useSsl === 'true', accessKey, secretKey });
        try {
            return { configured: true, provider: 's3-compatible', bucket, reachable: await client.bucketExists(bucket) };
        }
        catch {
            return { configured: true, provider: 's3-compatible', bucket, reachable: false };
        }
    }
    async publish(workspacePath, datasetName, version, runStep = (_name, action) => action(), datasetId, signal) {
        const relativeWorkspacePath = workspacePath.replace(/\\/g, '/');
        const remote = this.remoteConfig();
        if (remote.bucket === this.config.getOrThrow('OBJECT_STORAGE_BUCKET')) {
            throw new common_1.ServiceUnavailableException('DVC remote bucket must not be the raw object-storage bucket');
        }
        signal?.throwIfAborted();
        await this.ensureDvcRepository(remote, signal);
        await runStep('DVC Add', () => this.runDvc(['add', relativeWorkspacePath], remote, signal));
        const dvcPath = `${relativeWorkspacePath}.dvc`;
        const dvcHash = await this.readDvcHash(dvcPath);
        await runStep('DVC Push', () => this.runDvc(['push'], remote, signal));
        return runStep('Git Commit/Tag', async () => {
            const gitTag = datasetId ? `dataset-${datasetId}-v${version}.0.0` : this.datasetTag(datasetName, version);
            const tagExists = await this.runGit(['rev-parse', '-q', '--verify', `refs/tags/${gitTag}`], true, signal);
            if (tagExists.code === 0)
                return { dvcPath, dvcHash, gitCommit: (await this.runGit(['rev-list', '-n', '1', gitTag], false, signal)).stdout.trim(), gitTag };
            await this.runGit(['add', '--', '.dvc/config', dvcPath], false, signal);
            const staged = await this.runGit(['diff', '--cached', '--quiet'], true, signal);
            if (staged.code !== 0)
                await this.runGit(['-c', `user.name=${remote.gitAuthorName}`, '-c', `user.email=${remote.gitAuthorEmail}`, 'commit', '-m', `Publish dataset ${datasetName} v${version}.0.0`], false, signal);
            const gitCommit = (await this.runGit(['rev-parse', 'HEAD'], false, signal)).stdout.trim();
            await this.runGit(['-c', `user.name=${remote.gitAuthorName}`, '-c', `user.email=${remote.gitAuthorEmail}`, 'tag', '-a', gitTag, '-m', `Dataset ${datasetName} version ${version}.0.0`], false, signal);
            return { dvcPath, dvcHash, gitCommit, gitTag };
        });
    }
    async recoveryVerify(gitTag, dvcPath, verify, runStep, signal) {
        signal?.throwIfAborted();
        const remote = this.remoteConfig();
        const temporaryRoot = await runStep('Prepare Temporary Workspace', () => (0, promises_1.mkdtemp)((0, node_path_1.join)((0, node_os_1.tmpdir)(), 'robot-dvc-recovery-')));
        try {
            await runStep('Checkout Git Tag', () => this.runGit(['worktree', 'add', '--detach', temporaryRoot, gitTag], false, signal));
            await runStep('DVC Pull', () => this.run(this.config.getOrThrow('DVC_COMMAND'), ['pull', dvcPath], remote, false, temporaryRoot, signal));
            await verify(temporaryRoot);
            return { verified: true, gitTag, dvcPath };
        }
        finally {
            if (!signal?.aborted)
                await runStep('Cleanup', async () => { await this.runGit(['worktree', 'remove', '--force', temporaryRoot], true, signal); await (0, promises_1.rm)(temporaryRoot, { recursive: true, force: true }); });
        }
    }
    async ensureDvcRepository(remote, signal) {
        signal?.throwIfAborted();
        try {
            await (0, promises_1.access)((0, node_path_1.resolve)(this.repositoryRoot, '.dvc', 'config'));
        }
        catch {
            signal?.throwIfAborted();
            await this.runDvc(['init'], undefined, signal);
        }
        await this.runDvc(['remote', 'add', '-f', '-d', remote.name, `s3://${remote.bucket}`], remote, signal);
        await this.runDvc(['remote', 'modify', remote.name, 'endpointurl', remote.endpointUrl], remote, signal);
        await this.runDvc(['remote', 'modify', remote.name, 'use_ssl', String(remote.useSsl)], remote, signal);
    }
    async readDvcHash(dvcPath) {
        const pointer = await (0, promises_1.readFile)((0, node_path_1.resolve)(this.repositoryRoot, dvcPath), 'utf8');
        const hash = parseDvcHash(pointer);
        if (!hash)
            throw new common_1.ServiceUnavailableException(`Unable to read DVC hash from ${dvcPath}`);
        return hash;
    }
    remoteConfig() {
        const useSsl = this.config.getOrThrow('DVC_REMOTE_USE_SSL') === 'true';
        const endpoint = this.config.getOrThrow('DVC_REMOTE_ENDPOINT');
        const port = this.config.getOrThrow('DVC_REMOTE_PORT');
        return {
            name: this.config.getOrThrow('DVC_REMOTE_NAME'), bucket: this.config.getOrThrow('DVC_REMOTE_BUCKET'),
            endpointUrl: `${useSsl ? 'https' : 'http'}://${endpoint}:${port}`, useSsl,
            accessKey: this.config.getOrThrow('DVC_REMOTE_ACCESS_KEY'), secretKey: this.config.getOrThrow('DVC_REMOTE_SECRET_KEY'),
            gitAuthorName: this.config.getOrThrow('DATASET_PUBLISH_GIT_AUTHOR_NAME'), gitAuthorEmail: this.config.getOrThrow('DATASET_PUBLISH_GIT_AUTHOR_EMAIL'),
        };
    }
    datasetTag(name, version) { return `dataset-${name.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '')}-v${version}.0.0`; }
    async runDvc(args, remote, signal) { return this.run(this.config.getOrThrow('DVC_COMMAND'), args, remote, false, this.repositoryRoot, signal); }
    async runGit(args, allowFailure = false, signal) { return this.run('git', args, undefined, allowFailure, this.repositoryRoot, signal); }
    dvcEnvironment() {
        const runtimeRoot = (0, node_path_1.resolve)(this.repositoryRoot, '.dvc', 'tmp', 'runtime');
        return {
            DVC_NO_ANALYTICS: this.config.get('DVC_NO_ANALYTICS') ?? 'true',
            DVC_SYSTEM_CONFIG_DIR: this.config.get('DVC_SYSTEM_CONFIG_DIR') ?? (0, node_path_1.resolve)(runtimeRoot, 'system-config'),
            DVC_GLOBAL_CONFIG_DIR: this.config.get('DVC_GLOBAL_CONFIG_DIR') ?? (0, node_path_1.resolve)(runtimeRoot, 'global-config'),
            DVC_SITE_CACHE_DIR: this.config.get('DVC_SITE_CACHE_DIR') ?? (0, node_path_1.resolve)(runtimeRoot, 'site-cache'),
        };
    }
    async run(command, args, remote, allowFailure = false, cwd = this.repositoryRoot, signal) {
        signal?.throwIfAborted();
        try {
            const dvcEnvironment = command === this.config.get('DVC_COMMAND') ? this.dvcEnvironment() : {};
            const result = await execFileAsync(command, args, { cwd, signal, env: { ...process.env, ...dvcEnvironment, ...(remote && { AWS_ACCESS_KEY_ID: remote.accessKey, AWS_SECRET_ACCESS_KEY: remote.secretKey }) } });
            return { stdout: result.stdout, code: 0 };
        }
        catch (error) {
            signal?.throwIfAborted();
            const failed = error;
            if (allowFailure)
                return { stdout: failed.stdout ?? '', code: typeof failed.code === 'number' ? failed.code : 1 };
            throw new common_1.ServiceUnavailableException(`${command} ${args[0]} failed: ${failed.stderr ?? failed.message}`);
        }
    }
};
exports.DvcService = DvcService;
exports.DvcService = DvcService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [config_1.ConfigService])
], DvcService);
//# sourceMappingURL=dvc.service.js.map