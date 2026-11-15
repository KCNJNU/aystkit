import { Injectable, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';

@Injectable()
export class ProductionSecurityValidator implements OnModuleInit {
  constructor(private readonly config: ConfigService) {}
  onModuleInit() {
    if (this.config.get<string>('DEPLOYMENT_ENVIRONMENT')?.toUpperCase() !== 'PRODUCTION' || process.env.RUNTIME_SERVICE_NAME !== 'api') return;
    for (const key of ['RATE_LIMIT_KEY_SALT']) this.required(key);
  }
  private required(key: string) {
    const value = this.config.getOrThrow<string>(key).trim();
    if (!value || /replace-me|development-only/i.test(value)) throw new Error(`Production security configuration ${key} is invalid`);
    return value;
  }
}
