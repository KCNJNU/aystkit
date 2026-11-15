ALTER TYPE "JobType" ADD VALUE 'DATASET_DELETE';
ALTER TABLE "Dataset" ADD COLUMN "deletingAt" TIMESTAMP(3);
-- Close the race between checking permissions and inserting a child while a
-- deletion takes an exclusive lock on the dataset. Existing legacy NULLs work.
CREATE FUNCTION ensure_dataset_writable() RETURNS trigger AS $$
DECLARE removal TIMESTAMP(3);
BEGIN
  IF NEW."datasetId" IS NOT NULL THEN
    SELECT "deletingAt" INTO removal FROM "Dataset" WHERE id = NEW."datasetId" FOR KEY SHARE;
    IF removal IS NOT NULL THEN RAISE EXCEPTION 'Dataset deletion is in progress' USING ERRCODE = '23514'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER dataset_upload_writable BEFORE INSERT OR UPDATE OF "datasetId" ON "UploadSession" FOR EACH ROW EXECUTE FUNCTION ensure_dataset_writable();
CREATE TRIGGER dataset_file_writable BEFORE INSERT OR UPDATE OF "datasetId" ON "RobotFile" FOR EACH ROW EXECUTE FUNCTION ensure_dataset_writable();
CREATE TRIGGER dataset_version_writable BEFORE INSERT OR UPDATE OF "datasetId" ON "DatasetVersion" FOR EACH ROW EXECUTE FUNCTION ensure_dataset_writable();
