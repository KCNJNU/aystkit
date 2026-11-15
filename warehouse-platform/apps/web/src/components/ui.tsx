import { ApiOutlined, AuditOutlined, BranchesOutlined, CloudServerOutlined, DatabaseOutlined, FileOutlined, FolderOpenOutlined, HddOutlined, InboxOutlined, SettingOutlined, TeamOutlined, UploadOutlined } from '@ant-design/icons';
import { Alert, Card, Empty, Flex, Skeleton, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';
import type { DatasetVersion, HealthStatus } from '../services/api';

export type ServiceState = HealthStatus;

const statusConfig: Record<ServiceState, { tone: StatusTone; label: string }> = {
  healthy: { tone: 'success', label: '健康' },
  degraded: { tone: 'warning', label: '降级' },
  unhealthy: { tone: 'error', label: '异常' },
  not_checked: { tone: 'neutral', label: '未检查' },
};

export function StatusBadge({ status, label }: { status: ServiceState; label?: string }) {
  const config = statusConfig[status];
  return <StatusTag status={status} label={label ?? config.label} tone={config.tone} />;
}

type StatusTone = 'info' | 'success' | 'warning' | 'error' | 'neutral';
const statePresentation: Record<string, { label: string; tone: StatusTone }> = {
  RUNNING: { label: '运行中', tone: 'info' },
  COMPLETED: { label: '已完成', tone: 'success' }, SUCCEEDED: { label: '成功', tone: 'success' }, FAILED: { label: '失败', tone: 'error' },
  CANCELLED: { label: '已取消', tone: 'neutral' }, QUEUED: { label: '排队中', tone: 'neutral' }, PENDING: { label: '待处理', tone: 'warning' },
  UPLOADING: { label: '上传中', tone: 'info' }, HASHING: { label: '计算校验和', tone: 'info' }, CHECKING_DUPLICATE: { label: '检查重复', tone: 'info' },
  FINALIZING: { label: '正在归档', tone: 'info' }, DUPLICATE: { label: '重复文件', tone: 'warning' },
  DRAFT: { label: '草稿', tone: 'warning' }, VALIDATING: { label: '校验中', tone: 'info' }, FROZEN: { label: '已冻结', tone: 'info' },
  PUBLISHED: { label: '已发布', tone: 'success' }, ARCHIVED: { label: '已归档', tone: 'neutral' },
  OPEN: { label: '待处理', tone: 'warning' }, IGNORED: { label: '已忽略', tone: 'neutral' }, RESOLVED: { label: '已解决', tone: 'success' },
  ERROR: { label: '异常', tone: 'error' }, WARNING: { label: '警告', tone: 'warning' }, INFO: { label: '信息', tone: 'info' },
};

export function StatusTag({ status, label, tone }: { status?: unknown; label?: string; tone?: StatusTone }) {
  const raw = String(status ?? 'UNKNOWN');
  const presentation = statePresentation[raw.toUpperCase()] ?? { label: raw === 'UNKNOWN' ? '未知' : raw, tone: 'neutral' as const };
  const resolvedTone = tone ?? presentation.tone;
  return <Tag bordered={false} title={raw} aria-label={`${label ?? presentation.label} (${raw})`} className={`status-tag status-tone-${resolvedTone} status-${raw.toLowerCase()}`}><span className="status-dot" />{label ?? presentation.label}</Tag>;
}

export function VersionTag({ status }: { status: DatasetVersion['status'] }) {
  return <StatusTag status={status} />;
}

export function PageHeader({ title, eyebrow, actions }: { title: string; eyebrow?: string; description?: string | null; actions?: ReactNode }) {
  const identities: Record<string, ReactNode> = {
    数据集: <FolderOpenOutlined />, 文件资产: <FileOutlined />, 上传中心: <UploadOutlined />,
    数据集版本: <BranchesOutlined />, 数据版本: <BranchesOutlined />,
    任务与审计: <AuditOutlined />, 数据处理任务: <AuditOutlined />, 存储管理: <HddOutlined />,
    系统状态: <CloudServerOutlined />, 系统设置: <SettingOutlined />, 用户管理: <TeamOutlined />,
    文件回收站: <InboxOutlined />, 活动记录: <AuditOutlined />,
  };
  const identity = eyebrow?.includes('版本') ? <BranchesOutlined /> : identities[title] ?? <DatabaseOutlined />;
  return <div className="page-header">
    <div className="page-header-copy">
      <span className="page-identity-icon" aria-hidden="true">{identity}</span>
      <div className="page-header-text">
        <Typography.Title level={2}>{title}</Typography.Title>
      </div>
    </div>
    {actions && <Flex className="page-header-actions" gap={8} wrap="wrap">{actions}</Flex>}
  </div>;
}

const statIcons = {
  api: <ApiOutlined />,
  database: <DatabaseOutlined />,
  storage: <FolderOpenOutlined />,
};

export function StatCard({ title, value, suffix, caption, tone = 'default', icon }: { title: string; value: ReactNode; suffix?: string; caption?: ReactNode; tone?: 'default' | 'success' | 'warning' | 'error'; icon?: keyof typeof statIcons }) {
  return <Card className={`stat-card stat-card-${tone}`} variant="borderless">
    <Flex justify="space-between" align="flex-start">
      <div>
        <Typography.Text type="secondary">{title}</Typography.Text>
        <div className="stat-value">{value}{suffix && <span>{suffix}</span>}</div>
      </div>
      {icon && <div className="stat-icon">{statIcons[icon]}</div>}
    </Flex>
    {caption && <div className="stat-caption">{caption}</div>}
  </Card>;
}

export function DataState({ loading, error, empty, emptyTitle, action, children }: { loading: boolean; error?: string; empty: boolean; emptyTitle: string; emptyDescription?: string; action?: ReactNode; children: ReactNode }) {
  if (loading) return <Card variant="borderless"><Skeleton active paragraph={{ rows: 6 }} /></Card>;
  if (error) return <Alert type="error" showIcon message="数据加载失败" description={error} action={action} />;
  if (empty) return <Card variant="borderless" className="empty-panel"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<Typography.Title level={4}>{emptyTitle}</Typography.Title>}>{action}</Empty></Card>;
  return <>{children}</>;
}
