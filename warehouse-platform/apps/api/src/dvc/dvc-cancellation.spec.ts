import { ConfigService } from '@nestjs/config';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DvcService } from './dvc.service';

describe('DVC command cancellation', () => {
  it('cancels an actual child and preserves the lease error instead of treating it as an allowed command failure', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'warehouse-dvc-cancel-'));
    const service = new DvcService(new ConfigService({ DVC_REPOSITORY_ROOT: directory }));
    const io = service as unknown as { run(command: string, args: string[], remote: undefined, allowFailure: boolean, cwd: string, signal: AbortSignal): Promise<unknown> };
    const control = new AbortController();
    const reason = new Error('publication lease lost');
    const running = io.run(process.execPath, ['-e', "require('node:fs').writeFileSync('started', 'yes'); setInterval(() => {}, 1000)"], undefined, true, directory, control.signal);
    const result = expect(running).rejects.toBe(reason);
    try {
      const deadline = Date.now() + 3000;
      while (true) {
        try { await readFile(join(directory, 'started')); break; }
        catch { if (Date.now() > deadline) throw new Error('fixture child did not start'); await new Promise(resolve => setTimeout(resolve, 20)); }
      }
      control.abort(reason);
      await result;
    } finally { control.abort(reason); await running.catch(() => undefined); await rm(directory, { recursive: true, force: true }); }
  });
});
