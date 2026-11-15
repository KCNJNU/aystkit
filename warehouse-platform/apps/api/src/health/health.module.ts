import { Module } from '@nestjs/common';
import { ProviderModule } from '../ai/provider.module';
import { PrismaModule } from '../prisma/prisma.module';
import { StorageModule } from '../storage/storage.module';
import { HealthController } from './health.controller';
import { HealthService } from './health.service';

@Module({ imports: [PrismaModule, StorageModule, ProviderModule], controllers: [HealthController], providers: [HealthService], exports: [HealthService] })
export class HealthModule {}
