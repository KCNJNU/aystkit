import { IsString, IsUUID, MaxLength, MinLength } from 'class-validator';
import { UserRole } from '@prisma/client';
import { Body, Controller, Get, Post } from '@nestjs/common';
import { ApiOperation, ApiTags } from '@nestjs/swagger';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { DataReader } from '../auth/permissions';
import { WarehouseQueryService } from './warehouse-query.service';

class RegisterRobotDto {
  @IsUUID() id!: string;
  @IsUUID() projectId!: string;
  @IsString() @MinLength(1) @MaxLength(60) name!: string;
  @IsString() @MinLength(1) @MaxLength(64) externalId!: string;
}

@ApiTags('warehouse')
@DataReader()
@Controller('warehouse')
export class WarehouseController {
  constructor(private readonly warehouse: WarehouseQueryService) {}
  @Post('robots') @Roles(UserRole.ADMIN, UserRole.SUPER_ADMIN, UserRole.SYSTEM_ADMIN)
  @ApiOperation({ summary: 'Register a physical robot in an active project' })
  registerRobot(@Body() body: RegisterRobotDto, @CurrentUser() user: AuthUser) { return this.warehouse.registerRobot(body, user); }
  @Get('robots') @ApiOperation({ summary: 'List accessible physical robot sources for selection' }) robots(@CurrentUser() user: AuthUser) { return this.warehouse.robots(user); }
  @Get('projects') @ApiOperation({ summary: 'List accessible warehouse projects' }) projects(@CurrentUser() user: AuthUser) { return this.warehouse.projects(user); }
  @Get('sources') @ApiOperation({ summary: 'List accessible file sources' }) sources(@CurrentUser() user: AuthUser) { return this.warehouse.sources(user); }
  @Get('overview') @ApiOperation({ summary: 'Get the current user warehouse overview' }) overview(@CurrentUser() user: AuthUser) { return this.warehouse.overview(user); }
}
