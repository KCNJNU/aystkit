import { Module } from '@nestjs/common';
import { FilesController } from './files.controller';
import { FilesService } from './files.service';
import { StorageModule } from '../storage/storage.module';
import { FileLifecycleService } from './file-lifecycle.service';

@Module({ imports: [StorageModule], controllers: [FilesController], providers: [FilesService, FileLifecycleService] })
export class FilesModule {}
