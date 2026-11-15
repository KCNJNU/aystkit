import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Modal, message } from 'antd';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DatasetVersion, RobotFile, ValidationReport } from '../services/api';
import { VersionValidation } from './VersionValidation';
import { VersionDrawer } from './DatasetDrawers';
import { Users } from '../pages/Users';
import { Storage } from '../pages/Storage';

const state = vi.hoisted(() => ({ role: 'SUPER_ADMIN', userId: 'actor' }));
const mocks = vi.hoisted(() => ({
  validateVersion: vi.fn(), freeze: vi.fn(), publishJob: vi.fn(), recoveryJob: vi.fn(),
  version: vi.fn(), allFiles: vi.fn(), removeFile: vi.fn(), addFiles: vi.fn(), publish: vi.fn(),
  request: vi.fn(), storageObjects: vi.fn(), reconciliationRuns: vi.fn(), reconciliationRun: vi.fn(), deleteOrphans: vi.fn(),
}));
vi.mock('../services/api', async (original) => {
  const module = await original<typeof import('../services/api')>();
  return { ...module, request: mocks.request, api: { ...module.api, ...mocks } };
});
vi.mock('../auth/AuthContext', () => {
  const auth = () => ({ user: { id: state.userId, role: state.role }, hasRole: (...roles: string[]) => roles.includes(state.role) });
  return { useOptionalAuth: auth, useAuth: auth };
});
const stamp = '2026-10-30T00:00:00.000Z';
const file = { id: 'file', robotId: 'robot', objectKey: 'fixture/file.json', originalName: 'file.json', fileType: 'json', mimeType: 'application/json', fileSize: '12', checksum: 'a'.repeat(64), recognitionType: 'fixture', schemaVersion: '1', capturedAt: stamp, createdAt: stamp, updatedAt: stamp } satisfies RobotFile;
const version = (status: DatasetVersion['status'] = 'DRAFT'): DatasetVersion => ({ id: 'version', datasetId: 'dataset', version: 1, status, fileCount: 1, totalSize: '12', createdAt: stamp, updatedAt: stamp, files: [{ robotFileId: file.id, robotFile: file }] });
const report = (valid = true): ValidationReport => ({ versionId: 'version', status: 'DRAFT', valid, validatedAt: stamp, summary: { files: 1, totalSize: '12', checkedObjects: 1, errors: valid ? 0 : 1, warnings: 0 }, issues: valid ? [] : [{ code: 'OBJECT_MISSING', severity: 'error', message: 'missing' }] });
function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  function Destination() { return <output data-testid="destination">{useLocation().pathname}</output>; }
  const value = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/']}><Routes><Route path="/" element={node} /><Route path="/jobs/:id" element={<Destination />} /></Routes></MemoryRouter></QueryClientProvider>);
  return { ...value, client };
}
let confirm: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  vi.clearAllMocks(); state.role = 'SUPER_ADMIN'; state.userId = 'actor';
  confirm = vi.spyOn(Modal, 'confirm');
  vi.spyOn(message, 'error').mockImplementation(() => (() => undefined) as never);
  vi.spyOn(message, 'success').mockImplementation(() => (() => undefined) as never);
  mocks.validateVersion.mockResolvedValue(report()); mocks.freeze.mockResolvedValue(version('FROZEN'));
  mocks.publishJob.mockResolvedValue({ id: 'publish-job' }); mocks.recoveryJob.mockResolvedValue({ id: 'recovery-job' });
  mocks.version.mockResolvedValue(version()); mocks.allFiles.mockResolvedValue([file]); mocks.removeFile.mockResolvedValue(undefined); mocks.publish.mockResolvedValue(version('PUBLISHED'));
  mocks.request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path.startsWith('/v1/users/page')) return { items: [{ id: 'member', email: 'fixture@example.test', displayName: '测试用户', role: 'OPERATOR', isActive: true, mustChangePassword: false, createdAt: stamp }], total: 1 };
    if (options?.method === 'DELETE' || options?.method === 'POST') return {};
    throw new Error('Unexpected mock route');
  });
  mocks.storageObjects.mockResolvedValue({ items: [] });
  mocks.reconciliationRuns.mockResolvedValue({ items: [{ id: 'scan-one', status: 'COMPLETED', scannedObjects: 2, issueCount: 2, createdAt: stamp }], total: 1 });
  mocks.reconciliationRun.mockResolvedValue({ id: 'scan-one', status: 'COMPLETED', issues: [{ id: 'orphan', type: 'ORPHAN_OBJECT', status: 'OPEN', objectKey: 'fixture/orphan' }, { id: 'missing', type: 'MISSING_OBJECT', status: 'OPEN', objectKey: 'fixture/missing' }] });
  mocks.deleteOrphans.mockResolvedValue({ id: 'delete-job' });
});
afterEach(() => { vi.restoreAllMocks(); message.destroy(); Modal.destroyAll(); });

