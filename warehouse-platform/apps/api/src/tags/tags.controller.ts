import { DataReader } from '../auth/permissions';
import { Body, Controller, Delete, HttpCode, Param, Patch, Post, Get } from '@nestjs/common';
import { UserRole } from '@prisma/client';
import { IsArray, IsNotEmpty, IsOptional, IsString, IsUUID, Matches } from 'class-validator';
import { AuthUser } from '../auth/auth.types';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { TagsService } from './tags.service';

class CreateTagDto { @IsString() @IsNotEmpty() name!: string; @Matches(/^#[0-9a-f]{6}$/i) color!: string; }
class UpdateTagDto { @IsOptional() @IsString() @IsNotEmpty() name?: string; @IsOptional() @Matches(/^#[0-9a-f]{6}$/i) color?: string; }
class BulkTagDto { @IsArray() @IsUUID('4', { each: true }) fileIds!: string[]; @IsArray() @IsUUID('4', { each: true }) tagIds!: string[]; }

@DataReader()
@Controller('v1/tags')
export class TagsController {
  constructor(private readonly tags: TagsService) {}
  @Get() list(@CurrentUser() user: AuthUser) { return this.tags.list(user); }
  @Post() @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) create(@Body() body: CreateTagDto, @CurrentUser() user: AuthUser) { return this.tags.create(body.name, body.color, user); }
  @Patch(':id') @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) update(@Param('id') id: string, @Body() body: UpdateTagDto, @CurrentUser() user: AuthUser) { return this.tags.update(id, body, user); }
  @Delete(':id') @HttpCode(204) @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) remove(@Param('id') id: string, @CurrentUser() user: AuthUser) { return this.tags.remove(id, user); }
  @Post('files/bulk') @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) bulk(@Body() body: BulkTagDto, @CurrentUser() user: AuthUser) { return this.tags.bulkAttachFiles(body.fileIds, body.tagIds, user); }
  @Post('files/:fileId/:tagId') @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) attachFile(@Param('fileId') fileId: string, @Param('tagId') tagId: string, @CurrentUser() user: AuthUser) { return this.tags.attachFile(fileId, tagId, user); }
  @Delete('files/:fileId/:tagId') @HttpCode(204) @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) detachFile(@Param('fileId') fileId: string, @Param('tagId') tagId: string, @CurrentUser() user: AuthUser) { return this.tags.detachFile(fileId, tagId, user); }
  @Post('datasets/:datasetId/:tagId') @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) attachDataset(@Param('datasetId') datasetId: string, @Param('tagId') tagId: string, @CurrentUser() user: AuthUser) { return this.tags.attachDataset(datasetId, tagId, user); }
  @Delete('datasets/:datasetId/:tagId') @HttpCode(204) @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN, UserRole.TENANT_ADMIN, UserRole.PROJECT_ADMIN, UserRole.OPERATOR) detachDataset(@Param('datasetId') datasetId: string, @Param('tagId') tagId: string, @CurrentUser() user: AuthUser) { return this.tags.detachDataset(datasetId, tagId, user); }
}
