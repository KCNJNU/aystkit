import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Outlet, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Datasets } from './Datasets';
import { api } from '../services/api';
import { selectRobot } from '../services/robot-selection';
import { useRobotSelection } from '../components/useRobotSelection';
vi.mock('../services/api', () => ({ api: { fleetRobots: vi.fn(), datasetPage: vi.fn(), tags: vi.fn(), version: vi.fn() } }));
vi.mock('../components/SavedViews', () => ({ SavedViews: () => null }));
vi.mock('../components/DatasetDrawers', () => ({ CreateDatasetModal: () => null }));
vi.mock('../components/DatasetCard', () => ({ DatasetCard: ({ dataset, robotName }: { dataset: { name: string }; robotName?: string }) => <article>{dataset.name}{robotName && <span>归属 {robotName}</span>}</article> }));
const a='00000000-0000-4000-8000-000000000001',b='00000000-0000-4000-8000-000000000002';
const clients:QueryClient[]=[];
function Layout(){const scope=useRobotSelection();const nav=useNavigate();return <><button onClick={()=>nav(-1)}>返回</button>{!scope.transitioning&&<Outlet key={scope.robotId}/>}</>;}
beforeEach(()=>{
 vi.clearAllMocks();sessionStorage.clear();selectRobot('');
 vi.mocked(api.fleetRobots).mockResolvedValue([{id:a,name:'一号车'},{id:b,name:'二号车'}]);vi.mocked(api.tags).mockResolvedValue([]);
 vi.mocked(api.datasetPage).mockImplementation(async query=>{const id=typeof query.robotId==='string'?query.robotId:undefined;return {items:[{id:id||'legacy',name:id===a?'一号车视频':id===b?'二号车视频':'历史数据集',metadata:id?{robotId:id}:{},versions:[],latestVersion:null,versionCount:0,createdAt:'2026-11-09',updatedAt:'2026-11-09'}],total:1,page:1,pageSize:12};});
});
afterEach(()=>clients.splice(0).forEach(client=>client.clear()));
it('shows separate vehicle datasets and restores the category with browser history',async()=>{
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});clients.push(client);
 render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/datasets?robotId=all']}><Routes><Route element={<Layout/>}><Route path="/datasets" element={<Datasets/>}/></Route></Routes></MemoryRouter></QueryClientProvider>);
 await screen.findByText('历史数据集');
 const category=()=>screen.getByRole('navigation',{name:'按机器人查看数据集'});
 fireEvent.click(within(category()).getByRole('button',{name:'一号车'}));
 await screen.findByText('一号车视频');expect(screen.queryByText('历史数据集')).not.toBeInTheDocument();
 expect(screen.getByText('归属 一号车')).toBeVisible();
 expect(within(category()).getByRole('button',{name:'一号车'})).toHaveAttribute('aria-pressed','true');
 fireEvent.click(within(category()).getByRole('button',{name:'二号车'}));
 await screen.findByText('二号车视频');expect(screen.queryByText('一号车视频')).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'返回'}));await screen.findByText('一号车视频');
 expect(api.datasetPage).toHaveBeenCalledWith(expect.objectContaining({robotId:a}));
 fireEvent.click(within(category()).getByRole('button',{name:'全部机器人'}));await screen.findByText('历史数据集');
 await waitFor(()=>expect(api.datasetPage).toHaveBeenCalledWith(expect.objectContaining({robotId:undefined})));
 expect(screen.queryByText(/归属/)).not.toBeInTheDocument();
});
