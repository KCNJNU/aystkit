import { Module } from '@nestjs/common';
import { HealthModule } from '../health/health.module';
import { PrismaModule } from '../prisma/prisma.module';
import { RedisThrottlerStorage } from '../auth/redis-throttler.storage';
import { ProviderModule } from './provider.module';
import { AIController } from './ai.controller';
import { AIChatService } from './ai-chat.service';
import { AIToolRegistry } from './ai-tool-registry';
import { WarehouseModule } from '../warehouse/warehouse.module';
@Module({ imports: [PrismaModule, ProviderModule, HealthModule, WarehouseModule], controllers: [AIController], providers: [AIChatService, AIToolRegistry, RedisThrottlerStorage] })
export class AIModule {}

