import { datasetFolder, uploadDestination } from './dataset-path';
describe('Dataset storage paths', () => {
  it('keeps readable names and resolves relative paths for the active upload flow', () => {
    expect(datasetFolder('test01')).toBe('test01/');
    expect(uploadDestination('logs', 'session/file.txt')).toEqual({ folderPath: 'logs/session', name: 'file.txt' });
  });
  it.each(['../test', 'a/b', 'a\\b', ' test', '.', 'uploads', 'manifests', ''])('rejects unsafe or reserved dataset name %s', name => {
    expect(() => datasetFolder(name)).toThrow();
  });
  it.each(['../file.txt', '/file.txt', 'a//b', 'a/./b'])('rejects unsafe file path %s', name => {
    expect(() => uploadDestination('', name)).toThrow();
  });
});
