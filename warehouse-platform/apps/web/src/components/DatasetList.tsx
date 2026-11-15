import { ArrowRightOutlined, FolderOpenOutlined, HistoryOutlined, RobotOutlined } from '@ant-design/icons';
import { Button, Grid, Space, Table, Tag } from 'antd';
import type { Dataset } from '../services/api';
import { datasetRobotId } from '../services/dataset-robot';
import { bytes, date } from '../services/format';
import { VersionTag } from './ui';

export function DatasetList({ datasets, robotNames, onOpen, onVersion }: {
  datasets: Dataset[]; robotNames: Map<string, string>; onOpen: (id: string) => void;
  onVersion: (datasetId: string, versionId: string) => void;
}) {
  const screens = Grid.useBreakpoint();
  if (screens.md === false) return <ul className="dataset-mobile-list" aria-label="数据集列表视图">{datasets.map(dataset => {
    const robot = robotNames.get(datasetRobotId(dataset.metadata) ?? '');
    const latest = dataset.versions[0];
    return <li key={dataset.id}>
      <div className="dataset-list-identity"><span className="dataset-list-icon"><FolderOpenOutlined /></span><div>
        <button className="dataset-list-name" onClick={() => onOpen(dataset.id)}>{dataset.name}</button>
        {robot && <Tag className="dataset-robot-label" icon={<RobotOutlined />}>{robot}</Tag>}
      </div></div>
      <div className="dataset-mobile-facts">{latest ? <><VersionTag status={latest.status} /><span>v{latest.version}</span><span>{latest.fileCount.toLocaleString()} 个文件</span><span>{bytes(latest.totalSize)}</span></> : <span>暂无版本</span>}</div>
      <div className="dataset-mobile-list-footer"><time dateTime={dataset.updatedAt}>{date(dataset.updatedAt)}</time><Space size={2}>
        <Button type="link" onClick={() => onOpen(dataset.id)}>打开文件<ArrowRightOutlined /></Button>
        <Button type="text" icon={<HistoryOutlined />} aria-label={`查看最新版本 ${dataset.name}`} title="查看最新版本"
          disabled={!dataset.versions[0]} onClick={() => dataset.versions[0] && onVersion(dataset.id, dataset.versions[0].id)} />
      </Space></div>
    </li>;
  })}</ul>;
  return <div className="dataset-list-surface"><Table<Dataset> className="dataset-list-table" rowKey="id"
    dataSource={datasets} pagination={false} scroll={{ x: 660 }} size="middle" columns={[
      { title: '数据集名称', key: 'name', render: (_, dataset) => {
        const robot = robotNames.get(datasetRobotId(dataset.metadata) ?? '');
        return <div className="dataset-list-identity"><span className="dataset-list-icon"><FolderOpenOutlined /></span>
          <div><button className="dataset-list-name" title={dataset.name} onClick={() => onOpen(dataset.id)}>{dataset.name}</button>
            {dataset.description && <span className="dataset-list-description" title={dataset.description}>{dataset.description}</span>}
            {robot && <Tag className="dataset-robot-label" icon={<RobotOutlined />}>{robot}</Tag>}
          </div></div>;
      } },
      { title: '最新版本', key: 'version', width: 150, responsive: ['md'], render: (_, dataset) => dataset.versions[0]
        ? <Space size={6}><span className="dataset-list-version">v{dataset.versions[0].version}</span><VersionTag status={dataset.versions[0].status} /></Space> : '—' },
      { title: '版本文件', key: 'files', className: 'dataset-numeric-cell', width: 100, responsive: ['xl'], render: (_, dataset) => dataset.versions[0]?.fileCount.toLocaleString() ?? '—' },
      { title: '版本容量', key: 'size', className: 'dataset-numeric-cell', width: 110, responsive: ['xl'], render: (_, dataset) => dataset.versions[0] ? bytes(dataset.versions[0].totalSize) : '—' },
      { title: '更新时间', dataIndex: 'updatedAt', width: 170, responsive: ['md'], render: date },
      { title: '操作', key: 'actions', width: 200, fixed: 'right', render: (_, dataset) => <Space size={2}>
        <Button type="link" icon={<ArrowRightOutlined />} onClick={() => onOpen(dataset.id)}>打开文件</Button>
        <Button type="text" icon={<HistoryOutlined />} aria-label={`查看最新版本 ${dataset.name}`} title="查看最新版本"
          disabled={!dataset.versions[0]} onClick={() => dataset.versions[0] && onVersion(dataset.id, dataset.versions[0].id)} />
      </Space> },
    ]} /></div>;
}
