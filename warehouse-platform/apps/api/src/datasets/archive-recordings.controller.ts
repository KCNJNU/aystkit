import { BadRequestException, Body, Controller, Get, HttpCode, Param, Post, Query, Res } from '@nestjs/common';
import { ApiOperation, ApiResponse, ApiTags } from '@nestjs/swagger';
import type { Response } from 'express';
import { UserRole } from '@prisma/client';
import { ArrayMaxSize, ArrayUnique, IsArray, IsBoolean, IsDateString, IsIn, IsOptional, IsUUID, Matches } from 'class-validator';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { DataReader } from '../auth/permissions';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { PreparedFolderPackagesService } from './prepared-folder-packages.service';

class EndRecordingSessionDto {
  @IsUUID() robotId!: string;
  @IsDateString() startedAt!: string;
  @IsDateString() endedAt!: string;
  @IsIn(['desktop_stopped', 'recovered_interruption']) reason!: 'desktop_stopped' | 'recovered_interruption';
  @IsBoolean() captureIncomplete!: boolean;
  @IsOptional() @IsArray() @ArrayMaxSize(5000) @ArrayUnique() @Matches(/^[a-f0-9]{32}$/, { each: true }) segmentIds?: string[];
  @IsOptional() @Matches(/^[a-f0-9]{64}$/) recoveryEvidenceSha256?: string;
}

@ApiTags('datasets')
@DataReader()
@Controller('v1/datasets')
export class ArchiveRecordingsController {
  constructor(private readonly recordings: ArchiveRecordingsService, private readonly prepared: PreparedFolderPackagesService) {}

  @Get(':id/capture-sessions')
  @ApiOperation({ summary: 'List exact robot/capture-session memberships among uploaded files; no capture-end marker is required' })
  captureSessions(@Param('id') id: string, @Query('robotId') robotId: string | undefined, @CurrentUser() user: AuthUser) {
    return this.recordings.captureSessions(id, robotId, user);
  }

  @Get(':id/capture-sessions/:sessionId/package')
  @ApiOperation({ summary: 'Download a verified uploaded snapshot across all sensors of one robot capture session' })
  @ApiResponse({ status: 202, description: 'Background generation is running; poll the same robot/session URL with taskId.' })
  async captureSessionPackage(@Param('id') id: string, @Param('sessionId') sessionId: string,
    @Query('robotId') robotId: string, @CurrentUser() user: AuthUser,
    @Query('async') background: string | undefined, @Query('retry') retry: string | undefined, @Query('taskId') taskId: string | undefined,
    @Res({ passthrough: true }) response: Response) {
    if (background !== 'true' || retry !== undefined && retry !== 'true' && retry !== 'false'
        || taskId !== undefined && (retry !== undefined || typeof taskId !== 'string' || !/^[a-f0-9]{64}$/.test(taskId)))
      throw new BadRequestException('采集压缩包必须使用有效的后台任务参数');
    const result = await this.recordings.captureSessionPackage(id, sessionId, robotId, user,
      { background: true, retry: retry === 'true', ...(taskId ? { taskId } : {}) });
    const processing = 'state' in result && result.state === 'processing';
    response.status(processing ? 202 : 200);
    if (processing) response.setHeader('Retry-After', '2');
    return result;
  }

  @Get(':id/prepared-folder-packages')
  @ApiOperation({ summary: 'List persistent device-folder preparations and completed download snapshots' })
  preparedFolderPackages(@Param('id') id: string, @CurrentUser() user: AuthUser) {
    return this.prepared.list(id, user);
  }

  @Post(':id/prepared-folder-packages')
  @HttpCode(202)
  @ApiOperation({ summary: 'Persist preparation requests for every root device folder; processing continues after logout' })
  prepareFolderPackages(@Param('id') id: string, @CurrentUser() user: AuthUser) {
    return this.prepared.request(id, user);
  }

  @Get(':id/folder-package')
  @ApiOperation({ summary: 'Download a timestamped, verified snapshot of a folder and all its uploaded descendants' })
  @ApiResponse({ status: 202, description: 'With async=true, generation is running; poll the same URL without retry=true.' })
  async folderPackage(@Param('id') id: string, @Query('path') path: string, @CurrentUser() user: AuthUser,
    @Query('async') background: string | undefined, @Query('retry') retry: string | undefined, @Query('taskId') taskId: string | undefined,
    @Res({ passthrough: true }) response: Response) {
    if ([background, retry].some(value => value !== undefined && value !== 'true' && value !== 'false'))
      throw new BadRequestException('压缩包请求参数无效');
    if ((retry !== undefined || taskId !== undefined) && background !== 'true'
        || taskId !== undefined && (retry !== undefined || typeof taskId !== 'string' || !/^[a-f0-9]{64}$/.test(taskId)))
      throw new BadRequestException('压缩包任务参数无效');
    const result = await this.recordings.folderPackage(id, path, user,
      { background: background === 'true', retry: retry === 'true', ...(taskId ? { taskId } : {}) });
    const processing = 'state' in result && result.state === 'processing';
    response.status(processing ? 202 : 200);
    if (processing) response.setHeader('Retry-After', '2');
    return result;
  }

  @Post(':id/recording-sessions/:sessionId/end')
  @HttpCode(200)
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @ApiOperation({ summary: 'Persist an authenticated capture-session end or evidenced interruption proof without modifying source files' })
  endSession(@Param('id') id: string, @Param('sessionId') sessionId: string,
    @Body() body: EndRecordingSessionDto, @CurrentUser() user: AuthUser) {
    return this.recordings.endSession(id, sessionId, body, user);
  }

  @Get(':id/recordings')
  @ApiOperation({ summary: 'List continuous native-camera recordings in a sensor directory' })
  list(@Param('id') id: string, @Query('path') path: string = '', @CurrentUser() user: AuthUser) {
    return this.recordings.list(id, path, user);
  }

  @Get(':id/recordings/:recordingId/package')
  @ApiOperation({ summary: 'Get a timestamped compressed package containing the verified recording' })
  package(@Param('id') id: string, @Param('recordingId') recordingId: string, @CurrentUser() user: AuthUser) {
    return this.recordings.package(id, recordingId, user);
  }

  @Get(':id/archive-packages')
  @ApiOperation({ summary: 'List timestamped compact archive packages in a sensor directory' })
  packages(@Param('id') id: string, @Query('path') path: string = '', @CurrentUser() user: AuthUser) {
    return this.recordings.listPackages(id, path, user);
  }

  @Post(':id/recordings/:recordingId/assemble')
  @HttpCode(202)
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @ApiOperation({ summary: 'Enqueue a verified, lossless continuous-recording export' })
  assemble(@Param('id') id: string, @Param('recordingId') recordingId: string, @CurrentUser() user: AuthUser) {
    return this.recordings.requestAssembly(id, recordingId, user);
  }
}
