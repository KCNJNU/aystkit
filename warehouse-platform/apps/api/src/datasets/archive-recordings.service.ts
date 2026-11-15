import { BadRequestException, ConflictException, ForbiddenException, HttpException, Inject, Injectable, NotFoundException, ServiceUnavailableException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Dataset, JobStatus, JobType, Prisma, RobotFile, UserRole } from '@prisma/client';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, mkdtemp, open, readFile, rm, stat, statfs, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { AuthUser } from '../auth/auth.types';
import { JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { datasetFolder, validateEntryName, validateFolderPath } from '../storage/dataset-path';
import { OBJECT_STORAGE, ObjectStorage } from '../storage/object-storage';
import { inspectStoredObject } from '../storage/object-stream';
import { warehouseScope } from '../warehouse/warehouse-query.service';
import { ARCHIVE_ASSEMBLY_ROLE, ARCHIVE_ASSEMBLY_TYPE, ArchiveSource, RecordingGroup, groupArchiveRecordings, inputMetadata, isOriginalArchiveVideo, jsonRecord, sourceFingerprint } from './archive-recording-groups';
import { ensureDatasetFolders } from './dataset-folders';
import { CAPTURE_ROBOT_ID, CAPTURE_SESSION_ID, CaptureSessionScope, groupCaptureSessions } from './capture-session-groups';

export class ArchiveAssemblyCancelledError extends Error {}
export class ArchiveAssemblyBusyError extends Error {}
export type PreparedFolderSnapshot = { schemaVersion: 1; datasetId: string; path: string; datasetName: string;
  at: string; files: Array<{ id: string; hash: string }>; folders: string[];
  objectKey: string; taskId: string; fileName: string };
type FolderPackageSnapshot = { dataset: Dataset; files: RobotFile[]; folders: Array<{ path: string }>; at: string; captureSession?: CaptureSessionScope };
type FolderPackageControl = { check: (force?: boolean) => Promise<void>; signal: AbortSignal };
const MAX_SENSOR_PARTS = 5_000;
const ASSEMBLY_GROUP_CHECK_MS = 5_000;
const STAGE_BYTES = 8 * 1024 * 1024;
const PACKAGE_IDLE_TIMEOUT_MS = 120_000;
const PACKAGE_TOTAL_TIMEOUT_MS = 30 * 60_000;
const FOLDER_SOURCE_IDLE_TIMEOUT_MS = 60_000;
const MAX_FOLDER_ENTRIES = 10_000;
const MAX_FOLDER_PACKAGE_TASKS = 32;
const FOLDER_PACKAGE_TASK_TTL_MS = 15 * 60_000;
const MAX_PACKAGE_WAITERS = 8;
const PACKAGE_QUEUE_TIMEOUT_MS = 30 * 60_000;
type PackageWaiter = { expiresAt: number; resolve: () => void; reject: (error: Error) => void;
  timer?: ReturnType<typeof setTimeout>; signal?: AbortSignal; abort?: () => void };
type FolderPackageTask = { state: 'processing' | 'ready' | 'failed'; updatedAt: number; error?: HttpException;
  id: string; datasetId: string; datasetName: string; path: string; objectKey: string; fileName: string;
  files: Map<string, string>; folders: Set<string>; scopeKey: string };
type FolderPackageResponse = { url: string; fileName: string; state?: undefined; taskId?: undefined }
  | { state: 'processing'; taskId: string; retryAfterMs: number }
  | { state: 'ready'; taskId: string; url: string; fileName: string };
type JobContext = { id: string; payload: Prisma.JsonValue };
type SessionEnd = { robotId: string; startedAt: string; endedAt: string; reason: 'desktop_stopped' | 'recovered_interruption'; captureIncomplete: boolean; segmentIds?: string[]; recoveryEvidenceSha256?: string };
type HelperResult = { schemaVersion: number; sha256: string; size: string; frameCount: number; durationSeconds: number; codec: string; width: number; height: number; sourceCount: number; indexSha256: string; losslessRemux: boolean; packetSha256Validated: boolean; timecodeScaleNs: number; attachments: Array<{name: string; sha256: string; bytes: number}>; gaps: unknown[] };

@Injectable()
export class ArchiveRecordingsService {
  private packaging = false;
  private readonly packageWaiters: PackageWaiter[] = [];
  private readonly folderPackageTasks = new Map<string, FolderPackageTask>();
  private nextScanAt = 0;
  private backfillCursor: string | undefined;
  private backfillDone = false;
  private liveCursor = { createdAt: new Date(Date.now() - 300_000), id: '00000000-0000-0000-0000-000000000000' };
  private readonly pendingSensors = new Map<string, {datasetId: string; path: string; retryAt: number}>();
  private closureCursor = { updatedAt: new Date(Date.now() - 300_000), id: '00000000-0000-0000-0000-000000000000' };

  constructor(private readonly prisma: PrismaService, private readonly jobs: JobsService,
    @Inject(OBJECT_STORAGE) private readonly storage: ObjectStorage, private readonly config: ConfigService) {}

  async endSession(datasetId: string, sessionId: string, body: SessionEnd, user: AuthUser) {
    const writers: UserRole[] = [UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR];
    if (!writers.includes(user.role)) throw new ForbiddenException('当前账号无权提交采集结束证明');
    const start = Date.parse(body.startedAt), end = Date.parse(body.endedAt);
    const segmentIds = Array.isArray(body.segmentIds) ? [...body.segmentIds].sort() : undefined;
    const recovered = body.reason === 'recovered_interruption';
    if (!/^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.test(sessionId)
        || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(body.robotId)
        || !Number.isFinite(start) || !Number.isFinite(end) || end < start || end - start > 24 * 3600_000
        || end > Date.now() + 300_000 || (!recovered && body.reason !== 'desktop_stopped') || typeof body.captureIncomplete !== 'boolean'
        || (recovered ? body.captureIncomplete !== true || !segmentIds || typeof body.recoveryEvidenceSha256 !== 'string'
          || !/^[a-f0-9]{64}$/.test(body.recoveryEvidenceSha256) : body.recoveryEvidenceSha256 !== undefined)
        || (body.segmentIds !== undefined && !Array.isArray(body.segmentIds))
        || (segmentIds && (segmentIds.length === 0 || segmentIds.length > 5000 || new Set(segmentIds).size !== segmentIds.length
          || segmentIds.some(id => typeof id !== 'string' || !/^[a-f0-9]{32}$/.test(id)))))
      throw new BadRequestException('采集结束证明参数无效');
    const proof = { robotId: body.robotId, startedAt: new Date(start).toISOString(), endedAt: new Date(end).toISOString(),
      reason: body.reason, captureIncomplete: body.captureIncomplete, ...(segmentIds ? { segmentIds } : {}),
      ...(recovered ? { recoveryEvidenceSha256: body.recoveryEvidenceSha256 } : {}) };
    return this.prisma.$transaction(async tx => {
      await tx.$queryRaw(Prisma.sql`SELECT id FROM "Dataset" WHERE id = ${datasetId}::uuid FOR UPDATE`);
      const dataset = await tx.dataset.findFirst({ where: { id: datasetId, deletingAt: null, ...warehouseScope(user) } });
      if (!dataset) throw new NotFoundException('数据集不存在');
      const metadata = jsonRecord(dataset.metadata);
      if (metadata.type !== 'garage-native-archive' || metadata.robotId !== body.robotId)
        throw new ConflictException('采集机器人与数据集不匹配');
      if (typeof metadata.day === 'string') {
        const day = Date.parse(metadata.day + 'T00:00:00+08:00');
        if (!Number.isFinite(day) || end < day || start >= day + 24 * 3600_000)
          throw new ConflictException('采集结束证明不在数据集日期范围内');
      }
      const sessions = jsonRecord(metadata.archiveRecordingSessions);
      if (Object.hasOwn(sessions, sessionId)) {
        const ordered = (value: unknown) => JSON.stringify(Object.entries(jsonRecord(value)).sort(([a], [b]) => a.localeCompare(b)));
        if (ordered(sessions[sessionId]) !== ordered(proof)) throw new ConflictException('采集会话已有不同结束证明');
        return { sessionId, closed: true, captureIncomplete: proof.captureIncomplete, idempotent: true };
      }
      if (Object.keys(sessions).length >= 256) throw new ConflictException('采集结束证明数量超限');
      if (segmentIds) {
        const rows = await tx.robotFile.findMany({ where: { datasetId, robotId: body.robotId,
          recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' },
          capturedAt: { gte: new Date(start), lte: new Date(end) } }, take: MAX_SENSOR_PARTS + 1 });
        if (rows.length > MAX_SENSOR_PARTS) throw new ConflictException('采集片段过多，请分批核对');
        const found = new Set<string>();
        for (const row of rows) {
          const meta = jsonRecord(row.metadata), id = meta.segmentId;
          if (typeof id !== 'string' || !segmentIds.includes(id)) continue;
          const begin = Date.parse(String(meta.startedAt)), finish = Date.parse(String(meta.endAt));
          const legacyToken = meta.captureSessionId === undefined || meta.captureSessionId === null || meta.captureSessionId === '';
          if (row.robotId !== body.robotId || !isOriginalArchiveVideo(row) || !Number.isFinite(begin) || !Number.isFinite(finish)
              || finish < begin || begin < start || finish > end || (!legacyToken && meta.captureSessionId !== sessionId))
            throw new ConflictException('采集片段与结束证明的机器人、会话或时间范围不匹配');
          found.add(id);
        }
        if (found.size !== segmentIds.length) throw new ConflictException('结束证明包含缺失或不属于本次采集的片段');
      }
      await tx.dataset.update({ where: { id: datasetId }, data: { metadata: inputMetadata({ ...metadata,
        archiveRecordingSessions: { ...sessions, [sessionId]: proof } }), updatedAt: new Date() } });
      return { sessionId, closed: true, captureIncomplete: proof.captureIncomplete, idempotent: false };
    });
  }

  async list(datasetId: string, path: string, user: AuthUser) {
    const dataset = await this.dataset(datasetId, user);
    const normalized = this.sensorPath(path);
    if (!normalized || jsonRecord(dataset.metadata).type !== 'garage-native-archive') return { datasetId, path: normalized, recordings: [] };
    const groups = await this.groups(datasetId, normalized);
    const ids = groups.map(group => group.id);
    const [exports, jobs] = await Promise.all([
      this.prisma.robotFile.findMany({ where: { datasetId, recognitionType: ARCHIVE_ASSEMBLY_TYPE, folderPath: `${normalized}/连续录像` } }),
      this.prisma.job.findMany({ where: { type: JobType.ARCHIVE_VIDEO_ASSEMBLE, resourceType: 'ARCHIVE_RECORDING', resourceId: { in: ids } }, orderBy: { createdAt: 'desc' } }),
    ]);
    return { datasetId, path: normalized, recordings: groups.map(group => {
      const file = group.closed && !group.error ? exports.find(item => jsonRecord(item.metadata).recordingId === group.id) ?? (group.partCount === 1 ? group.sources[0] : null) : null;
      // RobotFile rows are committed after upload finalization. A saved MKV can
      // be downloaded independently of whether later rotation parts will arrive;
      // never inspect upload staging objects or change session completeness here.
      const downloadableVideos = !file && !group.error ? group.sources.filter(source => source.datasetId === datasetId
        && source.robotId === group.robotId && source.fileSize > 0n && isOriginalArchiveVideo(source))
        .slice(0, MAX_SENSOR_PARTS).map(source => {
          const metadata = jsonRecord(source.metadata);
          return { id: source.id, originalName: source.originalName, fileSize: source.fileSize.toString(),
            startedAt: String(metadata.startedAt), endAt: String(metadata.endAt) };
        }) : [];
      const job = jobs.find(item => item.resourceId === group.id);
      // Stored rotation segments do not prove the recorder is still running.
      // Keep their completeness undecided until an explicit terminal segment arrives.
      const state = group.error ? 'failed' : !group.closed ? 'waiting_end' : file ? 'ready' : job?.status === JobStatus.RUNNING ? 'processing' : job?.status === JobStatus.FAILED || job?.status === JobStatus.CANCELLED || job?.status === JobStatus.SUCCEEDED ? 'failed' : 'pending';
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- Omit private source records from the public response.
      const { sources: _sources, error, ...publicGroup } = group;
      return { ...publicGroup, state, file: file ? inputMetadata(file) : null, downloadableVideos, jobId: job?.id ?? null,
        error: error ?? (state === 'failed' ? job?.status === JobStatus.SUCCEEDED ? '完整录像已删除，可重新生成' : job?.status === JobStatus.CANCELLED ? '完整录像生成已取消，可重新生成' : job?.errorMessage ?? '完整录像生成失败' : null) };
    }) };
  }

  /** List sealed sensor segments whose downloadable members are already compact derivatives. */
  async listPackages(datasetId: string, path: string, user: AuthUser) {
    const dataset = await this.dataset(datasetId, user);
    const normalized = this.sensorPath(path);
    if (!normalized || jsonRecord(dataset.metadata).type !== 'garage-native-archive') return { datasetId, path: normalized, packages: [] };
    const groups = await this.compactPackageGroups(datasetId, normalized);
    return { datasetId, path: normalized, packages: groups.map(group => ({
      id: group.id, sensor: normalized, startedAt: group.startedAt, endAt: group.endAt,
      bytes: group.sources.reduce((sum, source) => sum + source.fileSize, 0n).toString(),
      fileCount: group.sources.length, state: 'ready' as const,
    })) };
  }

  /** An uploaded snapshot, independent of capture-end markers and device clocks. */
  async captureSessions(datasetId: string, robotId: string | undefined, user: AuthUser) {
    if (typeof robotId === 'string') robotId = robotId.toLowerCase();
    const inventory = await this.captureSessionInventory(datasetId, robotId, user);
    return { datasetId, robotId: robotId ?? null, snapshotAt: inventory.at,
      sessions: inventory.sessions.map(({ files, sensors, ...session }) => ({ ...session,
        fileCount: files.length, totalBytes: files.reduce((sum, file) => sum + file.fileSize, 0n).toString(),
        sensorCount: sensors.length, sensors, snapshot: true as const })),
      unassigned: { fileCount: inventory.unassigned.length,
        totalBytes: inventory.unassigned.reduce((sum, file) => sum + file.fileSize, 0n).toString() },
      excludedDerivedFileCount: inventory.excludedDerivedFileCount };
  }

  async captureSessionPackage(datasetId: string, sessionId: string, robotId: string, user: AuthUser,
    options: { background?: boolean; retry?: boolean; taskId?: string } = {}) {
    if (typeof sessionId !== 'string' || !CAPTURE_SESSION_ID.test(sessionId)
        || typeof robotId !== 'string' || !CAPTURE_ROBOT_ID.test(robotId))
      throw new BadRequestException('采集会话或机器人标识无效');
    robotId = robotId.toLowerCase();
    return this.folderPackage(datasetId, '', user, { ...options, captureSession: { robotId, sessionId } });
  }

  private async captureSessionInventory(datasetId: string, robotId: string | undefined, user: AuthUser) {
    if (robotId !== undefined && (typeof robotId !== 'string' || !CAPTURE_ROBOT_ID.test(robotId)))
      throw new BadRequestException('机器人标识无效');
    const dataset = await this.dataset(datasetId, user);
    const files = await this.prisma.robotFile.findMany({ where: { datasetId, ...(robotId ? { robotId } : {}) },
      orderBy: { id: 'asc' }, take: MAX_FOLDER_ENTRIES + 1 });
    if (files.length > MAX_FOLDER_ENTRIES) throw new BadRequestException('采集记录条目超过单次查询上限，请按机器人查看或分批下载');
    if (files.some(file => file.datasetId !== datasetId || robotId && file.robotId !== robotId))
      throw new ConflictException('采集文件范围无效');
    return { dataset, at: new Date().toISOString(),
      ...groupCaptureSessions(files, jsonRecord(dataset.metadata).archiveRecordingSessions) };
  }

  private async captureSessionSnapshot(datasetId: string, scope: CaptureSessionScope, user: AuthUser): Promise<FolderPackageSnapshot> {
    const inventory = await this.captureSessionInventory(datasetId, scope.robotId, user);
    const session = inventory.sessions.find(item => item.robotId === scope.robotId && item.sessionId === scope.sessionId);
    if (!session) throw new NotFoundException('未找到本次采集的已入仓文件');
    return { dataset: inventory.dataset, at: inventory.at, files: session.files, folders: [], captureSession: scope };
  }

  private packageScopeKey(scope?: CaptureSessionScope) {
    return scope ? `capture-session:${scope.robotId}:${scope.sessionId}` : '';
  }

  /** Snapshot the uploaded files in a directory, including its descendants.
   * Generated download objects and unfinished uploads are not RobotFiles.
   */
  async capturePreparedFolderSnapshot(datasetId: string, path: string, user: AuthUser): Promise<PreparedFolderSnapshot> {
    const normalized = validateFolderPath(path);
    const snapshot = await this.folderPackageSnapshot(datasetId, normalized, user);
    const descriptor = this.folderPackageDescriptor(datasetId, normalized, snapshot);
    return { schemaVersion: 1, datasetId, path: normalized, datasetName: snapshot.dataset.name, at: snapshot.at,
      files: snapshot.files.map(file => ({ id: file.id, hash: this.folderPackageFileHash(file) })),
      folders: snapshot.folders.map(folder => folder.path), objectKey: descriptor.objectKey,
      taskId: descriptor.taskId, fileName: descriptor.fileName };
  }

  async validatePreparedFolderPackage(datasetId: string, path: string, user: AuthUser, saved: Prisma.JsonValue) {
    const snapshot = await this.restorePreparedFolderSnapshot(datasetId, validateFolderPath(path), user, saved);
    const descriptor = this.folderPackageDescriptor(datasetId, path, snapshot);
    if (!await this.storage.exists(descriptor.objectKey)) throw new ConflictException('已准备压缩包已失效，请重新准备');
  }

  async runPreparedFolderPackage(datasetId: string, path: string, user: AuthUser, saved: Prisma.JsonValue,
    control: FolderPackageControl): Promise<void> {
    await this.folderPackage(datasetId, path, user, { preparedSnapshot: saved, control, wait: true });
  }

  async folderPackage(datasetId: string, path: string, user: AuthUser,
    options: { background?: boolean; retry?: boolean; taskId?: string; preparedSnapshot?: Prisma.JsonValue;
      control?: FolderPackageControl; wait?: boolean; captureSession?: CaptureSessionScope } = {}): Promise<FolderPackageResponse> {
    if (options.taskId && (!options.background || options.retry || !/^[a-f0-9]{64}$/.test(options.taskId)))
      throw new BadRequestException('压缩包任务参数无效');
    const normalized = validateFolderPath(path);
    const scopeKey = this.packageScopeKey(options.captureSession);
    const freshSnapshot = () => options.captureSession
      ? this.captureSessionSnapshot(datasetId, options.captureSession, user)
      : this.folderPackageSnapshot(datasetId, normalized, user);
    const check = async (force = true) => {
      if (options.control?.signal.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
      await options.control?.check(force);
      if (options.control?.signal.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
    };
    await check();
    const now = Date.now();
    for (const [key, task] of this.folderPackageTasks) {
      if (task.state !== 'processing' && now - task.updatedAt > FOLDER_PACKAGE_TASK_TTL_MS) this.folderPackageTasks.delete(key);
    }
    let requestedTask: FolderPackageTask | undefined;
    if (options.taskId) {
      requestedTask = [...this.folderPackageTasks.values()].find(item => item.id === options.taskId
        && item.datasetId === datasetId && item.path === normalized && item.scopeKey === scopeKey);
      if (requestedTask) {
        const dataset = await this.dataset(datasetId, user);
        if (options.captureSession) await freshSnapshot();
        if (dataset.name !== requestedTask.datasetName)
          throw new ConflictException('文件夹内容已被删除、移动或修改，请刷新后重试');
        if (normalized && !await this.prisma.datasetFolder.findUnique({ where: { datasetId_path: { datasetId, path: normalized } } }))
          throw new NotFoundException('文件夹不存在');
        // A poll cannot publish or download bytes. Reauthorize it cheaply;
        // generation fences and every ready response keep the full inventory.
        if (requestedTask.state === 'processing') return { state: 'processing', retryAfterMs: 2000, taskId: requestedTask.id };
      }
      // The process-local task map expires. A durable READY record retains its
      // original membership even after more files have arrived during the day.
      const prepared = !options.captureSession && await this.prisma.preparedFolderPackage?.findFirst({ where: {
        datasetId, path: normalized, taskId: options.taskId, state: 'READY' } });
      if (prepared) {
        const snapshot = await this.restorePreparedFolderSnapshot(datasetId, normalized, user, prepared.snapshot);
        const descriptor = this.folderPackageDescriptor(datasetId, normalized, snapshot);
        if (descriptor.objectKey !== prepared.objectKey || descriptor.taskId !== prepared.taskId
            || descriptor.fileName !== prepared.fileName || !await this.storage.exists(descriptor.objectKey))
          throw new ConflictException('已准备压缩包已失效，请重新准备');
        return { state: 'ready', taskId: descriptor.taskId, fileName: descriptor.fileName,
          url: await this.storage.getPresignedDownloadUrl(descriptor.objectKey) };
      }
      if (requestedTask?.state === 'failed') throw requestedTask.error!;
    }
    const snapshot = options.preparedSnapshot
      ? await this.restorePreparedFolderSnapshot(datasetId, normalized, user, options.preparedSnapshot)
      : await freshSnapshot();
    const verifySnapshot = async () => {
      if (options.preparedSnapshot) await this.restorePreparedFolderSnapshot(datasetId, normalized, user, options.preparedSnapshot);
      else await this.verifyFolderPackageSnapshot(datasetId, normalized, user, snapshot);
    };
    if (requestedTask) return this.folderPackageTaskResult(requestedTask, snapshot, user);
    const { objectKey, rootName } = this.folderPackageDescriptor(datasetId, normalized, snapshot);
    if (options.taskId && createHash('sha256').update(objectKey).digest('hex') !== options.taskId)
      throw new ConflictException({ code: 'FOLDER_PACKAGE_EXPIRED', message: '压缩包任务已过期，请重新点击下载' });
    // A separate tree keeps user manifest.json files distinct from our manifest.
    const members = snapshot.files.map(file => {
      validateFolderPath(file.folderPath);
      const relative = normalized ? file.folderPath.slice(normalized.length).replace(/^\//, '') : file.folderPath;
      const leaf = validateEntryName(file.originalName);
      return { file, relativePath: [rootName, relative, leaf].filter(Boolean).join('/') };
    });
    const directories = new Set([rootName, ...snapshot.folders.map(folder => {
      validateFolderPath(folder.path);
      const relative = normalized ? folder.path.slice(normalized.length).replace(/^\//, '') : folder.path;
      return [rootName, relative].filter(Boolean).join('/');
    })]);
    const names = new Set<string>();
    for (const member of members) {
      if (names.has(member.relativePath)) throw new ConflictException('文件夹中存在同名文件，请重命名后下载压缩包');
      names.add(member.relativePath);
      const parts = member.relativePath.split('/');
      for (let index = 1; index < parts.length; index++) directories.add(parts.slice(0, index).join('/'));
      if (member.file.fileSize < 0n || member.file.fileSize > BigInt(Number.MAX_SAFE_INTEGER)
          || !/^[a-f0-9]{64}$/i.test(member.file.checksum)) throw new ConflictException('文件缺少有效大小或校验值，暂时无法打包');
    }
    if ([...directories].some(directory => names.has(directory)))
      throw new ConflictException('文件夹中存在文件和目录同名，请重命名后下载压缩包');
    const generate = async () => {
      let publishAttempted = false;
      try {
        if (!await this.storage.exists(objectKey)) await this.withPackageSlot(async () => {
          await check();
          // A queued request may acquire the slot much later. Reuse a completed
          // cache and revalidate the original snapshot before reading sources.
          if (await this.storage.exists(objectKey)) return;
          await verifySnapshot();
          await check();
          const cachePrefix = objectKey.slice(0, objectKey.lastIndexOf('/') + 1);
          await this.cleanupFolderPackageCache(cachePrefix, objectKey);
          const sourceBytes = snapshot.files.reduce((sum, file) => sum + file.fileSize, 0n);
          // Input, output and the newly published object may share the same disk.
          const scratch = await this.packageScratch(sourceBytes, 3n);
          try {
            const input = join(scratch, 'input');
            const tree = join(input, 'files');
            await mkdir(tree, { recursive: true, mode: 0o700 });
            for (const directory of directories) await mkdir(join(tree, directory), { recursive: true, mode: 0o700 });
            for (const { file, relativePath } of members) {
              await check(false);
              const target = await open(join(tree, relativePath), 'wx', 0o600);
              try {
                const result = await inspectStoredObject(this.storage, file.objectKey,
                  async chunk => { await check(false); await target.writeFile(chunk); },
                  { maxBytes: Number(file.fileSize), idleTimeoutMs: FOLDER_SOURCE_IDLE_TIMEOUT_MS, signal: options.control?.signal });
                if (result.size !== file.fileSize || result.checksum !== file.checksum.toLowerCase())
                  throw new ConflictException('文件内容与入仓校验值不符，压缩包未生成');
              } finally { await target.close(); }
            }
            await writeFile(join(input, 'manifest.json'), JSON.stringify(inputMetadata({ schemaVersion: 1,
              type: options.captureSession ? 'capture-session-download' : 'dataset-folder-download',
              ...(options.captureSession ? { robotId: options.captureSession.robotId, captureSessionId: options.captureSession.sessionId, uploadedSnapshot: true } : {}),
              datasetId, datasetName: snapshot.dataset.name, path: normalized,
              snapshotAt: snapshot.at, fileCount: members.length, totalBytes: sourceBytes.toString(),
              folders: [...directories], files: members.map(({ file, relativePath }) => ({
                id: file.id, path: `files/${relativePath}`, originalName: file.originalName, folderPath: file.folderPath,
                bytes: file.fileSize.toString(), sha256: file.checksum, robotId: file.robotId,
                capturedAt: file.capturedAt, recognitionType: file.recognitionType, schemaVersion: file.schemaVersion,
                mimeType: file.mimeType, fileType: file.fileType, metadata: file.metadata,
              })) }), null, 2) + '\n', { mode: 0o600 });
            const output = join(scratch, 'folder.tar.zst');
            await check();
            await this.createTarZstd(input, output, 1, options.control?.signal);
            await verifySnapshot();
            await check();
            publishAttempted = true;
            await this.publishPackage(objectKey, output, options.control?.signal);
            await check();
            await this.cleanupFolderPackageCache(cachePrefix, objectKey);
          } finally { await rm(scratch, { recursive: true, force: true }); }
        }, options.background === true || options.wait === true, options.control?.signal);
        // Reauthorize cached downloads and reject deletion/moves during generation.
        // New uploads belong to the next click rather than restarting this snapshot.
        await verifySnapshot();
        await check();
        return { url: await this.storage.getPresignedDownloadUrl(objectKey),
          fileName: `${rootName}_${this.packageTime(snapshot.at)}.tar.zst` };
      } catch (error) {
        // Deletion can begin after the pre-publication check. Its cleanup job
        // might already have inventoried the cache, so reclaim our new object.
        if (publishAttempted) {
          // A lost owner must not delete a successor's identical cache. Dataset
          // deletion has no successor, and its earlier inventory can miss us.
          const deleting = options.control?.signal.aborted && await this.prisma.dataset.findFirst({
            where: { id: datasetId, deletingAt: { not: null } }, select: { id: true },
          }).catch(() => null);
          if (!options.control?.signal.aborted || deleting) await this.storage.deleteObject(objectKey).catch(() => undefined);
        }
        throw error;
      }
    };
    if (!options.background) return generate();
    const previous = this.folderPackageTasks.get(objectKey);
    // Every poll above has already authorized the caller and rebuilt the exact
    // immutable content identity. No task token can bypass dataset permissions.
    if (previous?.state === 'processing') return this.folderPackageTaskResult(previous, snapshot, user);
    if (previous?.state === 'failed' && !options.retry) throw previous.error!;
    const cached = await this.storage.exists(objectKey);
    // A parallel first click can enter while the object lookup is awaiting I/O.
    // Reserve only after checking the map again, then do not await before set.
    const current = this.folderPackageTasks.get(objectKey);
    if (current?.state === 'processing') return this.folderPackageTaskResult(current, snapshot, user);
    if (current?.state === 'failed' && !options.retry) throw current.error!;
    // After an API restart only an already published, exact immutable snapshot
    // can recover an old task. Polling must never start a replacement build.
    if (options.taskId && !cached)
      throw new ConflictException({ code: 'FOLDER_PACKAGE_EXPIRED', message: '压缩包缓存已过期，请重新点击下载' });
    if (!current && this.folderPackageTasks.size >= MAX_FOLDER_PACKAGE_TASKS) {
      const terminal = [...this.folderPackageTasks].filter(([, task]) => task.state !== 'processing')
        .sort(([, left], [, right]) => left.updatedAt - right.updatedAt)[0];
      if (terminal) this.folderPackageTasks.delete(terminal[0]);
      else throw new ServiceUnavailableException('压缩包任务已满，请稍后重试');
    }
    const task: FolderPackageTask = { state: cached ? 'ready' : 'processing', updatedAt: now,
      id: createHash('sha256').update(objectKey).digest('hex'), datasetId, datasetName: snapshot.dataset.name, path: normalized, objectKey,
      fileName: `${rootName}_${this.packageTime(snapshot.at)}.tar.zst`,
      scopeKey, files: new Map(snapshot.files.map(file => [file.id, this.folderPackageFileHash(file)])),
      folders: new Set(snapshot.folders.map(folder => folder.path)) };
    this.folderPackageTasks.set(objectKey, task);
    if (cached) return this.folderPackageTaskResult(task, snapshot, user);
    // Both continuations consume the background promise. A failed upload or
    // compression is reported by the next poll, never as an unhandled rejection.
    void generate().then(() => {
      task.state = 'ready'; task.updatedAt = Date.now();
    }, error => {
      task.state = 'failed'; task.updatedAt = Date.now();
      task.error = error instanceof HttpException ? error : new ServiceUnavailableException({
        code: 'FOLDER_PACKAGE_FAILED', message: '文件夹压缩包生成失败，原始文件已保留，请重试',
      });
    });
    return { state: 'processing' as const, retryAfterMs: 2000, taskId: task.id };
  }

  private folderPackageDescriptor(datasetId: string, path: string,
    snapshot: Awaited<ReturnType<ArchiveRecordingsService['folderPackageSnapshot']>>) {
    const scopeKey = this.packageScopeKey(snapshot.captureSession);
    const folderHash = createHash('sha256').update(scopeKey || path).digest('hex');
    const digest = createHash('sha256').update(JSON.stringify({ datasetId, path, ...(scopeKey ? { scopeKey } : {}),
      files: snapshot.files.map(file => this.folderPackageFileIdentity(file)), folders: snapshot.folders.map(folder => folder.path) })).digest('hex');
    const objectKey = `${datasetFolder(snapshot.dataset.name)}.folder-downloads/${scopeKey ? 'capture-sessions/' : ''}${folderHash}/${digest}.tar.zst`;
    const rootName = validateEntryName(snapshot.captureSession
      ? `采集_${snapshot.captureSession.sessionId.slice(0, 12)}_${snapshot.captureSession.robotId.slice(0, 8)}`
      : path.split('/').at(-1) || snapshot.dataset.name);
    return { objectKey, rootName, taskId: createHash('sha256').update(objectKey).digest('hex'),
      fileName: `${rootName}_${this.packageTime(snapshot.at)}.tar.zst` };
  }

  private async restorePreparedFolderSnapshot(datasetId: string, path: string, user: AuthUser, value: Prisma.JsonValue) {
    const saved = jsonRecord(value);
    const invalid = () => new ConflictException('已准备压缩包快照已失效，请重新准备');
    if (saved.schemaVersion !== 1 || saved.datasetId !== datasetId || saved.path !== path
        || typeof saved.datasetName !== 'string' || typeof saved.at !== 'string' || !Number.isFinite(Date.parse(saved.at))
        || !Array.isArray(saved.files) || saved.files.length > MAX_FOLDER_ENTRIES
        || !Array.isArray(saved.folders) || saved.folders.length > MAX_FOLDER_ENTRIES
        || typeof saved.objectKey !== 'string' || typeof saved.taskId !== 'string'
        || !/^[a-f0-9]{64}$/.test(saved.taskId) || typeof saved.fileName !== 'string') throw invalid();
    const expected = new Map<string, string>();
    for (const member of saved.files) {
      const file = jsonRecord(member);
      if (typeof file.id !== 'string' || !file.id || typeof file.hash !== 'string'
          || !/^[a-f0-9]{64}$/.test(file.hash) || expected.has(file.id)) throw invalid();
      expected.set(file.id, file.hash);
    }
    const folders = new Set<string>();
    for (const folder of saved.folders) {
      if (typeof folder !== 'string' || folders.has(folder)) throw invalid();
      if (path && folder !== path && !folder.startsWith(path + '/')) throw invalid();
      validateFolderPath(folder); folders.add(folder);
    }
    const dataset = await this.dataset(datasetId, user);
    if (path && !await this.prisma.datasetFolder.findUnique({ where: { datasetId_path: { datasetId, path } } }))
      throw new NotFoundException('文件夹不存在');
    // Query the saved bounded membership, so later uploads do not require a
    // full new inventory or invalidate an old download at the 10,000-entry cap.
    const [files, selectedFolders] = await Promise.all([
      this.prisma.robotFile.findMany({ where: { datasetId, id: { in: [...expected.keys()] } }, orderBy: { id: 'asc' } }),
      this.prisma.datasetFolder.findMany({ where: { datasetId, path: { in: [...folders] } },
        select: { path: true }, orderBy: { path: 'asc' } }),
    ]);
    if (dataset.name !== saved.datasetName || files.length !== expected.size || selectedFolders.length !== folders.size
        || files.some(file => path && file.folderPath !== path && !file.folderPath.startsWith(path + '/')
          || expected.get(file.id) !== this.folderPackageFileHash(file))) throw invalid();
    const snapshot = { dataset, files, folders: selectedFolders, at: saved.at };
    const descriptor = this.folderPackageDescriptor(datasetId, path, snapshot);
    if (descriptor.objectKey !== saved.objectKey || descriptor.taskId !== saved.taskId || descriptor.fileName !== saved.fileName)
      throw invalid();
    return snapshot;
  }

  private folderPackageFileHash(file: RobotFile) {
    return createHash('sha256').update(this.folderPackageFileIdentity(file)).digest('hex');
  }

  private async folderPackageTaskResult(task: FolderPackageTask,
    snapshot: Awaited<ReturnType<ArchiveRecordingsService['folderPackageSnapshot']>>, user: AuthUser) {
    // Keep the established final reauthorization for cache hits and downloads.
    const current = task.state === 'ready' ? snapshot.captureSession
      ? await this.captureSessionSnapshot(task.datasetId, snapshot.captureSession, user)
      : await this.folderPackageSnapshot(task.datasetId, task.path, user) : snapshot;
    const files = new Map(current.files.map(file => [file.id, this.folderPackageFileHash(file)]));
    const folders = new Set(current.folders.map(folder => folder.path));
    if (current.dataset.name !== task.datasetName || [...task.files].some(([id, hash]) => files.get(id) !== hash)
        || [...task.folders].some(path => !folders.has(path)))
      throw new ConflictException('文件夹内容已被删除、移动或修改，请刷新后重试');
    if (task.state === 'failed') throw task.error!;
    if (task.state === 'processing') return { state: 'processing' as const, retryAfterMs: 2000, taskId: task.id };
    if (!await this.storage.exists(task.objectKey)) throw new ConflictException({
      code: 'FOLDER_PACKAGE_EXPIRED', message: '压缩包缓存已过期，请重新点击下载',
    });
    return { state: 'ready' as const, taskId: task.id,
      url: await this.storage.getPresignedDownloadUrl(task.objectKey), fileName: task.fileName };
  }

  private async cleanupFolderPackageCache(prefix: string, keepKey: string) {
    const entries: Array<{ objectKey: string; lastModified?: Date }> = [];
    const cursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await this.storage.listObjects({ prefix, cursor, limit: 1000 });
      for (const entry of page.items) {
        if (!entry.objectKey.startsWith(prefix) || !/^[a-f0-9]{64}\.tar\.zst$/.test(entry.objectKey.slice(prefix.length)))
          throw new ConflictException('下载缓存目录内容无效');
        entries.push(entry);
      }
      cursor = page.nextCursor;
      if (entries.length > MAX_FOLDER_ENTRIES || cursor && cursors.has(cursor))
        throw new ServiceUnavailableException('下载缓存清理尚未完成，请稍后重试');
      if (cursor) cursors.add(cursor);
    } while (cursor);
    // Keep the selected immutable snapshot plus one recent predecessor.
    // New revisions reclaim older/expired downloads instead of accumulating
    // another full copy every time uploads change this directory.
    const recent = entries.filter(entry => entry.objectKey !== keepKey
      && (entry.lastModified?.getTime() ?? 0) >= Date.now() - 3600_000)
      .sort((a, b) => (b.lastModified?.getTime() ?? 0) - (a.lastModified?.getTime() ?? 0))[0]?.objectKey;
    // PROCESSING also protects the short publish-to-READY interval and a cache
    // that has completed just before an API restart.
    const pinned = new Set((await this.prisma.preparedFolderPackage?.findMany({ where: { state: { in: ['QUEUED', 'PROCESSING', 'READY'] },
      objectKey: { startsWith: prefix } }, select: { objectKey: true } }) ?? []).map(item => item.objectKey));
    const obsolete = entries.filter(entry => entry.objectKey !== keepKey && entry.objectKey !== recent
      && !pinned.has(entry.objectKey)).map(entry => entry.objectKey);
    if (obsolete.length) await this.storage.deleteObjects(obsolete);
  }

  private async folderPackageSnapshot(datasetId: string, path: string, user: AuthUser): Promise<FolderPackageSnapshot> {
    const dataset = await this.dataset(datasetId, user);
    if (path && !await this.prisma.datasetFolder.findUnique({ where: { datasetId_path: { datasetId, path } } }))
      throw new NotFoundException('文件夹不存在');
    // Prisma's startsWith uses SQL LIKE, where '_' and '%' are wildcards.
    const descendantPrefix = path.replace(/[\\%_]/g, '\\$&') + '/';
    const [files, folders] = await Promise.all([
      this.prisma.robotFile.findMany({ where: { datasetId,
        ...(path ? { OR: [{ folderPath: path }, { folderPath: { startsWith: descendantPrefix } }] } : {}) },
        orderBy: { id: 'asc' }, take: MAX_FOLDER_ENTRIES + 1 }),
      this.prisma.datasetFolder.findMany({ where: { datasetId,
        ...(path ? { OR: [{ path }, { path: { startsWith: descendantPrefix } }] } : {}) },
        select: { path: true }, orderBy: { path: 'asc' }, take: MAX_FOLDER_ENTRIES + 1 }),
    ]);
    if (files.length > MAX_FOLDER_ENTRIES || folders.length > MAX_FOLDER_ENTRIES)
      throw new BadRequestException('文件夹条目超过单次打包上限，请进入子文件夹分别下载');
    // Validate again at the boundary; do not trust legacy database paths as fs paths.
    if (files.some(file => file.datasetId !== datasetId || path && file.folderPath !== path && !file.folderPath.startsWith(path + '/'))
        || folders.some(folder => path && folder.path !== path && !folder.path.startsWith(path + '/')))
      throw new ConflictException('文件夹内容范围无效');
    return { dataset, files, folders, at: new Date().toISOString() };
  }

  private async verifyFolderPackageSnapshot(datasetId: string, path: string, user: AuthUser,
    previous: Awaited<ReturnType<ArchiveRecordingsService['folderPackageSnapshot']>>) {
    const current = previous.captureSession
      ? await this.captureSessionSnapshot(datasetId, previous.captureSession, user)
      : await this.folderPackageSnapshot(datasetId, path, user);
    const files = new Map(current.files.map(file => [file.id, this.folderPackageFileIdentity(file)]));
    const folders = new Set(current.folders.map(folder => folder.path));
    if (current.dataset.name !== previous.dataset.name
        || previous.files.some(file => files.get(file.id) !== this.folderPackageFileIdentity(file))
        || previous.folders.some(folder => !folders.has(folder.path)))
      throw new ConflictException('文件夹内容已被删除、移动或修改，请刷新后重试');
  }

  private folderPackageFileIdentity(file: RobotFile) {
    // Keep directory snapshots independent from the established recording IDs.
    return JSON.stringify({ source: sourceFingerprint(file), originalName: file.originalName,
      capturedAt: file.capturedAt, mimeType: file.mimeType, fileType: file.fileType,
      recognitionType: file.recognitionType, schemaVersion: file.schemaVersion });
  }

  /** Build a compact, timestamped package containing only the verified video and a manifest.
   * Raw frame/ROS spool files are intentionally excluded from this download path.
   */
  async package(datasetId: string, recordingId: string, user: AuthUser) {
    if (!/^[a-f0-9]{32,64}$/.test(recordingId)) throw new BadRequestException('归档片段标识无效');
    const dataset = await this.dataset(datasetId, user);
    if (jsonRecord(dataset.metadata).type !== 'garage-native-archive') throw new NotFoundException('地库录像不存在');
    for (const path of await this.sensorPaths(datasetId)) {
      const group = (await this.groups(datasetId, path)).find(item => item.id === recordingId);
      if (!group) continue;
      if (!group.closed || group.error) throw new ConflictException('采集尚未封装完成，暂时无法下载压缩包');
      const exportFile = await this.prisma.robotFile.findFirst({ where: { datasetId, recognitionType: ARCHIVE_ASSEMBLY_TYPE, folderPath: `${path}/连续录像`, metadata: { path: ['recordingId'], equals: group.id } } });
      const source = exportFile ?? (group.partCount === 1 ? group.sources[0] : null);
      if (!source) throw new ConflictException('请先生成完整录像，再下载压缩包');
      const packageKey = `${datasetFolder(dataset.name)}${path}/连续录像/${group.id}/${group.id}.tar.zst`;
      if (!await this.storage.exists(packageKey)) await this.withPackageSlot(async () => {
        const scratch = await this.packageScratch(source.fileSize);
        try {
          const input = join(scratch, 'input');
          await mkdir(input, { mode: 0o700 });
          const videoPath = join(input, source.originalName.replace(/[^a-zA-Z0-9._-]/g, '_') || 'recording.mkv');
          const stream = await this.storage.getObject(source.objectKey);
          await pipeline(stream, createWriteStream(videoPath, { mode: 0o600, flags: 'wx' }));
          await writeFile(join(input, 'manifest.json'), JSON.stringify({ schemaVersion: 1, datasetId, sensor: path, recordingId: group.id, startedAt: group.startedAt, endAt: group.endAt, source: { originalName: source.originalName, checksum: source.checksum, bytes: source.fileSize.toString() } }, null, 2) + '\n', { mode: 0o600 });
          const packagePath = join(scratch, `${group.id}.tar.zst`);
          await this.createTarZstd(input, packagePath);
          await this.publishPackage(packageKey, packagePath);
        } finally { await rm(scratch, { recursive: true, force: true }); }
      });
      return { url: await this.storage.getPresignedDownloadUrl(packageKey), fileName: `${path}_${this.packageTime(group.startedAt)}_${this.packageTime(group.endAt)}.tar.zst` };
    }
    for (const path of await this.sensorPaths(datasetId)) {
      const group = (await this.compactPackageGroups(datasetId, path)).find(item => item.id === recordingId);
      if (!group) continue;
      const packageKey = `${datasetFolder(dataset.name)}${path}/连续数据包/${group.id}.tar.zst`;
      if (!await this.storage.exists(packageKey)) await this.withPackageSlot(() => this.createCompactPackage(packageKey, datasetId, path, group));
      return { url: await this.storage.getPresignedDownloadUrl(packageKey), fileName: `${path}_${this.packageTime(group.startedAt)}_${this.packageTime(group.endAt)}_数据包.tar.zst` };
    }
    throw new NotFoundException('录像片段已变化，请刷新后重试');
  }

  private async compactPackageGroups(datasetId: string, path: string) {
    const rows = await this.prisma.robotFile.findMany({
      where: { datasetId, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' } },
      orderBy: [{ capturedAt: 'asc' }, { id: 'asc' }], take: MAX_SENSOR_PARTS * 4,
    });
    const groups = new Map<string, { id: string; startedAt: string; endAt: string; sources: RobotFile[] }>();
    for (const row of rows) {
      const metadata = jsonRecord(row.metadata);
      const segmentId = typeof metadata.segmentId === 'string' ? metadata.segmentId : undefined;
      const relative = typeof metadata.relativePath === 'string' ? metadata.relativePath : row.originalName;
      const compact = /(?:\.mkv|\.zst|compressed-bag\/metadata\.yaml)$/i.test(relative);
      if (!segmentId || !/^[a-f0-9]{32,64}$/.test(segmentId) || !compact
          || typeof metadata.startedAt !== 'string' || typeof metadata.endAt !== 'string') continue;
      const group = groups.get(segmentId) ?? { id: segmentId, startedAt: metadata.startedAt, endAt: metadata.endAt, sources: [] };
      group.sources.push(row); groups.set(segmentId, group);
    }
    return [...groups.values()].filter(group => group.sources.length > 0);
  }

  private async createCompactPackage(packageKey: string, datasetId: string, sensor: string,
    group: { id: string; startedAt: string; endAt: string; sources: RobotFile[] }) {
    const scratch = await this.packageScratch(group.sources.reduce((sum, source) => sum + source.fileSize, 0n));
    try {
      const input = join(scratch, 'input');
      await mkdir(input, { mode: 0o700 });
      for (const source of group.sources) {
        const relative = jsonRecord(source.metadata).relativePath;
        const name = typeof relative === 'string' && relative.length ? relative : source.originalName;
        const safe = name.split('/').filter(part => part && part !== '.' && part !== '..').join('/') || 'archive.bin';
        const destination = join(input, safe);
        await mkdir(join(destination, '..'), { recursive: true, mode: 0o700 });
        await pipeline(await this.storage.getObject(source.objectKey), createWriteStream(destination, { mode: 0o600, flags: 'wx' }));
      }
      await writeFile(join(input, 'manifest.json'), JSON.stringify({ schemaVersion: 1, datasetId, sensor, recordingId: group.id, startedAt: group.startedAt, endAt: group.endAt, files: group.sources.map(source => ({ name: source.originalName, checksum: source.checksum, bytes: source.fileSize.toString() })) }, null, 2) + '\n', { mode: 0o600 });
      const output = join(scratch, `${group.id}.tar.zst`);
      await this.createTarZstd(input, output);
      await this.publishPackage(packageKey, output);
    } finally { await rm(scratch, { recursive: true, force: true }); }
  }

  private async withPackageSlot(work: () => Promise<void>, wait = false, signal?: AbortSignal) {
    if (signal?.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
    if (this.packaging) {
      if (!wait) throw new ServiceUnavailableException('已有压缩包正在生成，请稍后重试');
      if (this.packageWaiters.length >= MAX_PACKAGE_WAITERS)
        throw new ServiceUnavailableException({ code: 'FOLDER_PACKAGE_QUEUE_FULL', message: '压缩包等待队列已满，请稍后重试' });
      await new Promise<void>((resolve, reject) => {
        const waiter: PackageWaiter = { expiresAt: Date.now() + PACKAGE_QUEUE_TIMEOUT_MS, resolve, reject, signal };
        const remove = () => {
          const index = this.packageWaiters.indexOf(waiter);
          if (index < 0) return false;
          this.packageWaiters.splice(index, 1); clearTimeout(waiter.timer);
          if (waiter.abort) signal?.removeEventListener('abort', waiter.abort);
          return true;
        };
        waiter.abort = () => { if (remove()) reject(new ArchiveAssemblyCancelledError('Folder packaging cancelled')); };
        waiter.timer = setTimeout(() => {
          if (remove()) reject(this.packageQueueTimeout());
        }, PACKAGE_QUEUE_TIMEOUT_MS);
        waiter.timer.unref();
        this.packageWaiters.push(waiter);
        signal?.addEventListener('abort', waiter.abort, { once: true });
        if (signal?.aborted) waiter.abort();
      });
    } else this.packaging = true;
    try {
      if (signal?.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
      await work();
    }
    finally { this.releasePackageSlot(); }
  }

  private packageQueueTimeout() {
    return new ServiceUnavailableException({ code: 'FOLDER_PACKAGE_QUEUE_TIMEOUT', message: '压缩包排队等待超时，请稍后重试' });
  }

  private releasePackageSlot() {
    for (;;) {
      const next = this.packageWaiters.shift();
      if (!next) { this.packaging = false; return; }
      clearTimeout(next.timer);
      if (next.abort) next.signal?.removeEventListener('abort', next.abort);
      if (next.expiresAt <= Date.now()) { next.reject(this.packageQueueTimeout()); continue; }
      // Reserve the slot before waking the next owner. New arrivals cannot
      // overtake it, even before its promise continuation gets a turn.
      next.resolve();
      return;
    }
  }

  private async packageScratch(sourceBytes: bigint, copies = 2n) {
    const base = resolve(this.config.get<string>('ARCHIVE_VIDEO_WORK_DIR') ?? './data/archive-video-assembly');
    await mkdir(base, { recursive: true, mode: 0o700 });
    const disk = await statfs(base, { bigint: true });
    // Retain both the downloaded inputs and output, including tar/zstd overhead.
    if (disk.bavail * disk.bsize < sourceBytes * copies + 64n * 1024n * 1024n)
      throw new ServiceUnavailableException('压缩包临时磁盘空间不足，原始文件已保留');
    return mkdtemp(join(base, 'package-'));
  }

  private async publishPackage(objectKey: string, path: string, signal?: AbortSignal) {
    const info = await stat(path);
    if (!info.isFile() || !Number.isSafeInteger(info.size) || info.size <= 0) throw new ConflictException('压缩包文件无效');
    const content = createReadStream(path);
    const abort = () => { content.destroy(new ArchiveAssemblyCancelledError('Folder packaging cancelled')); };
    signal?.addEventListener('abort', abort, { once: true });
    try {
      if (signal?.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
      await this.storage.putObject({ objectKey, content, contentLength: info.size, contentType: 'application/zstd' });
    } finally { signal?.removeEventListener('abort', abort); content.destroy(); }
  }

  private packageTime(value: string) {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value)).replaceAll('-', '') + '_' + new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)).replaceAll(':', '');
  }

  private async createTarZstd(directory: string, output: string, compressionLevel: 1 | 3 = 3, signal?: AbortSignal) {
    if (signal?.aborted) throw new ArchiveAssemblyCancelledError('Folder packaging cancelled');
    await new Promise<void>((resolvePromise, reject) => {
      // Output is a sibling of the sealed input directory. Creating it inside
      // the input tree changes directory mtime even if tar excludes the file.
      const child = spawn('tar', ['-C', directory, '-cf', '-', '.'], { stdio: ['ignore', 'pipe', 'pipe'] });
      const compressor = spawn('zstd', ['-T1', ...(compressionLevel === 1 ? ['-1'] : []), '-q', '-o', output], { stdio: ['pipe', 'ignore', 'pipe'] });
      let stderr = '', failure: Error | undefined, closed = 0, pipeDone = false, settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined, totalTimer: ReturnType<typeof setTimeout> | undefined,
        progressTimer: ReturnType<typeof setInterval> | undefined, killTimer: ReturnType<typeof setTimeout> | undefined;
      let outputBytes = 0, checkingOutput = false;
      const done = () => {
        if (settled || closed !== 2 || !pipeDone) return;
        settled = true; clearTimeout(timer); clearTimeout(totalTimer); clearInterval(progressTimer); clearTimeout(killTimer);
        signal?.removeEventListener('abort', abort);
        if (failure) reject(failure); else resolvePromise();
      };
      const fail = (error: Error) => {
        if (!failure) {
          failure = error;
          killTimer = setTimeout(() => { child.kill('SIGKILL'); compressor.kill('SIGKILL'); }, 1000);
          killTimer.unref();
          child.kill('SIGTERM'); compressor.kill('SIGTERM');
        }
        done();
      };
      const abort = () => fail(new ArchiveAssemblyCancelledError('Folder packaging cancelled'));
      const progress = () => {
        if (failure || settled) return;
        clearTimeout(timer);
        timer = setTimeout(() => fail(new ServiceUnavailableException('压缩包生成超时，原始文件已保留，请稍后重试')), PACKAGE_IDLE_TIMEOUT_MS);
        timer.unref();
      };
      for (const [command, process] of [['tar', child], ['zstd', compressor]] as const) {
        process.stderr.on('data', (chunk: Buffer) => { stderr = (stderr + chunk.toString('utf8')).slice(-2000); });
        process.once('error', (error: NodeJS.ErrnoException) => fail(new ServiceUnavailableException({
          code: error.code === 'ENOENT' ? 'FOLDER_PACKAGE_COMMAND_MISSING' : 'FOLDER_PACKAGE_COMMAND_FAILED',
          message: error.code === 'ENOENT' ? `服务器缺少 ${command} 打包工具，请联系管理员` : '压缩包命令启动失败，请稍后重试',
        }, { cause: error })));
        process.once('close', code => {
          closed += 1;
          if (code !== 0) fail(new ServiceUnavailableException({ code: 'FOLDER_PACKAGE_COMPRESSION_FAILED',
            message: '压缩包生成失败，原始文件已保留，请重试',
          }, { cause: new Error(`${command}: ${stderr || `exit=${code}`}`) }));
          done();
        });
      }
      progress();
      totalTimer = setTimeout(() => fail(new ServiceUnavailableException('压缩包生成超过总时限，原始文件已保留，请稍后重试')), PACKAGE_TOTAL_TIMEOUT_MS);
      totalTimer.unref();
      // Input bytes renew the idle budget. During the final compressor flush,
      // output growth also counts; keep metadata polling single-flight.
      progressTimer = setInterval(() => {
        if (checkingOutput || failure || settled) return;
        checkingOutput = true;
        void stat(output).then(info => {
          if (info.size > outputBytes) { outputBytes = info.size; progress(); }
        }).catch((error: NodeJS.ErrnoException) => {
          if (error.code !== 'ENOENT' && !failure && !settled)
            fail(new ServiceUnavailableException('压缩包输出状态读取失败，原始文件已保留', { cause: error }));
        }).finally(() => { checkingOutput = false; });
      }, 1000);
      progressTimer.unref();
      const tracked = new Transform({ transform: (chunk: Buffer, _encoding, callback) => {
        if (chunk.length) progress();
        callback(null, chunk);
      } });
      void pipeline(child.stdout, tracked, compressor.stdin).then(() => { pipeDone = true; done(); }, error => { pipeDone = true; fail(error); });
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
    });
  }

  async requestAssembly(datasetId: string, recordingId: string, user: AuthUser) {
    if (!/^[a-f0-9]{64}$/.test(recordingId)) throw new BadRequestException('录像标识无效');
    const dataset = await this.dataset(datasetId, user);
    if (jsonRecord(dataset.metadata).type !== 'garage-native-archive') throw new NotFoundException('地库录像不存在');
    // Recording IDs are intentionally opaque; discover only within this authorized dataset.
    const sensors = await this.sensorPaths(datasetId);
    for (const path of sensors) {
      const group = (await this.groups(datasetId, path)).find(item => item.id === recordingId);
      if (group) return { jobId: (await this.enqueue(dataset, group, user))?.id ?? null };
    }
    throw new NotFoundException('录像片段已变化，请刷新后重试');
  }

  private async dataset(id: string, user?: AuthUser) {
    const value = await this.prisma.dataset.findFirst({ where: { id, deletingAt: null, ...(user ? warehouseScope(user) : {}) } });
    if (!value) throw new NotFoundException('数据集不存在');
    return value;
  }

  private sensorPath(path: string) {
    validateFolderPath(path);
    if (path.includes('/')) throw new BadRequestException('请在传感器目录查看完整录像');
    return path;
  }

  private async sensorPaths(datasetId: string) {
    const folders = await this.prisma.datasetFolder.findMany({ where: { datasetId, parentPath: '' }, select: { path: true }, take: 100 });
    return folders.map(folder => folder.path);
  }

  private async groups(datasetId: string, path: string) {
    const sources = await this.prisma.robotFile.findMany({ where: { datasetId, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' } }, take: MAX_SENSOR_PARTS + 1 });
    if (sources.length > MAX_SENSOR_PARTS) throw new ConflictException('该传感器单日片段过多，请联系管理员分批处理');
    const dataset = await this.prisma.dataset.findFirst({ where: { id: datasetId, deletingAt: null }, select: { metadata: true } });
    return groupArchiveRecordings(datasetId, path, sources, jsonRecord(dataset?.metadata).archiveRecordingSessions);
  }

  private async enqueue(dataset: Dataset, group: RecordingGroup, user?: AuthUser) {
    if (!group.closed) throw new ConflictException('采集尚未结束，请等待关闭后生成完整录像');
    if (group.error) throw new ConflictException(group.error);
    if (group.partCount === 1) return null;
    const key = `archive-video:${dataset.id}:${group.id}`;
    let existing = await this.prisma.job.findUnique({ where: { deduplicationKey: key } });
    if (existing) {
      if (user && existing.status === JobStatus.FAILED) return this.jobs.retry(existing.id, user);
      if (user && (existing.status === JobStatus.SUCCEEDED || existing.status === JobStatus.CANCELLED)) return this.resetMissingExport(existing.id, dataset.id, group.id);
      return existing;
    }
    try {
      return await this.jobs.create(JobType.ARCHIVE_VIDEO_ASSEMBLE, { payload: { datasetId: dataset.id, path: group.path, recordingId: group.id },
        resourceType: 'ARCHIVE_RECORDING', resourceId: group.id, deduplicationKey: key,
        requestedById: user?.id, tenantId: dataset.tenantId, projectId: dataset.projectId, maxAttempts: 3 });
    } catch (error) {
      if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === 'P2002') {
        existing = await this.prisma.job.findUnique({ where: { deduplicationKey: key } });
        if (existing) return existing;
      }
      throw error;
    }
  }

  private async resetMissingExport(jobId: string, datasetId: string, recordingId: string) {
    return this.prisma.$transaction(async tx => {
      await tx.$queryRaw(Prisma.sql`SELECT id FROM "Job" WHERE id = ${jobId}::uuid FOR UPDATE`);
      const current = await tx.job.findUniqueOrThrow({ where: { id: jobId } });
      if (current.status !== JobStatus.SUCCEEDED && current.status !== JobStatus.CANCELLED) return current;
      const file = await tx.robotFile.findFirst({ where: { datasetId, recognitionType: ARCHIVE_ASSEMBLY_TYPE, metadata: { path: ['recordingId'], equals: recordingId } } });
      if (file) return current;
      await tx.jobStep.updateMany({ where: { jobId }, data: { status: 'PENDING', progress: 0, errorMessage: null, startedAt: null, finishedAt: null } });
      return tx.job.update({ where: { id: jobId }, data: { status: JobStatus.QUEUED, progress: 0, attempts: 0, cancelRequested: false, result: Prisma.DbNull,
        startedAt: null, finishedAt: null, lockedAt: null, lockedBy: null, heartbeatAt: null, errorCode: null, errorMessage: null, availableAt: new Date() } });
    });
  }

  /** One bounded historical pass, then only newly inserted archive sources. No page visit is required. */
  async scanClosedRecordings(owner: string) {
    if (Date.now() < this.nextScanAt) return;
    this.nextScanAt = Date.now() + 5_000;
    if (!await this.jobs.acquireLease('archive-recording-scan', owner, 30_000)) return;
    try {
      const dirty = this.pendingSensors;
      const add = (datasetId: string, path: string) => {
        const key = `${datasetId}:${path}`;
        if (!dirty.has(key)) dirty.set(key, { datasetId, path, retryAt: 0 });
      };
      if (!this.backfillDone && dirty.size < 200) {
        const datasets = await this.prisma.dataset.findMany({ where: { deletingAt: null, metadata: { path: ['type'], equals: 'garage-native-archive' }, ...(this.backfillCursor ? { id: { gt: this.backfillCursor } } : {}) }, orderBy: { id: 'asc' }, take: 2 });
        for (const dataset of datasets) { for (const path of await this.sensorPaths(dataset.id)) add(dataset.id, path); this.backfillCursor = dataset.id; }
        if (datasets.length < 2) this.backfillDone = true;
      }
      // New recordings have a separate read budget; failing historical paths cannot block discovery.
      const rows = await this.prisma.robotFile.findMany({ where: { recognitionType: { startsWith: 'garage-native-' }, datasetId: { not: null }, OR: [{ createdAt: { gt: this.liveCursor.createdAt } }, { createdAt: this.liveCursor.createdAt, id: { gt: this.liveCursor.id } }] }, orderBy: [{ createdAt: 'asc' }, { id: 'asc' }], take: 50 });
      for (const row of rows) {
        this.liveCursor = { createdAt: row.createdAt, id: row.id };
        if (!isOriginalArchiveVideo(row) || !row.datasetId) continue;
        const path = row.folderPath.split('/')[0];
        if (!path) continue;
        add(row.datasetId, path);
      }
      // End proofs can arrive after every source upload. Discover their dataset
      // updates without modifying RobotFile or rescanning all historical files.
      const closedDatasets = await this.prisma.dataset.findMany({ where: { deletingAt: null,
        metadata: { path: ['type'], equals: 'garage-native-archive' }, OR: [{ updatedAt: { gt: this.closureCursor.updatedAt } },
          { updatedAt: this.closureCursor.updatedAt, id: { gt: this.closureCursor.id } }] },
        orderBy: [{ updatedAt: 'asc' }, { id: 'asc' }], take: 2 });
      for (const dataset of closedDatasets) {
        if (Object.keys(jsonRecord(jsonRecord(dataset.metadata).archiveRecordingSessions)).length)
          for (const path of await this.sensorPaths(dataset.id)) add(dataset.id, path);
        this.closureCursor = { updatedAt: dataset.updatedAt ?? new Date(), id: dataset.id };
      }
      let deferred: unknown;
      for (const [key, value] of [...dirty].filter(([, item]) => item.retryAt <= Date.now()).slice(0, 10)) {
        try {
          const dataset = await this.prisma.dataset.findFirst({ where: { id: value.datasetId, deletingAt: null } });
          if (dataset && jsonRecord(dataset.metadata).type === 'garage-native-archive') {
            for (const group of await this.groups(value.datasetId, value.path)) if (group.closed && !group.error && group.partCount > 1) await this.enqueue(dataset, group);
          }
          dirty.delete(key);
        } catch (error) {
          // Cursor advancement is safe only because failed paths remain in this retry queue.
          dirty.delete(key); dirty.set(key, { ...value, retryAt: Date.now() + 30_000 }); deferred = error;
        }
      }
      if (deferred) throw deferred;
    } finally { await this.jobs.releaseLease('archive-recording-scan', owner); }
  }

  async assemble(job: JobContext, workerId: string) {
    const payload = jsonRecord(job.payload);
    if (typeof payload.datasetId !== 'string' || typeof payload.path !== 'string' || typeof payload.recordingId !== 'string') throw new ConflictException('录像任务参数无效');
    const path = this.sensorPath(payload.path), dataset = await this.dataset(payload.datasetId);
    const group = (await this.groups(dataset.id, path)).find(item => item.id === payload.recordingId);
    if (!group || !group.closed || group.error) throw new ConflictException('原始录像已变化或尚未封装完成，请刷新后重试');
    let groupCheckedAt = Date.now();
    let groupCheck: Promise<void> | undefined;
    let obsoleteGroup: ArchiveAssemblyCancelledError | undefined;
    const existing = await this.prisma.robotFile.findFirst({ where: { datasetId: dataset.id, recognitionType: ARCHIVE_ASSEMBLY_TYPE, metadata: { path: ['recordingId'], equals: group.id } } });
    if (existing) return { fileId: existing.id, recordingId: group.id };
    if (group.partCount === 1) return { fileId: group.sources[0].id, recordingId: group.id };
    const lease = 'archive-video-assembly', owner = `${workerId}:${job.id}`;
    if (!await this.jobs.acquireLease(lease, owner)) throw new ArchiveAssemblyBusyError('录像合并资源繁忙，稍后自动重试');
    let leaseLost = false;
    const timer = setInterval(() => void this.jobs.heartbeatLease(lease, owner).then(result => { if (result.count !== 1) leaseLost = true; }).catch(() => { leaseLost = true; }), 10_000);
    const check = async () => {
      if (obsoleteGroup) throw obsoleteGroup;
      if (leaseLost) throw new ServiceUnavailableException('录像处理租约已失效');
      const ownership = await this.prisma.job.findUnique({ where: { id: job.id }, select: { lockedBy: true, status: true, cancelRequested: true } });
      if (!ownership || ownership.cancelRequested) throw new ArchiveAssemblyCancelledError();
      if (ownership.lockedBy !== workerId || ownership.status !== JobStatus.RUNNING) throw new ServiceUnavailableException('录像任务所有权已失效');
      if (obsoleteGroup) throw obsoleteGroup;
      if (!groupCheck && Date.now() - groupCheckedAt >= ASSEMBLY_GROUP_CHECK_MS) {
        // A late source changes the group fingerprint. Stop obsolete work now
        // instead of holding the global lease until the publication check.
        // Streaming and helper checks share this one throttled inventory.
        groupCheck = this.groups(dataset.id, path).then(groups => {
          const current = groups.find(item => item.id === group.id);
          if (!current || !current.closed || current.error) {
            obsoleteGroup = new ArchiveAssemblyCancelledError('原始录像分组已变化，已取消过时的生成任务；原始片段保留');
            throw obsoleteGroup;
          }
          groupCheckedAt = Date.now();
        }).finally(() => { groupCheck = undefined; });
      }
      if (groupCheck) await groupCheck;
    };
    const attemptId = randomUUID();
    let scratch: string | undefined, finalKey: string | undefined, published = false;
    const staged: string[] = [];
    try {
      await check();
      const base = resolve(this.config.get<string>('ARCHIVE_VIDEO_WORK_DIR') ?? './data/archive-video-assembly');
      await mkdir(base, { recursive: true, mode: 0o700 });
      const disk = await statfs(base, { bigint: true });
      const required = BigInt(group.sourceBytes) * 5n + 256n * 1024n * 1024n;
      if (disk.bavail * disk.bsize < required) throw new ServiceUnavailableException('录像处理临时磁盘空间不足，原片段已保留');
      scratch = await mkdtemp(join(base, 'assembly-'));
      const sources: Array<Record<string, unknown>> = [];
      const identities = new Map<string, Awaited<ReturnType<ObjectStorage['statObject']>>>();
      for (const source of group.sources) {
        await check();
        const local = join(scratch, `${source.id}.mkv`);
        identities.set(source.id, await this.downloadSource(source, local, check));
        const metadata = jsonRecord(source.metadata);
        sources.push({ id: source.id, path: local, checksum: source.checksum, size: source.fileSize.toString(), startedAt: metadata.startedAt, endAt: metadata.endAt, frameCount: metadata.receivedFrames,
          metadata: { videoAttachments: metadata.videoAttachments, videoTimecodeScaleNs: metadata.videoTimecodeScaleNs } });
      }
      const input = join(scratch, 'input.json'), output = join(scratch, 'video.mkv'), resultPath = join(scratch, 'result.json');
      await writeFile(input, JSON.stringify({ schemaVersion: 1, recordingId: group.id, robotId: group.robotId, sensor: group.sensor, startedAt: group.startedAt, endAt: group.endAt, sources }), { mode: 0o600 });
      await this.runHelper(input, output, resultPath, check);
      const result = this.validateResult(JSON.parse(await readFile(resultPath, 'utf8')), group);
      const outputInfo = await this.hashFile(output, check);
      if (outputInfo.size.toString() !== result.size || outputInfo.sha256 !== result.sha256) throw new ConflictException('完整录像输出校验失败');
      await check();
      // Attempt-specific keys fence a stale worker's cleanup from a new lease owner.
      finalKey = `${datasetFolder(dataset.name)}${path}/连续录像/${group.id}/${attemptId}/video.mkv`;
      const descriptor = await open(output, 'r');
      try {
        let part = 0;
        // Only one 8 MiB buffer is retained; the source/output size may exceed 2 GiB.
        for (;;) {
          await check();
          const buffer = Buffer.allocUnsafe(STAGE_BYTES), { bytesRead } = await descriptor.read(buffer, 0, buffer.length, null);
          if (!bytesRead) break;
          const key = `uploads/archive-assembly/${job.id}/${attemptId}/${++part}`; staged.push(key);
          await this.storage.putObject({ objectKey: key, content: buffer.subarray(0, bytesRead), contentType: 'application/octet-stream' });
        }
      } finally { await descriptor.close(); }
      if (!staged.length || staged.length > 10_000) throw new ConflictException('录像输出为空或超出对象存储分片数限制');
      await check(); await this.storage.composeObject(finalKey, staged, 'video/x-matroska');
      const remote = await this.hashStream(await this.storage.getObject(finalKey), check);
      if (remote.size.toString() !== result.size || remote.sha256 !== result.sha256) throw new ConflictException('完整录像存储回读校验失败');
      await check();
      for (const source of group.sources) {
        const current = await this.storage.statObject(source.objectKey);
        const identity = identities.get(source.id)!;
        if (BigInt(current.size) !== source.fileSize || (identity.etag && current.etag !== identity.etag)
          || (identity.lastModified && current.lastModified?.getTime() !== identity.lastModified.getTime())) throw new ConflictException('原始录像已删除或变化，录像不会发布');
      }
      const file = await this.prisma.$transaction(async tx => {
        await tx.$queryRaw(Prisma.sql`SELECT id FROM "Dataset" WHERE id = ${dataset.id}::uuid FOR UPDATE`);
        await tx.$queryRaw(Prisma.sql`SELECT id FROM "Job" WHERE id = ${job.id}::uuid FOR UPDATE`);
        const jobState = await tx.job.findUnique({ where: { id: job.id }, select: { lockedBy: true, status: true, cancelRequested: true } });
        if (!jobState || jobState.cancelRequested) throw new ArchiveAssemblyCancelledError();
        if (jobState.lockedBy !== workerId || jobState.status !== JobStatus.RUNNING) throw new ServiceUnavailableException('录像任务所有权已失效');
        const currentDataset = await tx.dataset.findUnique({ where: { id: dataset.id } });
        if (!currentDataset || currentDataset.deletingAt) throw new ConflictException('数据集已删除，录像不会发布');
        const fresh = await tx.robotFile.findMany({ where: { datasetId: dataset.id, folderPath: { startsWith: `${path}/` }, recognitionType: { startsWith: 'garage-native-' }, originalName: { endsWith: '.mkv', mode: 'insensitive' } }, take: MAX_SENSOR_PARTS + 1 });
        // New uploads take the same dataset lock. A late earlier/middle part must also change the fingerprint.
        if (fresh.length > MAX_SENSOR_PARTS || !groupArchiveRecordings(dataset.id, path, fresh, jsonRecord(currentDataset.metadata).archiveRecordingSessions).some(item => item.id === group.id && item.closed && !item.error)) throw new ConflictException('原始录像已变化，录像不会发布');
        await check();
        await ensureDatasetFolders(tx, this.storage, currentDataset, `${path}/连续录像`);
        const duplicate = await tx.robotFile.findUnique({ where: { objectKey: finalKey! } });
        if (duplicate) { if (duplicate.checksum !== result.sha256) throw new ConflictException('完整录像对象冲突'); return duplicate; }
        return tx.robotFile.create({ data: { datasetId: dataset.id, robotId: group.robotId, folderPath: `${path}/连续录像`, objectKey: finalKey!,
          originalName: this.videoName(group), fileType: 'mkv', mimeType: 'video/x-matroska', fileSize: BigInt(result.size), checksum: result.sha256,
          recognitionType: ARCHIVE_ASSEMBLY_TYPE, schemaVersion: '1.0', capturedAt: new Date(group.startedAt),
          metadata: inputMetadata({ archiveType: 'garage-native-archive', archiveRole: ARCHIVE_ASSEMBLY_ROLE, recordingId: group.id, sensor: group.sensor,
            startedAt: group.startedAt, endAt: group.endAt, sourceIds: group.sources.map(source => source.id), sourceChecksums: group.sources.map(source => source.checksum),
            partCount: group.partCount, sourceBytes: group.sourceBytes, ...result }) } });
      }, { timeout: 60_000 });
      published = true;
      return { fileId: file.id, recordingId: group.id, frameCount: result.frameCount, bytes: result.size };
    } finally {
      try {
        if (staged.length) await this.storage.deleteObjects(staged).catch(() => undefined);
        if (finalKey && !published) {
          const attached = await this.prisma.robotFile.findUnique({ where: { objectKey: finalKey }, select: { id: true } }).catch(() => ({ id: 'unknown' }));
          if (!attached) await this.storage.deleteObject(finalKey).catch(() => undefined);
        }
        if (scratch) await rm(scratch, { recursive: true, force: true });
      } finally {
        clearInterval(timer); await this.jobs.releaseLease(lease, owner).catch(() => undefined);
      }
    }
  }

  private videoName(group: RecordingGroup) {
    const time = (value: string) => new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value)).replaceAll(':', '-');
    return `${group.sensor}_${time(group.startedAt)}_${time(group.endAt)}_完整录像.mkv`;
  }

  private validateResult(value: unknown, group: RecordingGroup): HelperResult {
    const result = jsonRecord(value);
    if (result.schemaVersion !== 1 || result.losslessRemux !== true || result.packetSha256Validated !== true || result.timecodeScaleNs !== 1000
      || result.sourceCount !== group.partCount || result.frameCount !== group.frameCount || !/^[a-f0-9]{64}$/.test(String(result.sha256))
      || !/^[a-f0-9]{64}$/.test(String(result.indexSha256)) || !/^[1-9][0-9]*$/.test(String(result.size))
      || typeof result.durationSeconds !== 'number' || !Number.isFinite(result.durationSeconds) || result.durationSeconds <= 0
      || typeof result.codec !== 'string' || !['hevc', 'mjpeg', 'ffv1'].includes(result.codec)
      || !Number.isSafeInteger(result.width) || Number(result.width) <= 0 || !Number.isSafeInteger(result.height) || Number(result.height) <= 0
      || !Array.isArray(result.attachments) || result.attachments.filter(item => ['index.jsonl', 'frames.index.jsonl'].includes(String(jsonRecord(item).name)) && jsonRecord(item).sha256 === result.indexSha256).length !== 1
      || !Array.isArray(result.gaps)) throw new ConflictException('录像封装程序未通过完整性验证');
    return result as HelperResult;
  }

  private async downloadSource(source: ArchiveSource, destination: string, check: () => Promise<void>) {
    const object = await this.storage.statObject(source.objectKey);
    if (BigInt(object.size) !== source.fileSize) throw new ConflictException('原始录像大小已变化');
    const stream = await this.storage.getObject(source.objectKey);
    const guard = this.guardStream(stream, check);
    const hash = createHash('sha256'); let size = 0n, lastCheck = 0;
    const verify = new Transform({ transform: (chunk: Buffer, _encoding, callback) => {
      guard.touch();
      hash.update(chunk); size += BigInt(chunk.length);
      if (size > source.fileSize) return callback(new ConflictException('原始录像大小校验失败'));
      if (Date.now() - lastCheck > 1_000) { lastCheck = Date.now(); void check().then(() => callback(null, chunk), callback); } else callback(null, chunk);
    } });
    try { await pipeline(stream, verify, createWriteStream(destination, { mode: 0o600, flags: 'wx' })); }
    finally { guard.stop(); }
    if (size !== source.fileSize || hash.digest('hex') !== source.checksum) throw new ConflictException('原始录像 SHA-256 校验失败');
    return object;
  }

  private async hashStream(stream: NodeJS.ReadableStream, check: () => Promise<void>) {
    const guard = this.guardStream(stream, check);
    const hash = createHash('sha256'); let size = 0n, lastCheck = 0;
    try {
      for await (const item of stream as AsyncIterable<Buffer>) { guard.touch(); hash.update(item); size += BigInt(item.length); if (Date.now() - lastCheck > 1_000) { lastCheck = Date.now(); await check(); } }
    } catch (error) { (stream as { destroy?: () => void }).destroy?.(); throw error; }
    finally { guard.stop(); }
    return { sha256: hash.digest('hex'), size };
  }
  private hashFile(path: string, check: () => Promise<void>) { return this.hashStream(createReadStream(path), check); }

  private guardStream(stream: NodeJS.ReadableStream, check: () => Promise<void>) {
    let lastData = Date.now(), checking = false, stopped = false;
    const timer = setInterval(() => {
      if (checking || stopped) return;
      checking = true;
      void (async () => {
        await check();
        if (Date.now() - lastData > 60_000) throw new ServiceUnavailableException('对象存储传输超时，原始录像已保留');
      })().catch(error => { if (!stopped) (stream as Readable).destroy(error instanceof Error ? error : new Error('Stream aborted')); }).finally(() => { checking = false; });
    }, 1_000);
    return { touch: () => { lastData = Date.now(); }, stop: () => { stopped = true; clearInterval(timer); } };
  }

  protected async runHelper(input: string, output: string, result: string, check: () => Promise<void>) {
    const helper = resolve(__dirname, '../../scripts/assemble-archive-video.py');
    await stat(helper);
    const timeoutSeconds = Math.max(60, Math.min(86_400, Number(this.config.get<string>('ARCHIVE_VIDEO_TIMEOUT_SECONDS') ?? 1800)));
    await new Promise<void>((done, reject) => {
      const child = spawn('python3', [helper, '--input', input, '--output', output, '--result', result, '--timeout-seconds', String(timeoutSeconds)], { stdio: ['ignore', 'ignore', 'pipe'] });
      let failure: unknown, stderr = '', killing: NodeJS.Timeout | undefined, checking = false;
      const stop = (error: unknown) => { if (failure) return; failure = error; child.kill('SIGTERM'); killing = setTimeout(() => child.kill('SIGKILL'), 5_000); };
      child.stderr.on('data', (chunk: Buffer) => { if (stderr.length < 4000) stderr += chunk.toString('utf8').slice(0, 4000 - stderr.length); });
      const timer = setInterval(() => { if (checking) return; checking = true; void check().catch(stop).finally(() => { checking = false; }); }, 1_000);
      const deadline = setTimeout(() => stop(new ConflictException('录像封装超时，原始文件已保留')), (timeoutSeconds + 15) * 1_000);
      const cleanup = () => { clearInterval(timer); clearTimeout(deadline); if (killing) clearTimeout(killing); };
      child.once('error', error => { cleanup(); reject(error); });
      child.once('close', code => { cleanup(); if (failure) reject(failure); else if (code !== 0) reject(new ConflictException(`录像封装失败：${stderr.trim().slice(-2000) || `exit ${code}`}`)); else done(); });
    });
  }
}
