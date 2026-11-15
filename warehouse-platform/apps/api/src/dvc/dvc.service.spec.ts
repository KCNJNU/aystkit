import { ServiceUnavailableException } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { resolve } from 'node:path';
import { DvcService, parseDvcHash } from './dvc.service';

describe('DvcService repository boundary', () => {
  const repository = resolve('dvc-test-repository');
  const service = new DvcService(new ConfigService({ DVC_REPOSITORY_ROOT: repository }));

  it('resolves a dataset workspace inside the mounted repository', () => {
    expect(service.resolveWorkspace('data/datasets/example/v1')).toBe(resolve(repository, 'data/datasets/example/v1'));
  });

  it('rejects the repository root and traversal outside it', () => {
    expect(() => service.resolveWorkspace('.')).toThrow(ServiceUnavailableException);
    expect(() => service.resolveWorkspace('../secret')).toThrow(ServiceUnavailableException);
  });

  it('isolates DVC global state inside the configured repository by default', () => {
    const environment = (service as unknown as { dvcEnvironment(): Record<string, string> }).dvcEnvironment();
    expect(environment.DVC_NO_ANALYTICS).toBe('true');
    expect(environment.DVC_SYSTEM_CONFIG_DIR).toBe(resolve(repository, '.dvc', 'tmp', 'runtime', 'system-config'));
    expect(environment.DVC_GLOBAL_CONFIG_DIR).toBe(resolve(repository, '.dvc', 'tmp', 'runtime', 'global-config'));
    expect(environment.DVC_SITE_CACHE_DIR).toBe(resolve(repository, '.dvc', 'tmp', 'runtime', 'site-cache'));
  });
});

describe('DVC pointer parsing', () => {
  it('accepts file and directory pointer formats emitted by DVC 3.x', () => {
    expect(parseDvcHash('outs:\n- md5: abc123.dir\n  path: data')).toBe('abc123.dir');
    expect(parseDvcHash('md5: def456\npath: sample.json')).toBe('def456');
  });
});

describe('DVC dataset publication identity', () => {
  it('does not reuse another Chinese dataset tag at the same version', async () => {
    const service = new DvcService(new ConfigService({
      OBJECT_STORAGE_BUCKET: 'raw', DVC_REMOTE_BUCKET: 'versions', DVC_REMOTE_USE_SSL: 'false',
      DVC_REMOTE_ENDPOINT: 'localhost', DVC_REMOTE_PORT: '9000', DVC_REMOTE_NAME: 'test',
      DVC_REMOTE_ACCESS_KEY: 'fixture', DVC_REMOTE_SECRET_KEY: 'fixture',
      DATASET_PUBLISH_GIT_AUTHOR_NAME: 'Test', DATASET_PUBLISH_GIT_AUTHOR_EMAIL: 'test@example.invalid',
    }));
    const io = service as unknown as {
      ensureDvcRepository(): Promise<void>; readDvcHash(path: string): Promise<string>;
      runDvc(args: string[]): Promise<unknown>; runGit(args: string[]): Promise<{ code: number; stdout: string }>;
    };
    jest.spyOn(io, 'ensureDvcRepository').mockResolvedValue();
    jest.spyOn(io, 'readDvcHash').mockResolvedValue('hash');
    jest.spyOn(io, 'runDvc').mockResolvedValue({ code: 0, stdout: '' });
    const tags = new Set<string>();
    const git = jest.spyOn(io, 'runGit').mockImplementation(async args => {
      if (args[0] === 'rev-parse' && args.includes('--verify')) {
        return { code: tags.has(args[args.length - 1].replace('refs/tags/', '')) ? 0 : 1, stdout: '' };
      }
      if (args.includes('tag')) tags.add(args[args.indexOf('-a') + 1]);
      return { code: 0, stdout: 'commit' };
    });
    const first = await service.publish('data/dataset-a/1', '地库甲', 1, undefined, 'dataset-a');
    const second = await service.publish('data/dataset-b/1', '地库乙', 1, undefined, 'dataset-b');
    expect(first.gitTag).toBe('dataset-dataset-a-v1.0.0');
    expect(second.gitTag).toBe('dataset-dataset-b-v1.0.0');
    expect(tags.size).toBe(2);
    expect(git.mock.calls.some(([args]) => args[0] === 'rev-list')).toBe(false);
  });
});
