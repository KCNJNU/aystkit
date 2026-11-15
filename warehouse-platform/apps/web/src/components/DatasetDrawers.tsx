import { DeleteOutlined, LockOutlined, RocketOutlined } from '@ant-design/icons';
import { Button, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Timeline, Typography, message } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, type DatasetVersion, type RobotFile, type RobotSummary } from '../services/api';
import { selectedRobot } from '../services/robot-selection';
import { bytes, date, shortHash } from '../services/format';
import { DataState, VersionTag } from './ui';
import { useOptionalAuth } from '../auth/AuthContext';

export function CreateDatasetModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [projects, setProjects] = useState<Array<{ id: string; name: string; tenant: { name: string } }>>([]);
  const [robots, setRobots] = useState<RobotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const projectId = Form.useWatch('projectId', form);
  useEffect(() => {
    if (!open) return;
    let active = true; setLoading(true); setLoadFailed(false);
    void Promise.all([api.warehouseProjects(), api.fleetRobots()]).then(([items, cars]) => {
      if (!active) return;
      setProjects(items); setRobots(cars);
      const preferred = cars.find(car => car.id === selectedRobot()) ?? (cars.length === 1 ? cars[0] : undefined);
      if (preferred) form.setFieldsValue({ robotId: preferred.id, projectId: preferred.projectId || (items.length === 1 ? items[0].id : undefined) });
      else if (items.length === 1) form.setFieldValue('projectId', items[0].id);
    }).catch(() => { if (active) { setLoadFailed(true); message.error('无法读取机器人或项目'); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open, form]);
  const submit = async () => {
    const values = await form.validateFields(); setSaving(true);
    api.createDataset(values).then(() => { message.success('数据集已创建'); onClose(); onDone(); }).catch((reason: Error) => message.error(reason.message)).finally(() => setSaving(false));
  };
  return <Modal open={open} title="创建数据集" okText="创建数据集" confirmLoading={saving} okButtonProps={{ disabled: loading || loadFailed }} onCancel={onClose} onOk={() => void submit()} afterOpenChange={(visible) => { if (!visible) form.resetFields(); }}><Form form={form} layout="vertical"><Form.Item name="name" label="数据集名称" rules={[{ required: true, whitespace: true, message: '请输入数据集名称' }, { pattern: /^[^/\\]+$/, message: '名称不能包含斜杠' }]}><Input placeholder="输入数据集名称" /></Form.Item><Form.Item name="robotId" label="所属机器人" rules={[{ required: robots.length > 0, message: '请选择所属机器人' }]}><Select loading={loading} disabled={loading} placeholder="选择机器人" showSearch optionFilterProp="label" options={robots.filter(robot => !projectId || robot.projectId === projectId).map(robot => ({ value: robot.id, label: robot.name }))} onChange={id => { const robot = robots.find(car => car.id === id); if (robot?.projectId) form.setFieldValue('projectId', robot.projectId); }} /></Form.Item><Form.Item name="projectId" label="所属数据项目" rules={[{ required: true, message: '请选择数据集所属项目' }]}><Select loading={loading} disabled={loading} options={projects.map(project => ({ value: project.id, label: `${project.tenant.name} / ${project.name}` }))} onChange={id => { const robot = robots.find(car => car.id === form.getFieldValue('robotId')); if (robot && robot.projectId !== id) form.setFieldValue('robotId', undefined); }} /></Form.Item><Form.Item name="description" label="说明"><Input.TextArea rows={4} maxLength={500} showCount placeholder="用途、来源与使用说明" /></Form.Item></Form></Modal>;
}

export function VersionDrawer({ versionId, datasetName, onClose, onChanged }: { versionId?: string; datasetName?: string; onClose: () => void; onChanged: () => void }) {
  const auth = useOptionalAuth();
  const canMutate = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR') : true;
  const canPublish = auth ? auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN') : true;
  const [pending, setPending] = useState<string>(); const running = useRef(false);
  const [version, setVersion] = useState<DatasetVersion>();
  const [files, setFiles] = useState<RobotFile[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string>();
  const load = useCallback(() => versionId && api.version(versionId).then(setVersion).catch((reason: Error) => setError(reason.message)), [versionId]);
  useEffect(() => {
    if (!versionId) return;
    setVersion(undefined); setSelected([]); setError(undefined); void load();
    api.allFiles().then(setFiles).catch(() => setFiles([]));
  }, [versionId, load]);
  const draft = version?.status === 'DRAFT';
  const available = useMemo(() => files.filter((file) => !version?.files?.some((member) => member.robotFileId === file.id)), [files, version]);
  const refresh = async () => { await load(); onChanged(); };
  const perform = async (kind: string, action: () => Promise<void>) => {
    if (running.current || !canMutate) return;
    running.current = true; setPending(kind);
    try { await action(); }
    catch (reason) { message.error((reason as Error).message); }
    finally { running.current = false; setPending(undefined); }
  };
  const freeze = () => perform('freeze', async () => {
    if (!version || !draft || !canPublish) return;
    const report = await api.validateVersion(version.id);
    if (!report.valid) { message.error('校验失败，请解决全部错误后再冻结。'); return; }
    await api.freeze(version.id); message.success('版本已冻结'); await refresh();
  });
  const publish = () => perform('publish', async () => {
    if (!version || version.status !== 'FROZEN' || !canPublish) return;
    const report = await api.validateVersion(version.id);
    if (!report.valid) { message.error('校验失败，暂时无法发布。'); return; }
    await api.publish(version.id); message.success('版本已发布'); await refresh();
  });
  return <Drawer open={Boolean(versionId)} width={920} title={<div><Typography.Text strong>数据集版本</Typography.Text>{version && <Typography.Text type="secondary" className="drawer-subtitle">{datasetName ?? "数据集"} · v{version.version}</Typography.Text>}</div>} onClose={onClose}>
    <DataState loading={!version && !error} error={error} empty={false} emptyTitle="版本不可用" emptyDescription="无法加载版本。">
      {version && <>
        <div className="version-hero"><div><span>版本</span><strong>v{version.version}</strong></div><VersionTag status={version.status} /></div>
        <Descriptions bordered size="small" column={2} items={[
          { key: 'files', label: "文件数量", children: version.fileCount }, { key: 'size', label: "总大小", children: bytes(version.totalSize) },
          { key: 'manifest', label: "清单路径", children: <Mono value={version.manifestKey} />, span: 2 },
          { key: 'manifestChecksum', label: "清单校验和", children: <Mono value={version.manifestChecksum} copyable />, span: 2 },
          { key: 'commit', label: "版本提交编号", children: <Mono value={version.gitCommit} copyable /> }, { key: 'tag', label: "版本标签", children: <Mono value={version.gitTag} copyable /> },
          { key: 'dvcPath', label: "数据版本路径", children: <Mono value={version.dvcPath} /> }, { key: 'dvcHash', label: "数据版本哈希", children: <Mono value={version.dvcHash} copyable /> },
          { key: 'created', label: "创建时间", children: date(version.createdAt) }, { key: 'frozen', label: "冻结时间", children: date(version.frozenAt) },
        ]} />
        <section className="drawer-section"><Typography.Title level={4}>生命周期</Typography.Title><Timeline className="version-timeline" items={[
          { color: 'green', children: <><strong>草稿已创建</strong><small>{date(version.createdAt)}</small></> },
          { color: version.frozenAt ? 'green' : 'gray', children: <><strong>文件列表已冻结</strong><small>{version.frozenAt ? date(version.frozenAt) : 'Pending'}</small></> },
          { color: version.status === 'PUBLISHED' ? 'green' : 'gray', children: <><strong>数据快照已发布</strong><small>{version.status === 'PUBLISHED' ? version.gitTag ?? 'Published' : 'Pending'}</small></> },
        ]} /></section>
        {draft && canMutate && <section className="drawer-section"><Typography.Title level={4}>添加文件</Typography.Title><Space.Compact block><Select mode="multiple" disabled={Boolean(pending)} value={selected} maxTagCount="responsive" onChange={setSelected} placeholder="选择可用文件" options={available.map((file) => ({ value: file.id, label: file.originalName }))} /><Button type="primary" loading={pending === 'add'} disabled={!selected.length || Boolean(pending)} onClick={() => void perform('add', async () => { await api.addFiles(version.id, selected); setSelected([]); await refresh(); })}>添加</Button></Space.Compact></section>}
        <section className="drawer-section"><Typography.Title level={4}>版本文件</Typography.Title><Table rowKey="robotFileId" size="small" dataSource={version.files} pagination={{ pageSize: 8, showSizeChanger: false }} columns={[
          { title: "文件", render: (_, member) => member.robotFile.originalName, ellipsis: true },
          { title: "识别类型", render: (_, member) => member.robotFile.recognitionType },
          { title: "校验和", render: (_, member) => <Typography.Text className="mono-value">{shortHash(member.robotFile.checksum)}</Typography.Text> },
          { title: "大小", render: (_, member) => bytes(member.robotFile.fileSize) },
          { title: '', width: 70, render: (_, member) => draft && canMutate ? <Button aria-label={`移除文件 ${member.robotFile.originalName}`} danger type="text" icon={<DeleteOutlined />} loading={pending === 'remove:' + member.robotFileId} disabled={Boolean(pending)} onClick={() => void perform('remove:' + member.robotFileId, async () => { await api.removeFile(version.id, member.robotFileId); await refresh(); })} /> : <Typography.Text type="secondary">{draft ? '只读' : '已锁定'}</Typography.Text> },
        ]} /></section>
        <div className="drawer-actions">
          {draft && canPublish && <Button type="primary" icon={<LockOutlined />} loading={pending === 'freeze'} disabled={Boolean(pending)} onClick={() => void freeze()}>冻结版本</Button>}
          {version.status === 'FROZEN' && canPublish && <Button type="primary" icon={<RocketOutlined />} loading={pending === 'publish'} disabled={Boolean(pending)} onClick={() => void publish()}>发布数据版本</Button>}
        </div>
      </>}
    </DataState>
  </Drawer>;
}

function Mono({ value, copyable = false }: { value?: string | null; copyable?: boolean }) { return value ? <Typography.Text className="mono-value" copyable={copyable}>{value}</Typography.Text> : <Typography.Text type="secondary">--</Typography.Text>; }
