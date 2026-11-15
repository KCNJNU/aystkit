import { AuditOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { Result, Tabs } from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { auditRoles } from '../auth/permissions';
import { AuditLogs } from './AuditLogs';
import { Jobs } from './Jobs';
import './admin-workspace.css';

export function TasksAndAudit() {
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const canAudit = auth.hasRole(...auditRoles);
  const activeTab = location.pathname === '/audit-logs' ? 'audit' : 'jobs';

  // The routes enforce their respective permissions. Keep the audit content
  // guarded here too so an unauthorized tab never mounts or requests records.
  if (activeTab === 'audit' && !canAudit) {
    return <Result status="403" title="权限不足" subTitle="当前角色无权访问审计日志。" />;
  }

  return <Tabs
    className="admin-tabs"
    aria-label="任务与审计"
    activeKey={activeTab}
    onChange={(key) => navigate(key === 'audit' ? '/audit-logs' : '/jobs')}
    items={[
      {
        key: 'jobs',
        label: '数据处理任务',
        icon: <ClockCircleOutlined />,
        children: activeTab === 'jobs' ? <Jobs /> : null,
      },
      ...(canAudit ? [{
        key: 'audit',
        label: '审计日志',
        icon: <AuditOutlined />,
        children: activeTab === 'audit' ? <AuditLogs /> : null,
      }] : []),
    ]}
  />;
}
