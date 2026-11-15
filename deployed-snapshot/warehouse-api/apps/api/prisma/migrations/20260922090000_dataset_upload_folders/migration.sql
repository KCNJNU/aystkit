ALTER TABLE "RobotFile" ADD COLUMN "datasetId" UUID;
ALTER TABLE "UploadSession" ADD COLUMN "datasetId" UUID;
ALTER TABLE "RobotFile" ADD CONSTRAINT "RobotFile_datasetId_fkey" FOREIGN KEY ("datasetId") REFERENCES "Dataset"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "UploadSession" ADD CONSTRAINT "UploadSession_datasetId_fkey" FOREIGN KEY ("datasetId") REFERENCES "Dataset"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
DROP INDEX "RobotFile_checksum_key";
CREATE UNIQUE INDEX "RobotFile_datasetId_checksum_key" ON "RobotFile"("datasetId", "checksum");
-- Preserve deduplication for legacy robot uploads outside datasets.
CREATE UNIQUE INDEX "RobotFile_legacy_checksum_key" ON "RobotFile"("checksum") WHERE "datasetId" IS NULL;
CREATE INDEX "RobotFile_checksum_idx" ON "RobotFile"("checksum");
DROP INDEX "UploadSession_robotFileId_key";
