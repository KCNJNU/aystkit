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
exports.ArchiveRecordingsController = void 0;
const common_1 = require("@nestjs/common");
const swagger_1 = require("@nestjs/swagger");
const client_1 = require("@prisma/client");
const class_validator_1 = require("class-validator");
const auth_decorators_1 = require("../auth/auth.decorators");
const permissions_1 = require("../auth/permissions");
const archive_recordings_service_1 = require("./archive-recordings.service");
const prepared_folder_packages_service_1 = require("./prepared-folder-packages.service");
class EndRecordingSessionDto {
    robotId;
    startedAt;
    endedAt;
    reason;
    captureIncomplete;
    segmentIds;
    recoveryEvidenceSha256;
}
__decorate([
    (0, class_validator_1.IsUUID)(),
    __metadata("design:type", String)
], EndRecordingSessionDto.prototype, "robotId", void 0);
__decorate([
    (0, class_validator_1.IsDateString)(),
    __metadata("design:type", String)
], EndRecordingSessionDto.prototype, "startedAt", void 0);
__decorate([
    (0, class_validator_1.IsDateString)(),
    __metadata("design:type", String)
], EndRecordingSessionDto.prototype, "endedAt", void 0);
__decorate([
    (0, class_validator_1.IsIn)(['desktop_stopped', 'recovered_interruption']),
    __metadata("design:type", String)
], EndRecordingSessionDto.prototype, "reason", void 0);
__decorate([
    (0, class_validator_1.IsBoolean)(),
    __metadata("design:type", Boolean)
], EndRecordingSessionDto.prototype, "captureIncomplete", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.IsArray)(),
    (0, class_validator_1.ArrayMaxSize)(5000),
    (0, class_validator_1.ArrayUnique)(),
    (0, class_validator_1.Matches)(/^[a-f0-9]{32}$/, { each: true }),
    __metadata("design:type", Array)
], EndRecordingSessionDto.prototype, "segmentIds", void 0);
__decorate([
    (0, class_validator_1.IsOptional)(),
    (0, class_validator_1.Matches)(/^[a-f0-9]{64}$/),
    __metadata("design:type", String)
], EndRecordingSessionDto.prototype, "recoveryEvidenceSha256", void 0);
let ArchiveRecordingsController = class ArchiveRecordingsController {
    recordings;
    prepared;
    constructor(recordings, prepared) {
        this.recordings = recordings;
        this.prepared = prepared;
    }
    captureSessions(id, robotId, user) {
        return this.recordings.captureSessions(id, robotId, user);
    }
    async captureSessionPackage(id, sessionId, robotId, user, background, retry, taskId, response) {
        if (background !== 'true' || retry !== undefined && retry !== 'true' && retry !== 'false'
            || taskId !== undefined && (retry !== undefined || typeof taskId !== 'string' || !/^[a-f0-9]{64}$/.test(taskId)))
            throw new common_1.BadRequestException('采集压缩包必须使用有效的后台任务参数');
        const result = await this.recordings.captureSessionPackage(id, sessionId, robotId, user, { background: true, retry: retry === 'true', ...(taskId ? { taskId } : {}) });
        const processing = 'state' in result && result.state === 'processing';
        response.status(processing ? 202 : 200);
        if (processing)
            response.setHeader('Retry-After', '2');
        return result;
    }
    preparedFolderPackages(id, user) {
        return this.prepared.list(id, user);
    }
    prepareFolderPackages(id, user) {
        return this.prepared.request(id, user);
    }
    async folderPackage(id, path, user, background, retry, taskId, response) {
        if ([background, retry].some(value => value !== undefined && value !== 'true' && value !== 'false'))
            throw new common_1.BadRequestException('压缩包请求参数无效');
        if ((retry !== undefined || taskId !== undefined) && background !== 'true'
            || taskId !== undefined && (retry !== undefined || typeof taskId !== 'string' || !/^[a-f0-9]{64}$/.test(taskId)))
            throw new common_1.BadRequestException('压缩包任务参数无效');
        const result = await this.recordings.folderPackage(id, path, user, { background: background === 'true', retry: retry === 'true', ...(taskId ? { taskId } : {}) });
        const processing = 'state' in result && result.state === 'processing';
        response.status(processing ? 202 : 200);
        if (processing)
            response.setHeader('Retry-After', '2');
        return result;
    }
    endSession(id, sessionId, body, user) {
        return this.recordings.endSession(id, sessionId, body, user);
    }
    list(id, path = '', user) {
        return this.recordings.list(id, path, user);
    }
    package(id, recordingId, user) {
        return this.recordings.package(id, recordingId, user);
    }
    packages(id, path = '', user) {
        return this.recordings.listPackages(id, path, user);
    }
    assemble(id, recordingId, user) {
        return this.recordings.requestAssembly(id, recordingId, user);
    }
};
exports.ArchiveRecordingsController = ArchiveRecordingsController;
__decorate([
    (0, common_1.Get)(':id/capture-sessions'),
    (0, swagger_1.ApiOperation)({ summary: 'List exact robot/capture-session memberships among uploaded files; no capture-end marker is required' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)('robotId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "captureSessions", null);
__decorate([
    (0, common_1.Get)(':id/capture-sessions/:sessionId/package'),
    (0, swagger_1.ApiOperation)({ summary: 'Download a verified uploaded snapshot across all sensors of one robot capture session' }),
    (0, swagger_1.ApiResponse)({ status: 202, description: 'Background generation is running; poll the same robot/session URL with taskId.' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Param)('sessionId')),
    __param(2, (0, common_1.Query)('robotId')),
    __param(3, (0, auth_decorators_1.CurrentUser)()),
    __param(4, (0, common_1.Query)('async')),
    __param(5, (0, common_1.Query)('retry')),
    __param(6, (0, common_1.Query)('taskId')),
    __param(7, (0, common_1.Res)({ passthrough: true })),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, String, Object, Object, Object, Object, Object]),
    __metadata("design:returntype", Promise)
], ArchiveRecordingsController.prototype, "captureSessionPackage", null);
__decorate([
    (0, common_1.Get)(':id/prepared-folder-packages'),
    (0, swagger_1.ApiOperation)({ summary: 'List persistent device-folder preparations and completed download snapshots' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "preparedFolderPackages", null);
__decorate([
    (0, common_1.Post)(':id/prepared-folder-packages'),
    (0, common_1.HttpCode)(202),
    (0, swagger_1.ApiOperation)({ summary: 'Persist preparation requests for every root device folder; processing continues after logout' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "prepareFolderPackages", null);
__decorate([
    (0, common_1.Get)(':id/folder-package'),
    (0, swagger_1.ApiOperation)({ summary: 'Download a timestamped, verified snapshot of a folder and all its uploaded descendants' }),
    (0, swagger_1.ApiResponse)({ status: 202, description: 'With async=true, generation is running; poll the same URL without retry=true.' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)('path')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __param(3, (0, common_1.Query)('async')),
    __param(4, (0, common_1.Query)('retry')),
    __param(5, (0, common_1.Query)('taskId')),
    __param(6, (0, common_1.Res)({ passthrough: true })),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object, Object, Object, Object, Object]),
    __metadata("design:returntype", Promise)
], ArchiveRecordingsController.prototype, "folderPackage", null);
__decorate([
    (0, common_1.Post)(':id/recording-sessions/:sessionId/end'),
    (0, common_1.HttpCode)(200),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: 'Persist an authenticated capture-session end or evidenced interruption proof without modifying source files' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Param)('sessionId')),
    __param(2, (0, common_1.Body)()),
    __param(3, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, EndRecordingSessionDto, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "endSession", null);
__decorate([
    (0, common_1.Get)(':id/recordings'),
    (0, swagger_1.ApiOperation)({ summary: 'List continuous native-camera recordings in a sensor directory' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)('path')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "list", null);
__decorate([
    (0, common_1.Get)(':id/recordings/:recordingId/package'),
    (0, swagger_1.ApiOperation)({ summary: 'Get a timestamped compressed package containing the verified recording' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Param)('recordingId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "package", null);
__decorate([
    (0, common_1.Get)(':id/archive-packages'),
    (0, swagger_1.ApiOperation)({ summary: 'List timestamped compact archive packages in a sensor directory' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Query)('path')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "packages", null);
__decorate([
    (0, common_1.Post)(':id/recordings/:recordingId/assemble'),
    (0, common_1.HttpCode)(202),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: 'Enqueue a verified, lossless continuous-recording export' }),
    __param(0, (0, common_1.Param)('id')),
    __param(1, (0, common_1.Param)('recordingId')),
    __param(2, (0, auth_decorators_1.CurrentUser)()),
    __metadata("design:type", Function),
    __metadata("design:paramtypes", [String, String, Object]),
    __metadata("design:returntype", void 0)
], ArchiveRecordingsController.prototype, "assemble", null);
exports.ArchiveRecordingsController = ArchiveRecordingsController = __decorate([
    (0, swagger_1.ApiTags)('datasets'),
    (0, permissions_1.DataReader)(),
    (0, common_1.Controller)('v1/datasets'),
    __metadata("design:paramtypes", [archive_recordings_service_1.ArchiveRecordingsService, prepared_folder_packages_service_1.PreparedFolderPackagesService])
], ArchiveRecordingsController);
//# sourceMappingURL=archive-recordings.controller.js.map