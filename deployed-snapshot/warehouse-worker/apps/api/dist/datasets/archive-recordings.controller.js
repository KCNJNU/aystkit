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
    constructor(recordings) {
        this.recordings = recordings;
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
    (0, common_1.Post)(':id/recording-sessions/:sessionId/end'),
    (0, common_1.HttpCode)(200),
    (0, auth_decorators_1.Roles)(client_1.UserRole.ADMIN, client_1.UserRole.SUPER_ADMIN, client_1.UserRole.SYSTEM_ADMIN, client_1.UserRole.TENANT_ADMIN, client_1.UserRole.PROJECT_ADMIN, client_1.UserRole.OPERATOR),
    (0, swagger_1.ApiOperation)({ summary: 'Persist an authenticated desktop capture-session end proof without modifying source files' }),
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
    __metadata("design:paramtypes", [archive_recordings_service_1.ArchiveRecordingsService])
], ArchiveRecordingsController);