describe('direct version operations', () => {
  it('freezes after successful validation on the first click, without a modal', async () => {
    const changed = vi.fn(); mount(<VersionValidation version={version()} onChanged={changed} />);
    fireEvent.click(screen.getByRole('button', { name: /校验并冻结/ }));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
    expect(mocks.freeze).toHaveBeenCalledExactlyOnceWith('version'); expect(confirm).not.toHaveBeenCalled();
  });
  it('publishes after validation and navigates directly to the returned job', async () => {
    mount(<VersionValidation version={version('FROZEN')} onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: /发布数据版本/ }));
    expect(await screen.findByTestId('destination')).toHaveTextContent('/jobs/publish-job');
    expect(mocks.publishJob).toHaveBeenCalledExactlyOnceWith('version'); expect(confirm).not.toHaveBeenCalled();
  });
  it('preserves integrity checks before a publish operation', async () => {
    mocks.validateVersion.mockResolvedValue(report(false)); mount(<VersionValidation version={version('FROZEN')} onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole('button', { name: /发布数据版本/ }));
    expect(await screen.findByText('版本存在阻止后续操作的完整性错误')).toBeVisible();
    expect(mocks.publishJob).not.toHaveBeenCalled(); expect(confirm).not.toHaveBeenCalled();
  });
  it('blocks duplicate clicks while validation is pending and permits an error retry', async () => {
    let resolve!: (value: ValidationReport) => void; mocks.validateVersion.mockImplementationOnce(() => new Promise<ValidationReport>(done => { resolve = done; }));
    mocks.freeze.mockRejectedValueOnce(new Error('冻结失败')); mount(<VersionValidation version={version()} onChanged={() => undefined} />);
    const button = screen.getByRole('button', { name: /校验并冻结/ }); fireEvent.click(button); fireEvent.click(button);
    expect(mocks.validateVersion).toHaveBeenCalledTimes(1); resolve(report());
    await waitFor(() => expect(message.error).toHaveBeenCalledWith('冻结失败'));
    fireEvent.click(await screen.findByRole('button', { name: /校验并冻结/ }));
    await waitFor(() => expect(mocks.freeze).toHaveBeenCalledTimes(2)); expect(confirm).not.toHaveBeenCalled();
  });
  it('keeps publication unavailable to read-only and operator roles', () => {
    state.role = 'BASIC'; const first = mount(<VersionValidation version={version()} onChanged={() => undefined} />);
    expect(screen.queryByRole('button', { name: /校验并冻结/ })).not.toBeInTheDocument(); first.unmount();
    state.role = 'OPERATOR'; mount(<VersionValidation version={version('FROZEN')} onChanged={() => undefined} />);
    expect(screen.queryByRole('button', { name: /发布数据版本/ })).not.toBeInTheDocument();
  });
});

describe('legacy drawer direct operations', () => {
  it('removes a draft membership immediately and freezes with validation, without Popconfirm', async () => {
    mount(<VersionDrawer versionId="version" onClose={() => undefined} onChanged={() => undefined} />);
    fireEvent.click(await screen.findByRole('button', { name: '移除文件 file.json' }));
    await waitFor(() => expect(mocks.removeFile).toHaveBeenCalledExactlyOnceWith('version', 'file'));
    fireEvent.click(await screen.findByRole('button', { name: /冻结版本/ }));
    await waitFor(() => expect(mocks.freeze).toHaveBeenCalledExactlyOnceWith('version'));
    expect(mocks.validateVersion).toHaveBeenCalledWith('version'); expect(screen.queryByText('确认冻结此版本？')).not.toBeInTheDocument(); expect(confirm).not.toHaveBeenCalled();
  });
  it('retains read-only controls in the legacy drawer', async () => {
    state.role = 'BASIC'; mount(<VersionDrawer versionId="version" onClose={() => undefined} onChanged={() => undefined} />);
    await screen.findByText('file.json'); expect(screen.queryByRole('button', { name: /移除文件/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /冻结版本/ })).not.toBeInTheDocument();
  });
});

