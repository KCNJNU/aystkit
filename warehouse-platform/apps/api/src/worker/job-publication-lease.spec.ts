import { JobType } from '@prisma/client';
import { ServiceUnavailableException } from '@nestjs/common';
import { JobExecutorService } from './job-executor.service';

const job = (id: string) => ({ id, type: JobType.DATASET_PUBLISH, payload: { versionId: id } }) as unknown as Parameters<JobExecutorService['execute']>[0];
const tick = () => new Promise<void>(resolve => setImmediate(resolve));

function setup() {
  let leaseOwner: string | undefined;
  const jobs = {
    acquireLease: jest.fn(async (_key: string, owner: string) => {
      if (leaseOwner && leaseOwner !== owner) return false;
      leaseOwner = owner; return true;
    }),
    releaseLease: jest.fn(async (_key: string, owner: string) => { if (owner === leaseOwner) leaseOwner = undefined; }),
    heartbeatLease: jest.fn(async () => ({ count: 1 })), heartbeat: jest.fn(async () => ({ count: 1 })),
    isCancelRequested: jest.fn(async () => false), beginStep: jest.fn(async () => true), completeStep: jest.fn(),
    finish: jest.fn(), fail: jest.fn(), defer: jest.fn(), markCancelled: jest.fn(),
  };
  const publisher = { publish: jest.fn<Promise<unknown>, [string, <T>(name: string, action: () => Promise<T>) => Promise<T>, AbortSignal]>(), recoveryVerify: jest.fn() };
  publisher.publish.mockResolvedValue({});
  const executor = new JobExecutorService(jobs as never, publisher as never, {} as never, {} as never, {} as never, {} as never);
  return { jobs, publisher, executor };
}

describe('publication lease isolation and recovery', () => {
  it('serializes two publication jobs in the same worker without releasing the first job lease', async () => {
    const { jobs, publisher, executor } = setup();
    let release!: () => void;
    publisher.publish.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({}); }));
    const first = executor.execute(job('job-a'), 'worker-a');
    await tick();
    try {
      await expect(executor.execute(job('job-b'), 'worker-a')).resolves.toBeUndefined();
      expect(publisher.publish).toHaveBeenCalledTimes(1);
      expect(jobs.defer).toHaveBeenCalledWith('job-b', 'worker-a');
      expect(jobs.releaseLease).not.toHaveBeenCalled();
    } finally { release(); await first; }
    expect(jobs.releaseLease).toHaveBeenCalledWith('git-dvc-publication', expect.stringMatching(/^worker-a:job-a:/));
  });

  it('handles acquisition failure in the normal bounded infrastructure retry path', async () => {
    const { jobs, publisher, executor } = setup();
    const error = new ServiceUnavailableException('database temporarily unreachable');
    jobs.acquireLease.mockRejectedValue(error);
    await expect(executor.execute(job('job-a'), 'worker-a')).resolves.toBeUndefined();
    expect(jobs.fail).toHaveBeenCalledWith('job-a', error, undefined, true, 'worker-a');
    expect(publisher.publish).not.toHaveBeenCalled();
    expect(jobs.releaseLease).not.toHaveBeenCalled();
  });

  it.each(['count', 'connection'] as const)('aborts an active publication on a lease heartbeat %s failure and never commits success', async failure => {
    jest.useFakeTimers();
    const { jobs, publisher, executor } = setup();
    let signal: AbortSignal | undefined;
    publisher.publish.mockImplementation(async (_version, step, activeSignal) => {
      signal = activeSignal;
      return step('DVC Push', () => new Promise((_resolve, reject) => {
        activeSignal.addEventListener('abort', () => reject(activeSignal.reason), { once: true });
      }));
    });
    try {
      const executing = executor.execute(job('job-a'), 'worker-a');
      await jest.advanceTimersByTimeAsync(0);
      expect(signal?.aborted).toBe(false);
      if (failure === 'count') jobs.heartbeatLease.mockResolvedValue({ count: 0 });
      else jobs.heartbeatLease.mockRejectedValue(new Error('connection closed'));
      await jest.advanceTimersByTimeAsync(10_000);
      await executing;
      expect(signal?.aborted).toBe(true);
      expect(jobs.finish).not.toHaveBeenCalled();
      expect(jobs.completeStep).not.toHaveBeenCalled();
      expect(jobs.fail).toHaveBeenCalledWith('job-a', expect.any(ServiceUnavailableException), 'DVC Push', true, 'worker-a');
    } finally { jest.useRealTimers(); }
  });

  it('keeps a lease release outage from rejecting the worker loop', async () => {
    const { jobs, executor } = setup();
    jobs.releaseLease.mockRejectedValue(new Error('database connection closed'));
    await expect(executor.execute(job('job-a'), 'worker-a')).resolves.toBeUndefined();
    expect(jobs.finish).toHaveBeenCalled();
  });
});
