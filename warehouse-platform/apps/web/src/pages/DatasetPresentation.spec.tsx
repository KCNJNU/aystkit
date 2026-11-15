import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigProvider } from 'antd';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Datasets } from './Datasets';
import { api } from '../services/api';
import { selectedRobot, selectRobot } from '../services/robot-selection';

const savedView = vi.hoisted(() => ({ query: {} as Record<string, string | number | boolean> }));

vi.mock('../services/api', () => ({ api: { fleetRobots: vi.fn(), datasetPage: vi.fn(), tags: vi.fn(), version: vi.fn() } }));
vi.mock('../components/DatasetDrawers', () => ({ CreateDatasetModal: () => null }));
vi.mock('../components/SavedViews', () => ({ SavedViews: ({ onApply }: { onApply: (query: Record<string, string | number | boolean>) => void }) => <button onClick={() => onApply(savedView.query)}>应用测试保存视图</button> }));
vi.mock('../components/DatasetCard', () => ({ DatasetCard: ({ dataset }: { dataset: { name: string } }) => <article aria-label="数据集卡片">{dataset.name}</article> }));
const clients: QueryClient[] = [];
const robotId = '00000000-0000-4000-8000-000000000001';
const name = '地库_2026-11-09';

beforeEach(() => {
  vi.clearAllMocks(); localStorage.removeItem('warehouse-dataset-view'); sessionStorage.clear(); selectRobot('');
  savedView.query = {};
  vi.mocked(api.fleetRobots).mockResolvedValue([{ id: robotId, name: '一号车' }]);
  vi.mocked(api.tags).mockResolvedValue([]);
  vi.mocked(api.datasetPage).mockResolvedValue({ items: [{ id: 'dataset-one', name, metadata: { robotId }, versions: [], latestVersion: null, versionCount: 0, createdAt: '2026-11-09', updatedAt: '2026-11-09' }], total: 1, page: 1, pageSize: 12 });
});
afterEach(() => { clients.splice(0).forEach(client => client.clear()); localStorage.removeItem('warehouse-dataset-view'); });

function Location() { const location = useLocation(); return <output aria-label="当前路由">{location.pathname}{location.search}|{(location.state as { datasetsReturnTo?: string } | null)?.datasetsReturnTo}</output>; }
function mount(initialEntry = '/datasets?search=garage&robotId=all') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  return render(<ConfigProvider theme={{ token: { motion: false } }}><QueryClientProvider client={client}><MemoryRouter initialEntries={[initialEntry]}><Location /><Routes>
    <Route path="/datasets" element={<Datasets />} /><Route path="/datasets/:datasetId" element={<div>目录</div>} />
  </Routes></MemoryRouter></QueryClientProvider></ConfigProvider>);
}

it('switches to a list without discarding filters and remembers that display preference', async () => {
  const first = mount(); await screen.findByRole('article', { name: '数据集卡片' });
  fireEvent.click(screen.getByRole('button', { name: '列表视图' }));
  expect(screen.getByRole('button', { name: '列表视图' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.queryByRole('article', { name: '数据集卡片' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name })).toBeVisible();
  expect(screen.getByLabelText('当前路由')).toHaveTextContent('/datasets?search=garage&robotId=all');
  expect(localStorage.getItem('warehouse-dataset-view')).toBe('list');
  first.unmount(); mount();
  expect(await screen.findByRole('button', { name })).toBeVisible();
  expect(screen.getByRole('button', { name: '列表视图' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getByRole('button', { name }));
  expect(screen.getByLabelText('当前路由')).toHaveTextContent('/datasets/dataset-one|/datasets?search=garage&robotId=all');
});

it('can refresh in place while keeping the selected display mode', async () => {
  localStorage.setItem('warehouse-dataset-view', 'list'); mount();
  await screen.findByRole('button', { name });
  fireEvent.click(screen.getByRole('button', { name: '刷新数据集' }));
  expect(api.datasetPage).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('button', { name: '列表视图' })).toHaveAttribute('aria-pressed', 'true');
});

it.each(['00000000-0000-4000-8000-000000000002', ''])('keeps the current robot when a saved view contains robotId=%s', async savedRobotId => {
  selectRobot(robotId);
  savedView.query = { robotId: savedRobotId, search: '标定图像', status: 'FROZEN', page: 8, pageSize: 24, sortBy: 'name', sortOrder: 'asc' };
  mount(`/datasets?robotId=${robotId}&page=3&search=old&status=DRAFT`);
  await screen.findByRole('article', { name: '数据集卡片' });
  expect(api.datasetPage).toHaveBeenLastCalledWith(expect.objectContaining({ robotId, page: 3, search: 'old', status: 'DRAFT' }));

  fireEvent.click(screen.getByRole('button', { name: '应用测试保存视图' }));

  await waitFor(() => expect(api.datasetPage).toHaveBeenLastCalledWith(expect.objectContaining({
    robotId, page: 1, pageSize: 24, search: '标定图像', status: 'FROZEN', sortBy: 'name', sortOrder: 'asc',
  })));
  const route = screen.getByLabelText('当前路由').textContent!.split('|')[0];
  const restored = new URL(route, 'https://warehouse.test').searchParams;
  expect(restored.get('robotId')).toBe(robotId);
  expect(restored.get('page')).toBe('1');
  expect(restored.get('search')).toBe('标定图像');
  expect(restored.get('status')).toBe('FROZEN');
  expect(restored.get('sort')).toBe('name:asc');
  expect(restored.has('sortBy')).toBe(false);
  expect(restored.has('sortOrder')).toBe(false);
  expect(selectedRobot()).toBe(robotId);
});