describe('direct user management', () => {
  it('deletes on the first click with a synchronous duplicate guard', async () => {
    let finish!: () => void;
    mocks.request.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path.startsWith('/v1/users/page')) return { items: [{ id: 'member', displayName: '测试用户', email: 'fixture@example.test', role: 'OPERATOR', isActive: true, mustChangePassword: false, createdAt: stamp }], total: 1 };
      if (options?.method === 'DELETE') return new Promise<void>(done => { finish = done; });
      throw new Error('Unexpected route');
    });
    mount(<Users />); const button = await screen.findByRole('button', { name: '删除用户 测试用户' }); fireEvent.click(button); fireEvent.click(button);
    expect(mocks.request.mock.calls.filter(([,options]) => options?.method === 'DELETE')).toHaveLength(1); expect(confirm).not.toHaveBeenCalled(); finish();
    await waitFor(() => expect(message.success).toHaveBeenCalledWith('用户已删除'));
  });
  it('requires one password-entry form and sends no reset before submission', async () => {
    mount(<Users />); fireEvent.click(await screen.findByRole('button', { name: '重置密码' }));
    const dialog = await screen.findByRole('dialog'); const input = within(dialog).getByLabelText('新临时密码');
    expect(within(dialog).getByRole('button', { name: '重置密码' })).toBeDisabled();
    expect(mocks.request.mock.calls.some(([,options]) => options?.method === 'POST')).toBe(false);
    fireEvent.change(input, { target: { value: 'fixture-password-123' } });
    fireEvent.click(within(dialog).getByRole('button', { name: '重置密码' }));
    await waitFor(() => expect(mocks.request).toHaveBeenCalledWith('/v1/users/member/reset-password', { method: 'POST', body: JSON.stringify({ temporaryPassword: 'fixture-password-123' }) }));
    expect(confirm).not.toHaveBeenCalled();
  });
  it('reports a user deletion error and allows the same action to be retried', async () => {
    const base = mocks.request.getMockImplementation()!; let fails = true;
    mocks.request.mockImplementation(async (path: string, options?: RequestInit) => {
      if (options?.method === 'DELETE') { if (fails) { fails = false; throw new Error('拒绝删除'); } return {}; }
      return base(path, options);
    });
    mount(<Users />); const button = await screen.findByRole('button', { name: '删除用户 测试用户' });
    fireEvent.click(button); await waitFor(() => expect(message.error).toHaveBeenCalledWith('拒绝删除'));
    await waitFor(() => expect(button).not.toHaveClass('ant-btn-loading'));
    fireEvent.click(button); await waitFor(() => expect(message.success).toHaveBeenCalledWith('用户已删除'));
    expect(mocks.request.mock.calls.filter(([,options]) => options?.method === 'DELETE')).toHaveLength(2);
    expect(confirm).not.toHaveBeenCalled();
  });
  it('keeps user deletion unavailable without management permission', async () => {
    state.role = 'BASIC'; mount(<Users />); await screen.findByText('测试用户');
    expect(screen.queryByRole('button', { name: '删除用户 测试用户' })).not.toBeInTheDocument();
    expect(mocks.request.mock.calls.some(([,options]) => options?.method === 'DELETE')).toBe(false);
  });
});

