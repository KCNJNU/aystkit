import { Module } from '@nestjs/common';
import { PrismaModule } from '../prisma/prisma.module';
import { WarehouseController } from './warehouse.controller';
import { WarehouseQueryService } from './warehouse-query.service';

@Module({ imports: [PrismaModule], controllers: [WarehouseController], providers: [WarehouseQueryService], exports: [WarehouseQueryService] })
export class WarehouseModule {}
