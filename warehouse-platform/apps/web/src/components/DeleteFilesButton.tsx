import { FileReferencesButton } from './FileReferencesButton';
import { DeleteOutlined } from '@ant-design/icons';
import { useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Collapse, Modal, Space, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { writeRoles } from '../auth/permissions';
import { api, type FileImpact } from '../services/api';
import { statusNames } from '../services/labels';

export type FileTarget = { id: string; originalName: string };
type FileCheck = { impact?: FileImpact; error?: string };
export function DeleteFilesButton({ files, onDeleted, batch = false, type = 'default' }: { files: FileTarget[]; onDeleted?: (ids: string[]) => void; batch?: boolean; type?: 'default' | 'link' }) {
  const auth = useOptionalAuth(); const [targets, setTargets] = useState<FileTarget[]>();
  if (!auth?.hasRole(...writeRoles)) return null;
  return <><Button danger type={type} icon={<DeleteOutlined aria-hidden />} disabled={!files.length || Boolean(targets)} aria-label={batch ? '删除选中文件' : `删除文件 ${files[0]?.originalName ?? ''}`} onClick={() => setTargets([...files])}>{batch ? `删除选中文件${files.length ? `（${files.length}）` : ''}` : '删除'}</Button>{targets && <FileDeleteDialog files={targets} onClose={() => setTargets(undefined)} onDeleted={onDeleted} />}</>;
}

/** Mounted only after a delete click; failures expose details without a confirm step. */
export function FileDeleteDialog({ files, onDeleted, onClose }: { files: FileTarget[]; onDeleted?: (ids: string[]) => void; onClose: () => void }) {
  const auth = useOptionalAuth(); const canDelete = auth?.hasRole(...writeRoles) ?? false;
  const client = useQueryClient(); const [targets, setTargets] = useState(() => [...files]);
  const [busy, setBusy] = useState(false); const running = useRef(false); const started = useRef(false);
  const [attempted, setAttempted] = useState(false); const [checks, setChecks] = useState<Record<string, FileCheck>>({});
  const [failures, setFailures] = useState<Array<{ name: string; reason: string }>>([]);
  const remove = useCallback(async () => {
    if (running.current || !canDelete || !targets.length) return;
    running.current = true; setBusy(true); setFailures([]);
    const deleted: string[] = []; const failed: Array<{ name: string; reason: string }> = [];
    try {
      const results = await Promise.allSettled(targets.map(file => client.fetchQuery({ queryKey: ['files', 'impact', file.id], queryFn: () => api.fileImpact(file.id), staleTime: 0, retry: false })));
      const inspected: Record<string, FileCheck> = {};
      for (const [index, file] of targets.entries()) {
        const result = results[index];
        inspected[file.id] = result.status === 'fulfilled' ? { impact: result.value } : { error: result.reason instanceof Error ? result.reason.message : '删除检查失败，请重试' };
      }
      setChecks(inspected);
      for (const file of targets) {
        if (!inspected[file.id]?.impact?.canRecycle) continue;
        try { await api.deleteFile(file.id); deleted.push(file.id); }
        catch (error) { failed.push({ name: file.originalName, reason: error instanceof Error ? error.message : '移入回收站失败，请稍后重试' }); }
      }
      setFailures(failed); setTargets(previous => previous.filter(file => !deleted.includes(file.id)));
      if (deleted.length) {
        void message.success(`已将 ${deleted.length} 个文件移入回收站`); onDeleted?.(deleted);
        await Promise.all(['files', 'recycle-bin', 'datasets', 'warehouse-overview', 'dashboard', 'robots', 'storage', 'search'].map(key => client.invalidateQueries({ queryKey: [key] })));
      }
      if (deleted.length === targets.length) onClose();
    } finally { running.current = false; setBusy(false); setAttempted(true); }
  }, [canDelete, targets, client, onDeleted, onClose]);
  useEffect(() => {
    if (started.current || !canDelete) return;
    started.current = true;
    void remove();
  }, [canDelete, remove]);
  if (!canDelete) return null;
  return <>
    {busy && <span role="status">正在删除文件…</span>}
    <Modal title={`未能删除的文件 · ${targets.length} 项`} open={attempted && targets.length > 0} width={780}
      footer={<Space><Button disabled={busy} onClick={onClose}>关闭</Button><Button danger loading={busy} disabled={busy} onClick={() => void remove()}>重试删除</Button></Space>}
      closable={!busy} maskClosable={!busy} keyboard={!busy} onCancel={onClose}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Collapse defaultActiveKey={targets.length === 1 ? [targets[0].id] : []} items={targets.map(file => {
          const check = checks[file.id]; const impact = check?.impact;
          return { key: file.id, label: <Space wrap><span style={{ overflowWrap: 'anywhere' }}>{file.originalName}</span><Tag color="orange">{check?.error ? '检查失败' : impact?.canRecycle ? '删除失败' : '暂不能删除'}</Tag>{Boolean(check?.error || impact?.blockers.length) && <Typography.Text type="danger">{check?.error || impact?.blockers.join('；')}</Typography.Text>}</Space>, children: <Space direction="vertical" style={{ width: '100%' }}>
            {impact && <><Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>存储路径：{impact.objectKey}</Typography.Text>
              {impact.references.map(ref => <div key={ref.versionId}>文件 → {ref.datasetName} → 第 {ref.version} 版 <Tag>{statusNames[ref.status]}</Tag>{ref.reason}</div>)}
              {!!impact.restrictedCount && <Typography.Text>另有 {impact.restrictedCount} 处范围外引用，请联系相关管理员。</Typography.Text>}
            </>}
            <FileReferencesButton file={file} disabled={busy} />
          </Space> };
        })} />
        {!!failures.length && <Alert type="error" showIcon message={`${failures.length} 个文件未删除成功`} description={failures.map((failure, index) => <Typography.Paragraph key={index}>{failure.name}：{failure.reason}</Typography.Paragraph>)} />}
      </Space>
    </Modal>
  </>;
}
