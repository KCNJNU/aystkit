import { JobStatus } from '@prisma/client';
import { PrismaService } from '../prisma/prisma.service';
import { JobCancellationRequestedError, JobOwnershipLostError, JobsService } from './jobs.service';

describe('assembly job ownership fencing', () => {
  function setup(owner = 'worker-new', cancelled = false) {
    const tx = { $queryRaw: jest.fn(async () => [{ lockedBy: owner, status: JobStatus.RUNNING, cancelRequested: cancelled }]),
      job: { update: jest.fn(async () => ({ id: 'job-a', type: 'ARCHIVE_VIDEO_ASSEMBLE', requestedById: null })) },
      jobStep: { update: jest.fn(), updateMany: jest.fn(), findUnique: jest.fn(), findMany: jest.fn() }, notification: { create: jest.fn() } };
    const prisma = { $transaction: jest.fn(async (fn: (client: typeof tx) => Promise<unknown>) => fn(tx)) };
    return { jobs: new JobsService(prisma as unknown as PrismaService), tx };
  }
  it('rejects finish, fail, cancel and step updates from a worker that lost ownership', async () => {
    const { jobs, tx } = setup();
    await expect(jobs.finish('job-a', {}, 'worker-old')).rejects.toBeInstanceOf(JobOwnershipLostError);
    await expect(jobs.fail('job-a', new Error('old failure'), 'step', false, 'worker-old')).rejects.toBeInstanceOf(JobOwnershipLostError);
    await expect(jobs.markCancelled('job-a', 'worker-old')).rejects.toBeInstanceOf(JobOwnershipLostError);
    await expect(jobs.beginStep('job-a', 'step', 'worker-old')).rejects.toBeInstanceOf(JobOwnershipLostError);
    await expect(jobs.completeStep('job-a', 'step', {}, 'worker-old')).rejects.toBeInstanceOf(JobOwnershipLostError);
    expect(tx.job.update).not.toHaveBeenCalled(); expect(tx.jobStep.updateMany).not.toHaveBeenCalled(); expect(tx.jobStep.update).not.toHaveBeenCalled();
  });
  it('checks a cancellation under the same job row lock used to commit success', async () => {
    const { jobs, tx } = setup('worker-a', true);
    await expect(jobs.finish('job-a', {}, 'worker-a')).rejects.toBeInstanceOf(JobCancellationRequestedError);
    expect(tx.job.update).not.toHaveBeenCalled();
  });
  it('allows the current owner to complete a running job', async () => {
    const { jobs, tx } = setup('worker-a');
    await jobs.finish('job-a', {}, 'worker-a');
    expect(tx.job.update).toHaveBeenCalledWith(expect.objectContaining({ data: expect.objectContaining({ status: JobStatus.SUCCEEDED }) }));
  });
});
