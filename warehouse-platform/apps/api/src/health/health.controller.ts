import { Controller, Get, Res } from '@nestjs/common';
import { ApiTags } from '@nestjs/swagger';
import { Response } from 'express';
import { Public } from '../auth/auth.decorators';
import { HealthService } from './health.service';
@ApiTags('system')
@Controller()
export class HealthController {
  constructor(private readonly health: HealthService) {}
  @Public() @Get('health') liveness() { return { status: 'healthy', timestamp: new Date().toISOString() }; }
  @Public() @Get('ready') async ready(@Res({ passthrough: true }) response: Response) {
    const report = await this.health.check();
    const ready = ['postgresql', 'redis', 'objectStorage'].every(key => ['healthy', 'slow'].includes(report.services[key].status));
    response.status(ready ? 200 : 503); return { status: ready ? 'healthy' : 'unhealthy', timestamp: report.timestamp };
  }
  @Get('system/status') status() { return this.health.check(); }
}