describe('direct orphan cleanup', () => {
  it('deletes selected orphan issues immediately while missing-file issues remain unselectable', async () => {
    mount(<Storage />); fireEvent.click(screen.getByRole('tab', { name: /一致性核对/ }));
    fireEvent.click(await screen.findByText('scan-one')); const orphan = await screen.findByText('fixture/orphan'); const missing = screen.getByText('fixture/missing');
    const orphanRow = orphan.closest('tr')!; const missingRow = missing.closest('tr')!;
    expect(within(missingRow).getByRole('checkbox')).toBeDisabled();
    fireEvent.click(within(orphanRow).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /删除选中的孤立对象/ }));
    expect(await screen.findByTestId('destination')).toHaveTextContent('/jobs/delete-job');
    expect(mocks.deleteOrphans).toHaveBeenCalledExactlyOnceWith(['orphan']); expect(confirm).not.toHaveBeenCalled();
  });
  it('retains selected orphan issues after a failure and permits a direct retry', async () => {
    mocks.deleteOrphans.mockRejectedValueOnce(new Error('删除失败'));
    mount(<Storage />); fireEvent.click(screen.getByRole('tab', { name: /一致性核对/ }));
    fireEvent.click(await screen.findByText('scan-one'));
    const row = (await screen.findByText('fixture/orphan')).closest('tr')!;
    fireEvent.click(within(row).getByRole('checkbox'));
    const button = screen.getByRole('button', { name: /删除选中的孤立对象/ });
    fireEvent.click(button); await waitFor(() => expect(message.error).toHaveBeenCalledWith('删除失败'));
    await waitFor(() => expect(button).not.toHaveClass('ant-btn-loading'));
    fireEvent.click(button); expect(await screen.findByTestId('destination')).toHaveTextContent('/jobs/delete-job');
    expect(mocks.deleteOrphans).toHaveBeenNthCalledWith(1, ['orphan']); expect(mocks.deleteOrphans).toHaveBeenNthCalledWith(2, ['orphan']);
    expect(confirm).not.toHaveBeenCalled();
  });

  const scanRows = ['scan-a', 'scan-b'].map(id => ({ id, status: 'COMPLETED', scannedObjects: 1, issueCount: 1, createdAt: stamp }));
  const scanResult = (id: string) => ({ id, status: 'COMPLETED', issues: [{ id: 'issue-' + id, type: 'ORPHAN_OBJECT', status: 'OPEN', objectKey: 'fixture/' + id }] });
  function useTwoScans() {
    mocks.reconciliationRuns.mockResolvedValue({ items: scanRows, total: 2 });
    mocks.reconciliationRun.mockImplementation(async (id: string) => scanResult(id));
  }
  it('clears the selected issue when changing scans, including when returning to a cached scan', async () => {
    useTwoScans(); mount(<Storage />); fireEvent.click(screen.getByRole('tab', { name: /一致性核对/ }));
    fireEvent.click(await screen.findByText('scan-a'));
    const firstRow = (await screen.findByText('fixture/scan-a')).closest('tr')!;
    fireEvent.click(within(firstRow).getByRole('checkbox'));
    expect(screen.getByRole('button', { name: /删除选中的孤立对象/ })).toBeVisible();
    fireEvent.click(screen.getByText('scan-b'));
    const secondRow = (await screen.findByText('fixture/scan-b')).closest('tr')!;
    expect(within(secondRow).getByRole('checkbox')).not.toBeChecked();
    expect(screen.queryByRole('button', { name: /删除选中的孤立对象/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('scan-a'));
    const returned = (await screen.findByText('fixture/scan-a')).closest('tr')!;
    expect(within(returned).getByRole('checkbox')).not.toBeChecked();
    expect(screen.queryByRole('button', { name: /删除选中的孤立对象/ })).not.toBeInTheDocument();
    expect(mocks.deleteOrphans).not.toHaveBeenCalled();
  });
  it('submits only the explicitly selected current scan issue after switching from a previous selection', async () => {
    useTwoScans(); let finish!: (value: ReturnType<typeof scanResult>) => void;
    mocks.reconciliationRun.mockImplementation((id: string) => id === 'scan-b'
      ? new Promise<ReturnType<typeof scanResult>>(resolve => { finish = resolve; })
      : Promise.resolve(scanResult(id)));
    mount(<Storage />); fireEvent.click(screen.getByRole('tab', { name: /一致性核对/ }));
    fireEvent.click(await screen.findByText('scan-a'));
    fireEvent.click(within((await screen.findByText('fixture/scan-a')).closest('tr')!).getByRole('checkbox'));
    fireEvent.click(screen.getByText('scan-b'));
    expect(screen.queryByRole('button', { name: /删除选中的孤立对象/ })).not.toBeInTheDocument();
    expect(mocks.deleteOrphans).not.toHaveBeenCalled();
    await act(async () => { finish(scanResult('scan-b')); });
    const secondRow = (await screen.findByText('fixture/scan-b')).closest('tr')!;
    expect(within(secondRow).getByRole('checkbox')).not.toBeChecked();
    fireEvent.click(within(secondRow).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: /删除选中的孤立对象/ }));
    expect(await screen.findByTestId('destination')).toHaveTextContent('/jobs/delete-job');
    expect(mocks.deleteOrphans).toHaveBeenCalledExactlyOnceWith(['issue-scan-b']);
    expect(confirm).not.toHaveBeenCalled();
  });
  it('disables cleanup during detail refresh and drops issues that are no longer open', async () => {
    const { client } = mount(<Storage />); fireEvent.click(screen.getByRole('tab', { name: /一致性核对/ }));
    fireEvent.click(await screen.findByText('scan-one'));
    fireEvent.click(within((await screen.findByText('fixture/orphan')).closest('tr')!).getByRole('checkbox'));
    const button = screen.getByRole('button', { name: /删除选中的孤立对象/ });
    let finish!: (value: { id: string; status: string; issues: { id: string; type: string; status: string; objectKey: string }[] }) => void;
    mocks.reconciliationRun.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    act(() => { void client.invalidateQueries({ queryKey: ['storage', 'run', 'scan-one'] }); });
    await waitFor(() => expect(button).toBeDisabled()); fireEvent.click(button);
    expect(mocks.deleteOrphans).not.toHaveBeenCalled();
    await act(async () => { finish({ id: 'scan-one', status: 'COMPLETED', issues: [{ id: 'orphan', type: 'ORPHAN_OBJECT', status: 'RESOLVED', objectKey: 'fixture/orphan' }] }); });
    await waitFor(() => expect(screen.queryByRole('button', { name: /删除选中的孤立对象/ })).not.toBeInTheDocument());
    expect(mocks.deleteOrphans).not.toHaveBeenCalled();
  });

});
