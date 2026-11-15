CREATE TABLE "RecycledFile" (
  "id" UUID NOT NULL,
  "datasetId" UUID,
  "tenantId" UUID,
  "projectId" UUID,
  "originalName" TEXT NOT NULL,
  "objectKey" TEXT NOT NULL,
  "fileSize" BIGINT NOT NULL,
  "folderPath" TEXT NOT NULL,
  "snapshot" JSONB NOT NULL,
  "deletedById" UUID NOT NULL,
  "deletedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "reviewAt" TIMESTAMP(3) NOT NULL,
  "purgingAt" TIMESTAMP(3),
  CONSTRAINT "RecycledFile_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "RecycledFile_objectKey_key" ON "RecycledFile"("objectKey");
CREATE INDEX "RecycledFile_tenantId_projectId_deletedAt_idx" ON "RecycledFile"("tenantId", "projectId", "deletedAt");
CREATE INDEX "RecycledFile_datasetId_idx" ON "RecycledFile"("datasetId");
