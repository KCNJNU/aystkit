CREATE TABLE IF NOT EXISTS "PreparedFolderPackage" (
    "id" UUID NOT NULL,
    "datasetId" UUID NOT NULL,
    "path" TEXT NOT NULL,
    "requestedById" UUID,
    "state" TEXT NOT NULL DEFAULT 'QUEUED',
    "taskId" TEXT NOT NULL,
    "snapshot" JSONB NOT NULL,
    "objectKey" TEXT NOT NULL,
    "fileName" TEXT NOT NULL,
    "readyAt" TIMESTAMP(3),
    "errorMessage" TEXT,
    "runOwner" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "PreparedFolderPackage_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "PreparedFolderPackage_state_check" CHECK ("state" IN ('QUEUED', 'PROCESSING', 'READY', 'FAILED')),
    CONSTRAINT "PreparedFolderPackage_datasetId_fkey" FOREIGN KEY ("datasetId") REFERENCES "Dataset"("id") ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT "PreparedFolderPackage_requestedById_fkey" FOREIGN KEY ("requestedById") REFERENCES "User"("id") ON DELETE SET NULL ON UPDATE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS "PreparedFolderPackage_datasetId_path_key" ON "PreparedFolderPackage"("datasetId", "path");
CREATE INDEX IF NOT EXISTS "PreparedFolderPackage_state_createdAt_idx" ON "PreparedFolderPackage"("state", "createdAt");
