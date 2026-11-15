import { Injectable } from '@nestjs/common';

@Injectable()
export class RetentionCleanupService {
  execute() {
    return Promise.resolve({ skipped: true, reason: 'Automatic retention cleanup is not configured for the data warehouse', completedAt: new Date().toISOString() });
  }
}
