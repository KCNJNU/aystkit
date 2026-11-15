import { AppstoreOutlined, CheckOutlined, RobotOutlined } from '@ant-design/icons';
import { Button } from 'antd';
import type { RobotSummary } from '../services/api';

export function DatasetRobotCategories({ robots, selected, loading, error, onSelect, onRetry }: {
  robots: RobotSummary[]; selected: string; loading: boolean; error: boolean;
  onSelect: (id: string) => void; onRetry: () => void;
}) {
  return <div className="dataset-robot-scope">
    <div className="dataset-scope-heading"><span><RobotOutlined />机器人分类</span><span>{loading && !robots.length ? '读取中' : error ? '读取失败' : `${robots.length} 台机器人`}</span></div>
    <nav className="dataset-robot-categories" aria-label="按机器人查看数据集" aria-busy={loading}>
    <button type="button" aria-label="全部机器人" className={`dataset-category-all ${!selected ? 'is-selected' : ''}`} aria-pressed={!selected} onClick={() => onSelect('')}>
      <AppstoreOutlined /><span className="dataset-category-identity"><strong>全部机器人</strong></span>{!selected && <CheckOutlined className="dataset-category-check" />}
    </button>
    {robots.map(robot => <button type="button" key={robot.id} className={selected === robot.id ? 'is-selected' : ''}
      aria-label={robot.name || robot.externalId || robot.id} aria-pressed={selected === robot.id} title={robot.name} onClick={() => onSelect(robot.id)}>
      <RobotOutlined /><span className="dataset-category-identity"><strong>{robot.name || robot.externalId || robot.id}</strong>{robot.externalId && robot.externalId !== robot.name && <small title={robot.externalId}>{robot.externalId}</small>}</span>{selected === robot.id && <CheckOutlined className="dataset-category-check" />}
    </button>)}
    {loading && !robots.length && <span className="robot-category-status" role="status">正在读取机器人</span>}
    {error && <Button type="text" danger onClick={onRetry}>重新读取机器人</Button>}
  </nav></div>;
}
