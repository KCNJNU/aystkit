import { ConflictException } from '@nestjs/common';
import { JobStatus, JobType, Prisma } from '@prisma/client';
import { ArchiveAssemblyCancelledError, ArchiveRecordingsService } from '../datasets/archive-recordings.service';
import { JobCancellationRequestedError, JobOwnershipLostError, JobsService } from '../jobs/jobs.service';
import { PrismaService } from '../prisma/prisma.service';
import { JobExecutorService } from './job-executor.service';

const job = { id: 'job-a', type: JobType.ARCHIVE_VIDEO_ASSEMBLE, payload: {} } as unknown as Parameters<JobExecutorService['execute']>[0];
const step = 'Assemble Continuous Recording';
const known = (code: string, message = 'database request failed', meta?: Record<string, unknown>) => new Prisma.PrismaClientKnownRequestError(message, { code, clientVersion: 'test', meta });
const initialization = (code?: string, message = 'database initialization failed') => new Prisma.PrismaClientInitializationError(message, 'test', code);
const acquisitionTimeout = 'Unable to start a transaction in the given time.';

function setup(error: Error, attempts = 1, maxAttempts = 3) {
  const tx = { $queryRaw: jest.fn(async () => [{ lockedBy: 'worker-a', status: JobStatus.RUNNING, cancelRequested: false }]),
    job: { findUniqueOrThrow: jest.fn(async () => ({ ...job, attempts, maxAttempts, requestedById: null })), update: jest.fn() },
    jobStep: { updateMany: jest.fn() }, notification: { create: jest.fn() } };
  const prisma = { $transaction: jest.fn(async (fn: (client: typeof tx) => Promise<unknown>) => fn(tx)) };
  const jobs = new JobsService(prisma as unknown as PrismaService);
  jest.spyOn(jobs, 'isCancelRequested').mockResolvedValue(false);
  jest.spyOn(jobs, 'beginStep').mockResolvedValue(true);
  const fail = jest.spyOn(jobs, 'fail'), cancelled = jest.spyOn(jobs, 'markCancelled');
  const recordings = { assemble: jest.fn().mockRejectedValue(error) };
  const executor = new JobExecutorService(jobs, {} as never, {} as never, {} as never, {} as never,
    recordings as unknown as ArchiveRecordingsService);
  return { executor, jobs, tx, fail, cancelled };
}

