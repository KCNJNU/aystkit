import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { CreateDatasetModal } from './DatasetDrawers';
import { api } from '../services/api';
import { selectRobot } from '../services/robot-selection';
vi.mock('../services/api',()=>({api:{warehouseProjects:vi.fn(),fleetRobots:vi.fn(),createDataset:vi.fn()}}));
const a='00000000-0000-4000-8000-000000000001',p='00000000-0000-4000-8000-000000000010';
beforeEach(()=>{
 vi.clearAllMocks();selectRobot(a);
 vi.mocked(api.warehouseProjects).mockResolvedValue([{id:p,name:'地库项目',tenantId:'tenant',tenant:{name:'车队'}}]);
 vi.mocked(api.fleetRobots).mockResolvedValue([{id:a,name:'一号车',projectId:p}]);
 vi.mocked(api.createDataset).mockResolvedValue({id:'created',name:'新视频',createdAt:'2026-11-09',updatedAt:'2026-11-09',versions:[]});
});
it('creates a dataset with the selected car and its project without creating an unassigned dataset',async()=>{
 const onDone=vi.fn();render(<CreateDatasetModal open onClose={()=>{}} onDone={onDone}/>);
 const dialog=await screen.findByRole('dialog');await within(dialog).findByText('一号车');
 fireEvent.change(within(dialog).getByLabelText('数据集名称'),{target:{value:'新视频'}});
 const submit=within(dialog).getByRole('button',{name:'创建数据集'});await waitFor(()=>expect(submit).not.toBeDisabled());fireEvent.click(submit);
 await waitFor(()=>expect(api.createDataset).toHaveBeenCalledWith(expect.objectContaining({name:'新视频',robotId:a,projectId:p})));
 await waitFor(()=>expect(onDone).toHaveBeenCalledOnce());
});
