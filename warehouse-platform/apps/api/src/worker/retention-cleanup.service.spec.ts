import { RetentionCleanupService } from './retention-cleanup.service';

describe('Warehouse retention compatibility', () => {
  it('settles an old queued retention job without deleting domain or audit data', async () => {
    await expect(new RetentionCleanupService().execute()).resolves.toMatchObject({ skipped: true });
  });
});
