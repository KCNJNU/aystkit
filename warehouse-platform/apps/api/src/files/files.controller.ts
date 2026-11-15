import { DataReader } from '../auth/permissions';
import { Body, Controller, Delete, Get, HttpCode, Param, Patch, Post, Query, UploadedFile, UseInterceptors } from '@nestjs/common';
import { FileInterceptor } from '@nestjs/platform-express';
import { ApiBody, ApiConsumes, ApiCreatedResponse, ApiOkResponse, ApiOperation, ApiTags } from '@nestjs/swagger';
import { UserRole } from '@prisma/client';
import { IsDateString, IsNotEmpty, IsObject, IsOptional, IsString, IsUUID } from 'class-validator';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { ListFilesQueryDto } from './dto/list-files-query.dto';
import { FilesService } from './files.service';
import { DetachFileReferencesDto } from './dto/detach-file-references.dto';
import { FileLifecycleService } from './file-lifecycle.service';
import { ConfirmFileActionDto, RecycleQueryDto, RelocateFileDto } from './dto/file-lifecycle.dto';
import { UploadedFile as UploadedFilePayload } from './uploaded-file';

class CreateFileDto { @IsUUID() robotId!: string; @IsString() @IsNotEmpty() recognitionType!: string; @IsDateString() capturedAt!: string; @IsString() @IsNotEmpty() schemaVersion!: string; @IsOptional() @IsString() metadata?: string; }

class UpdateFileDto { @IsOptional() @IsString() @IsNotEmpty() recognitionType?: string; @IsOptional() @IsObject() metadata?: Record<string, unknown>; }

@ApiTags('files') @DataReader()
@Controller('v1/files')
export class FilesController {
  constructor(private readonly files: FilesService, private readonly lifecycle: FileLifecycleService) {}
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @Get('recycle-bin') @ApiOperation({ summary: '分页查看授权范围内的回收文件' })
  recycleBin(@Query() query: RecycleQueryDto, @CurrentUser() user: AuthUser) { return this.lifecycle.recycleList(query, user); }
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @Post('recycle-bin/:id/restore') @HttpCode(200) @ApiOperation({ summary: '确认恢复回收文件，不重新附加到版本' })
  restore(@Param('id') id: string, @Body() _body: ConfirmFileActionDto, @CurrentUser() user: AuthUser) { return this.lifecycle.restore(id, user); }
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN)
  @Post('recycle-bin/:id/purge') @HttpCode(200) @ApiOperation({ summary: '管理员确认彻底清理文件及存储对象，失败可重试' })
  purge(@Param('id') id: string, @Body() _body: ConfirmFileActionDto, @CurrentUser() user: AuthUser) { return this.lifecycle.purge(id, user); }
  @Get(':id/delete-impact') @ApiOperation({ summary: '预览回收和路径整理对版本与存储的影响' })
  impact(@Param('id') id: string, @CurrentUser() user: AuthUser) { return this.lifecycle.impact(id, user); }
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @Patch(':id/location') @ApiOperation({ summary: '同一数据集内移动或重命名文件并同步存储路径' })
  relocate(@Param('id') id: string, @Body() body: RelocateFileDto, @CurrentUser() user: AuthUser) { return this.lifecycle.relocate(id, body, user); }
  @Roles(UserRole.ADMIN,UserRole.SUPER_ADMIN,UserRole.TENANT_ADMIN,UserRole.PROJECT_ADMIN,UserRole.OPERATOR)
  @Post() @UseInterceptors(FileInterceptor('file',{limits:{fileSize:10*1024*1024}})) @ApiOperation({summary:'Upload a robot file of any type'}) @ApiConsumes('multipart/form-data') @ApiBody({schema:{type:'object'}}) @ApiCreatedResponse()
  create(@Body() body:CreateFileDto,@UploadedFile() file:UploadedFilePayload,@CurrentUser() user:AuthUser){return this.files.create({...body,file},user);}
  @Get() @ApiOkResponse() list(@Query() query:ListFilesQueryDto,@CurrentUser() user:AuthUser){return this.files.list(query,user);}
  @Get(':id') @ApiOkResponse() get(@Param('id') id:string,@CurrentUser() user:AuthUser){return this.files.get(id,user);}
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @Patch(':id') @ApiOperation({ summary: '编辑授权文件的分类和元信息，不修改原始文件或冻结版本' })
  update(@Param('id') id: string, @Body() body: UpdateFileDto, @CurrentUser() user: AuthUser) { return this.files.update(id, body, user); }
  @Get(':id/download') @ApiOkResponse() download(@Param('id') id:string,@CurrentUser() user:AuthUser){return this.files.getDownloadUrl(id,user);}
  @Get(':id/references') @ApiOperation({ summary: '查看文件在授权数据集中的版本引用及解除限制' }) @ApiOkResponse()
  references(@Param('id') id: string, @CurrentUser() user: AuthUser) { return this.files.references(id, user); }
  @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR)
  @Post(':id/references/detach') @HttpCode(200) @ApiOperation({ summary: '确认解除选定的草稿版本引用，保留原始文件与历史快照' })
  detach(@Param('id') id: string, @Body() body: DetachFileReferencesDto, @CurrentUser() user: AuthUser) { return this.files.detachReferences(id, body, user); }
  @Roles(UserRole.ADMIN,UserRole.SUPER_ADMIN,UserRole.TENANT_ADMIN,UserRole.PROJECT_ADMIN,UserRole.OPERATOR)
  @Delete(':id') @HttpCode(204) @ApiOperation({ summary: '将未被版本引用的文件移入回收站，保留存储对象' }) remove(@Param('id') id:string,@CurrentUser() user:AuthUser){return this.lifecycle.recycle(id,user);}
}
