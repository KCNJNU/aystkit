ALTER TABLE "RobotFile" ADD COLUMN "folderPath" TEXT NOT NULL DEFAULT '';
ALTER TABLE "UploadSession" ADD COLUMN "folderPath" TEXT NOT NULL DEFAULT '';
DROP INDEX "RobotFile_datasetId_checksum_key";
CREATE UNIQUE INDEX "RobotFile_datasetId_folderPath_checksum_key" ON "RobotFile"("datasetId", "folderPath", "checksum");
CREATE INDEX "RobotFile_datasetId_folderPath_idx" ON "RobotFile"("datasetId", "folderPath");
CREATE TABLE "DatasetFolder" (
  "id" UUID NOT NULL, "datasetId" UUID NOT NULL, "path" TEXT NOT NULL,
  "parentPath" TEXT NOT NULL DEFAULT '', "name" TEXT NOT NULL, "objectKey" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "DatasetFolder_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "DatasetFolder_datasetId_fkey" FOREIGN KEY ("datasetId") REFERENCES "Dataset"("id") ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE UNIQUE INDEX "DatasetFolder_datasetId_path_key" ON "DatasetFolder"("datasetId", "path");
CREATE UNIQUE INDEX "DatasetFolder_objectKey_key" ON "DatasetFolder"("objectKey");
CREATE INDEX "DatasetFolder_datasetId_parentPath_idx" ON "DatasetFolder"("datasetId", "parentPath");
-- Reflect existing physical directories without moving files or changing frozen manifests.
UPDATE "RobotFile" f SET "folderPath" = regexp_replace(substring(f."objectKey" FROM length(d.name) + 2), '/[^/]+$', '')
FROM "Dataset" d WHERE f."datasetId" = d.id AND starts_with(f."objectKey", d.name || '/') AND position('/' in substring(f."objectKey" FROM length(d.name) + 2)) > 0;
UPDATE "UploadSession" u SET "folderPath" = regexp_replace(substring(u."finalObjectKey" FROM length(d.name) + 2), '/[^/]+$', '')
FROM "Dataset" d WHERE u."datasetId" = d.id AND starts_with(u."finalObjectKey", d.name || '/') AND position('/' in substring(u."finalObjectKey" FROM length(d.name) + 2)) > 0;
WITH paths AS (
  SELECT "datasetId", "folderPath" FROM "RobotFile" WHERE "datasetId" IS NOT NULL AND "folderPath" <> ''
  UNION SELECT "datasetId", "folderPath" FROM "UploadSession" WHERE "datasetId" IS NOT NULL AND "folderPath" <> ''
), ancestors AS (
 SELECT DISTINCT "datasetId", array_to_string((string_to_array("folderPath", '/'))[1:n], '/') AS path
 FROM paths CROSS JOIN LATERAL generate_series(1, array_length(string_to_array("folderPath", '/'), 1)) AS n
)
INSERT INTO "DatasetFolder" (id, "datasetId", path, "parentPath", name, "objectKey")
SELECT gen_random_uuid(), a."datasetId", a.path,
 CASE WHEN position('/' in a.path) > 0 THEN regexp_replace(a.path, '/[^/]+$', '') ELSE '' END,
 regexp_replace(a.path, '^.*/', ''), d.name || '/' || a.path || '/.folder'
FROM ancestors a JOIN "Dataset" d ON d.id = a."datasetId";
CREATE TRIGGER dataset_folder_writable BEFORE INSERT OR UPDATE OF "datasetId" ON "DatasetFolder" FOR EACH ROW EXECUTE FUNCTION ensure_dataset_writable();
