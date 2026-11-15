import '../pages/data-workspace.css';
import { ArrowRightOutlined, DatabaseOutlined, HistoryOutlined, RobotOutlined } from '@ant-design/icons';
import { Button, Card, Flex, Tag, Typography } from 'antd';
import type { Dataset } from '../services/api';
import { bytes, date } from '../services/format';
import { DeleteDatasetButton } from './DeleteDatasetButton';
import { VersionTag } from './ui';

export function DatasetCard({ dataset, onOpen, onVersion, canDelete = false, robotName }: { canDelete?: boolean; dataset: Dataset; onOpen: () => void; onVersion: (id: string) => void; robotName?: string }) {
  const latest = dataset.versions[0];
  return <Card variant="borderless" className="dataset-card" actions={[
    <Button type="link" key="manage" onClick={onOpen}>打开文件 <ArrowRightOutlined /></Button>,
    <Button type="link" key="history" icon={<HistoryOutlined />} disabled={!latest} onClick={() => latest && onVersion(latest.id)}>查看最新版本</Button>,
  ]}>
    <div className="dataset-card-topline">{robotName ? <Tag className="dataset-robot-label" icon={<RobotOutlined />}>{robotName}</Tag> : <span className="dataset-card-kind"><DatabaseOutlined />数据集</span>}{latest ? <VersionTag status={latest.status} /> : <Tag bordered={false}>暂无版本</Tag>}</div>
    <div className="dataset-card-heading">
      <span className="dataset-icon"><DatabaseOutlined /></span>
      <Typography.Title level={4}><button className="dataset-title-link" title={dataset.name} onClick={onOpen}>{dataset.name}</button></Typography.Title>
      {canDelete && <span className="dataset-card-delete" title={`删除数据集 ${dataset.name}`}><DeleteDatasetButton dataset={dataset} /></span>}
    </div>
    {dataset.description && <Typography.Paragraph type="secondary" className="dataset-description" ellipsis={{ rows: 1, tooltip: dataset.description }}>{dataset.description}</Typography.Paragraph>}
    <div className="dataset-stats">
      <div><span>最新版本</span><strong>{latest ? `v${latest.version}` : '--'}</strong></div>
      <div><span>版本文件</span><strong>{latest?.fileCount ?? '—'}</strong></div>
      <div><span>版本容量</span><strong>{latest ? bytes(latest.totalSize) : '—'}</strong></div>
    </div>
    <Flex align="center" justify="space-between" gap={6} className="dataset-card-footer"><Typography.Text type="secondary" className="dataset-updated"><HistoryOutlined /><time dateTime={dataset.updatedAt}>{date(dataset.updatedAt)}</time></Typography.Text><span className="dataset-updated-label">更新</span></Flex>
  </Card>;
}
