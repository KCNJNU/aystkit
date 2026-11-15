import { Body, Controller, Get, Post, Query } from '@nestjs/common';
import { ApiOperation, ApiTags } from '@nestjs/swagger';
import { IsBoolean, IsOptional, IsUUID } from 'class-validator';
import { UserRole } from '@prisma/client';
import { CurrentUser, Roles } from '../auth/auth.decorators';
import { AuthUser } from '../auth/auth.types';
import { ArchiveService } from './archive.service';

class RobotQueryDto { @IsOptional() @IsUUID() robotId?: string; }
class GarageDto extends RobotQueryDto { @IsBoolean() enabled!: boolean; }
@ApiTags('garage-archive')
@Roles(UserRole.ADMIN)
@Controller('v1/archive')
export class ArchiveController {
  constructor(private readonly archive: ArchiveService) {}
  @Get('status')
  @ApiOperation({ summary: '地库原生采集状态、缓存和缺流信息' })
  status(@Query() query: RobotQueryDto) { return this.archive.status(query.robotId); }
  @Post('garage')
  @ApiOperation({ summary: '进入地库时开启持久采集标记，离开时关闭；不启动任何传感器或运动控制' })
  garage(@Body() body: GarageDto, @CurrentUser() user: AuthUser) { return this.archive.setGarage(body.enabled, user.id, body.robotId); }
}
