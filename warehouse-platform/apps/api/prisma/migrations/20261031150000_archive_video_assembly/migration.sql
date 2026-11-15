ALTER TYPE "JobType" ADD VALUE IF NOT EXISTS 'ARCHIVE_VIDEO_ASSEMBLE';
ALTER TABLE "Job" ADD COLUMN "deduplicationKey" TEXT;
CREATE UNIQUE INDEX "Job_deduplicationKey_key" ON "Job"("deduplicationKey");
