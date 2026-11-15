import { validationMessages } from '../services/labels';
import { CheckCircleOutlined, LockOutlined, ReloadOutlined, RocketOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Col, Row, Space, Table, Tag, Typography, message } from 'antd';
import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type DatasetVersion, type ValidationReport } from '../services/api';
import { bytes, date } from '../services/format';
import { StatCard } from './ui';
import { useOptionalAuth } from '../auth/AuthContext';

export function VersionValidation({ version, onChanged }: { version: DatasetVersion; onChanged: () => void }) {
  const auth = useOptionalAuth(); const canOperate = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR') : true;
  const canPublish = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN') : true;
  const [report, setReport] = useState<ValidationReport>();
  const [operation, setOperation] = useState<'freeze' | 'publish' | 'recovery'>();
  const running = useRef(false); const queryClient = useQueryClient();
  const navigate = useNavigate();
  const validate = useMutation({ mutationFn: () => api.validateVersion(version.id), onSuccess: setReport, onError: (reason: Error) => message.error(reason.message) });
  const refreshResources = async () => { await Promise.all([queryClient.invalidateQueries({ queryKey: ['datasets'] }), queryClient.invalidateQueries({ queryKey: ['versions'] }), queryClient.invalidateQueries({ queryKey: ['dashboard'] })]); onChanged(); };
  const execute = async (kind: 'freeze' | 'publish' | 'recovery', action: () => Promise<void>) => {
    if (running.current || validate.isPending || !canPublish) return;
    running.current = true; setOperation(kind);
    try { await action(); }
    catch (reason) { message.error((reason as Error).message); }
    finally { running.current = false; setOperation(undefined); }
  };
  const freeze = () => execute('freeze', async () => {
    if (version.status !== 'DRAFT') return;
    const validation = await api.validateVersion(version.id); setReport(validation);
    if (!validation.valid) { message.error('校验失败，请解决全部错误后再冻结。'); return; }
    await api.freeze(version.id); message.success('版本已冻结'); await refreshResources();
  });
  const publish = () => execute('publish', async () => {
    if (version.status !== 'FROZEN') return;
    const validation = await api.validateVersion(version.id); setReport(validation);
    if (!validation.valid) { message.error('校验失败，暂时无法发布。'); return; }
    const job = await api.publishJob(version.id); message.success('发布任务已加入队列'); navigate(`/jobs/${job.id}`);
  });
  const recovery = () => execute('recovery', async () => {
    if (version.status !== 'PUBLISHED') return;
    const job = await api.recoveryJob(version.id); message.success('恢复校验已加入队列'); navigate(`/jobs/${job.id}`);
  });
  return <div className="validation-workspace">
      <div className="builder-toolbar"><Typography.Title level={4}>完整性校验</Typography.Title>{canOperate && <Space><Button icon={<ReloadOutlined />} loading={validate.isPending} disabled={Boolean(operation)} onClick={() => validate.mutate()}>{validate.isPending ? '正在检查对象' : '开始校验'}</Button>{canPublish && version.status === 'DRAFT' && <Button type="primary" icon={<LockOutlined />} loading={operation === 'freeze'} disabled={validate.isPending || Boolean(operation && operation !== 'freeze')} onClick={() => void freeze()}>{operation === 'freeze' ? '正在检查完整性' : '校验并冻结'}</Button>}{canPublish && version.status === 'FROZEN' && <Button type="primary" icon={<RocketOutlined />} loading={operation === 'publish'} disabled={validate.isPending || Boolean(operation && operation !== 'publish')} onClick={() => void publish()}>发布数据版本</Button>}{canPublish && version.status === 'PUBLISHED' && <Button icon={<SafetyCertificateOutlined />} loading={operation === 'recovery'} disabled={validate.isPending || Boolean(operation && operation !== 'recovery')} onClick={() => void recovery()}>恢复校验</Button>}</Space>}</div>
    {!canOperate && <Alert className="validation-result" type="info" showIcon message="只读权限" description="校验需要二级及以上权限；冻结、发布与恢复需要项目管理员及以上权限。" />}
    {!report ? <Card variant="borderless" className="validation-empty"><SafetyCertificateOutlined /><Typography.Title level={4}>暂无校验报告</Typography.Title></Card> : <>
      <Alert className="validation-result" showIcon type={report.valid ? 'success' : 'error'} message={report.valid ? '版本完整性校验通过' : '版本存在阻止后续操作的完整性错误'} description={`校验时间：${date(report.validatedAt)} · 已读取 ${report.summary.checkedObjects}/${report.summary.files} 个文件对象`} />
      <Row gutter={[12, 12]} className="summary-row"><Col xs={12} lg={6}><StatCard title="文件" value={report.summary.files} /></Col><Col xs={12} lg={6}><StatCard title="已校验数据" value={bytes(report.summary.totalSize)} /></Col><Col xs={12} lg={6}><StatCard title="错误" value={report.summary.errors} tone={report.summary.errors ? 'error' : 'success'} /></Col><Col xs={12} lg={6}><StatCard title="警告" value={report.summary.warnings} tone={report.summary.warnings ? 'warning' : 'default'} /></Col></Row>
      <Card variant="borderless" className="table-panel"><Table size="small" rowKey={(issue) => `${issue.code}:${issue.fileId ?? issue.objectKey ?? 'version'}`} dataSource={report.issues} pagination={false} locale={{ emptyText: <span><CheckCircleOutlined /> 未发现完整性问题</span> }} columns={[{ title: "严重程度", dataIndex: 'severity', width: 105, render: (value) => <Tag color={value === 'error' ? 'error' : 'warning'}>{String(value).toUpperCase()}</Tag> }, { title: "代码", dataIndex: 'code', width: 220, render: (value) => <Typography.Text code>{value}</Typography.Text> }, { title: "消息", dataIndex: 'message', render: (value: string, row) => <span title={value}>{validationMessages[row.code] ?? value}</span> }, { title: "存储对象", dataIndex: 'objectKey', width: 280, ellipsis: true, render: (value) => value ?? '--' }]} /></Card>
    </>}
  </div>;
}
