CREATE TYPE "PerceptionBatchStatus" AS ENUM ('UPLOADING', 'IN_TEMP', 'PARTIAL', 'UPLOAD_FAILED');
CREATE TYPE "PerceptionObjectStatus" AS ENUM ('PENDING', 'UPLOADING', 'VERIFIED', 'FAILED');

CREATE TABLE "PerceptionWorkstation" (
  "id" UUID NOT NULL,
  "tenantId" UUID NOT NULL,
  "projectId" UUID NOT NULL,
  "localId" TEXT NOT NULL,
  "name" TEXT NOT NULL,
  "configuration" JSONB,
  "lastSeenAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "PerceptionWorkstation_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "PerceptionBatch" (
  "id" UUID NOT NULL,
  "tenantId" UUID NOT NULL,
  "projectId" UUID NOT NULL,
  "workstationId" UUID NOT NULL,
  "robotId" UUID,
  "createdById" UUID NOT NULL,
  "localBatchId" TEXT NOT NULL,
  "status" "PerceptionBatchStatus" NOT NULL DEFAULT 'UPLOADING',
  "sampleRate" INTEGER NOT NULL,
  "manifest" JSONB NOT NULL,
  "manifestKey" TEXT,
  "manifestChecksum" TEXT NOT NULL,
  "channelSummary" JSONB,
  "issueSummary" JSONB,
  "totalObjects" INTEGER NOT NULL DEFAULT 0,
  "totalBytes" BIGINT NOT NULL DEFAULT 0,
  "startedAt" TIMESTAMP(3) NOT NULL,
  "endedAt" TIMESTAMP(3),
  "uploadedAt" TIMESTAMP(3),
  "errorMessage" TEXT,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "PerceptionBatch_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "PerceptionObject" (
  "id" UUID NOT NULL,
  "batchId" UUID NOT NULL,
  "channelId" TEXT NOT NULL,
  "kind" TEXT NOT NULL,
  "relativePath" TEXT NOT NULL,
  "objectKey" TEXT,
  "mimeType" TEXT NOT NULL,
  "fileSize" BIGINT NOT NULL,
  "checksum" TEXT NOT NULL,
  "required" BOOLEAN NOT NULL DEFAULT false,
  "status" "PerceptionObjectStatus" NOT NULL DEFAULT 'PENDING',
  "chunkSize" INTEGER NOT NULL,
  "totalParts" INTEGER NOT NULL,
  "errorMessage" TEXT,
  "capturedFrom" TIMESTAMP(3),
  "capturedTo" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "PerceptionObject_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "PerceptionObjectPart" (
  "id" UUID NOT NULL,
  "objectId" UUID NOT NULL,
  "partNumber" INTEGER NOT NULL,
  "objectKey" TEXT NOT NULL,
  "size" INTEGER NOT NULL,
  "checksum" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "PerceptionObjectPart_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "PerceptionUploadGrant" (
  "id" UUID NOT NULL,
  "tenantId" UUID NOT NULL,
  "projectId" UUID NOT NULL,
  "workstationId" UUID NOT NULL,
  "batchId" UUID,
  "createdById" UUID NOT NULL,
  "tokenHash" TEXT NOT NULL,
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "consumedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "PerceptionUploadGrant_pkey" PRIMARY KEY ("id")
);

CREATE TABLE "PerceptionBatchEvent" (
  "id" UUID NOT NULL,
  "batchId" UUID NOT NULL,
  "actorUserId" UUID,
  "type" TEXT NOT NULL,
  "details" JSONB,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "PerceptionBatchEvent_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "PerceptionWorkstation_projectId_localId_key" ON "PerceptionWorkstation"("projectId", "localId");
CREATE INDEX "PerceptionWorkstation_tenantId_projectId_lastSeenAt_idx" ON "PerceptionWorkstation"("tenantId", "projectId", "lastSeenAt");
CREATE UNIQUE INDEX "PerceptionBatch_workstationId_localBatchId_key" ON "PerceptionBatch"("workstationId", "localBatchId");
CREATE INDEX "PerceptionBatch_tenantId_projectId_status_createdAt_idx" ON "PerceptionBatch"("tenantId", "projectId", "status", "createdAt");
CREATE UNIQUE INDEX "PerceptionObject_objectKey_key" ON "PerceptionObject"("objectKey");
CREATE UNIQUE INDEX "PerceptionObject_batchId_relativePath_key" ON "PerceptionObject"("batchId", "relativePath");
CREATE INDEX "PerceptionObject_batchId_status_idx" ON "PerceptionObject"("batchId", "status");
CREATE UNIQUE INDEX "PerceptionObjectPart_objectKey_key" ON "PerceptionObjectPart"("objectKey");
CREATE UNIQUE INDEX "PerceptionObjectPart_objectId_partNumber_key" ON "PerceptionObjectPart"("objectId", "partNumber");
CREATE UNIQUE INDEX "PerceptionUploadGrant_tokenHash_key" ON "PerceptionUploadGrant"("tokenHash");
CREATE INDEX "PerceptionUploadGrant_workstationId_expiresAt_idx" ON "PerceptionUploadGrant"("workstationId", "expiresAt");
CREATE INDEX "PerceptionBatchEvent_batchId_createdAt_idx" ON "PerceptionBatchEvent"("batchId", "createdAt");

ALTER TABLE "PerceptionWorkstation" ADD CONSTRAINT "PerceptionWorkstation_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "Tenant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionWorkstation" ADD CONSTRAINT "PerceptionWorkstation_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatch" ADD CONSTRAINT "PerceptionBatch_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "Tenant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatch" ADD CONSTRAINT "PerceptionBatch_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatch" ADD CONSTRAINT "PerceptionBatch_workstationId_fkey" FOREIGN KEY ("workstationId") REFERENCES "PerceptionWorkstation"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatch" ADD CONSTRAINT "PerceptionBatch_robotId_fkey" FOREIGN KEY ("robotId") REFERENCES "Robot"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatch" ADD CONSTRAINT "PerceptionBatch_createdById_fkey" FOREIGN KEY ("createdById") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionObject" ADD CONSTRAINT "PerceptionObject_batchId_fkey" FOREIGN KEY ("batchId") REFERENCES "PerceptionBatch"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "PerceptionObjectPart" ADD CONSTRAINT "PerceptionObjectPart_objectId_fkey" FOREIGN KEY ("objectId") REFERENCES "PerceptionObject"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "PerceptionUploadGrant" ADD CONSTRAINT "PerceptionUploadGrant_tenantId_fkey" FOREIGN KEY ("tenantId") REFERENCES "Tenant"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionUploadGrant" ADD CONSTRAINT "PerceptionUploadGrant_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionUploadGrant" ADD CONSTRAINT "PerceptionUploadGrant_workstationId_fkey" FOREIGN KEY ("workstationId") REFERENCES "PerceptionWorkstation"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionUploadGrant" ADD CONSTRAINT "PerceptionUploadGrant_batchId_fkey" FOREIGN KEY ("batchId") REFERENCES "PerceptionBatch"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "PerceptionUploadGrant" ADD CONSTRAINT "PerceptionUploadGrant_createdById_fkey" FOREIGN KEY ("createdById") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatchEvent" ADD CONSTRAINT "PerceptionBatchEvent_batchId_fkey" FOREIGN KEY ("batchId") REFERENCES "PerceptionBatch"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "PerceptionBatchEvent" ADD CONSTRAINT "PerceptionBatchEvent_actorUserId_fkey" FOREIGN KEY ("actorUserId") REFERENCES "User"("id") ON DELETE SET NULL ON UPDATE CASCADE;