describe('worker infrastructure error classification', () => {
  it.each([
    ['known unreachable', known('P1001', "Can't reach database server at postgres:5432")],
    ['initialization unreachable', initialization('P1001', "Can't reach database server at postgres:5432")],
    ['known connection timeout', known('P1002')],
    ['initialization connection timeout', initialization('P1002')],
    ['closed connection', known('P1017')],
    ['connection pool timeout', known('P2024')],
    ['transaction acquisition timeout metadata', known('P2028', 'database request failed', { error: acquisitionTimeout })],
    ['transaction acquisition timeout message', known('P2028', `Invalid prisma invocation:\n\nTransaction API error: ${acquisitionTimeout}`)],
  ])('requeues %s using the existing bounded infrastructure policy', async (_name, error) => {
    const { executor, tx, fail, cancelled } = setup(error);
    const now = Date.now();
    const clock = jest.spyOn(Date, 'now').mockReturnValue(now);
    try {
      await executor.execute(job, 'worker-a');
      expect(fail).toHaveBeenCalledWith(job.id, error, step, true, 'worker-a');
      expect(tx.job.update).toHaveBeenCalledWith({ where: { id: job.id }, data: expect.objectContaining({
        status: JobStatus.QUEUED, errorCode: 'INFRASTRUCTURE_ERROR', availableAt: new Date(now + 5_000), lockedBy: null,
      }) });
      expect(cancelled).not.toHaveBeenCalled();
    } finally { clock.mockRestore(); }
  });

  it.each([
    ['connection failure', known('P1001')],
    ['transaction acquisition timeout', known('P2028', `Transaction API error: ${acquisitionTimeout}`)],
  ])('stops retrying %s at maxAttempts', async (_name, error) => {
    const { executor, tx, fail } = setup(error, 3, 3);
    await executor.execute(job, 'worker-a');
    expect(fail).toHaveBeenCalledWith(job.id, error, step, true, 'worker-a');
    expect(tx.job.update).toHaveBeenCalledWith({ where: { id: job.id }, data: expect.objectContaining({
      status: JobStatus.FAILED, errorCode: 'INFRASTRUCTURE_ERROR', finishedAt: expect.any(Date),
    }) });
    expect(tx.job.update.mock.calls[0][0].data).not.toHaveProperty('availableAt');
  });

  it.each([
    ['invalid image named storage.png', new ConflictException('压缩包图像格式不正确：处理前/storage.png')],
    ['invalid JSON named socket.json', new ConflictException('压缩包 JSON 文件无效：socket.json')],
    ['unique constraint mentioning storage', known('P2002', 'Unique constraint failed on storage_objectKey_key')],
    ['foreign key constraint', known('P2003')],
    ['record not found', known('P2025')],
    ['operation timeout without a connection code', known('P1008', 'Operations timed out after 1000ms')],
    ['transaction business error', known('P2028', 'Transaction API error: invalid storage record')],
    ['closed transaction', known('P2028', 'Transaction API error: Transaction already closed: A query cannot be executed on an expired transaction.',
      { error: 'Transaction already closed: A query cannot be executed on an expired transaction.' })],
    ['different transaction timeout wording', known('P2028', 'Transaction API error: Unable to start a transaction in given time.')],
    ['non-string transaction metadata', known('P2028', 'Transaction API error', { error: { message: acquisitionTimeout } })],
    ['constraint with acquisition wording', known('P2002', `Transaction API error: ${acquisitionTimeout}`, { error: acquisitionTimeout })],
    ['initialization with acquisition wording', initialization('P2028', `Transaction API error: ${acquisitionTimeout}`)],
    ['untyped acquisition wording', new Error(`Transaction API error: ${acquisitionTimeout}`)],
    ['authentication failure', initialization('P1000', 'Authentication failed for storage user')],
    ['schema configuration error', initialization('P1012')],
    ['uncoded initialization failure', initialization(undefined, 'Invalid storage configuration')],
  ])('keeps %s as a terminal business/configuration error', async (_name, error) => {
    const { executor, tx, fail } = setup(error);
    await executor.execute(job, 'worker-a');
    expect(fail).toHaveBeenCalledWith(job.id, error, step, false, 'worker-a');
    expect(tx.job.update).toHaveBeenCalledWith({ where: { id: job.id }, data: expect.objectContaining({
      status: JobStatus.FAILED, errorCode: 'BUSINESS_ERROR',
    }) });
  });

  it.each([new ArchiveAssemblyCancelledError(), new JobCancellationRequestedError()])('preserves normal cancellation for %s', async error => {
    const { executor, tx, fail, cancelled } = setup(error);
    await executor.execute(job, 'worker-a');
    expect(cancelled).toHaveBeenCalledWith(job.id, 'worker-a');
    expect(fail).not.toHaveBeenCalled();
    expect(tx.job.update).toHaveBeenCalledWith({ where: { id: job.id }, data: expect.objectContaining({ status: JobStatus.CANCELLED }) });
  });

  it('does not change a job after assembly loses ownership', async () => {
    const { executor, tx, fail, cancelled } = setup(new JobOwnershipLostError());
    await executor.execute(job, 'worker-a');
    expect(fail).not.toHaveBeenCalled(); expect(cancelled).not.toHaveBeenCalled(); expect(tx.job.update).not.toHaveBeenCalled();
  });

  it('does not requeue a connection failure after the job ownership has moved', async () => {
    const { executor, tx, fail } = setup(known('P1001'));
    tx.$queryRaw.mockResolvedValue([{ lockedBy: 'worker-new', status: JobStatus.RUNNING, cancelRequested: false }]);
    await expect(executor.execute(job, 'worker-a')).resolves.toBeUndefined();
    expect(fail).toHaveBeenCalledWith(job.id, expect.any(Prisma.PrismaClientKnownRequestError), step, true, 'worker-a');
    expect(tx.job.update).not.toHaveBeenCalled();
  });
});
