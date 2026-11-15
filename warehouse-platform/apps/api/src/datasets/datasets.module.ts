import { Module } from '@nestjs/common';
import { DatasetsController } from './datasets.controller';
import { DatasetPublishService } from './dataset-publish.service';
import { DatasetsService } from './datasets.service';
import { DvcModule } from '../dvc/dvc.module';
import { StorageModule } from '../storage/storage.module';
import { ArchiveRecordingsController } from './archive-recordings.controller';
import { ArchiveRecordingsService } from './archive-recordings.service';
import { PreparedFolderPackagesService } from './prepared-folder-packages.service';

@Module({ imports: [DvcModule, StorageModule], controllers: [DatasetsController, ArchiveRecordingsController], providers: [DatasetsService, DatasetPublishService, ArchiveRecordingsService, PreparedFolderPackagesService], exports: [DatasetsService, DatasetPublishService, ArchiveRecordingsService, PreparedFolderPackagesService] })
export class DatasetsModule {}
