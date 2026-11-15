import { CloudUploadOutlined, ControlOutlined, SaveOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Form, InputNumber, Switch, message } from 'antd';
import { PageHeader } from '../components/ui';
import { api } from '../services/api';
import './admin-workspace.css';

const uploadFields = [
  ['upload.jsonMaxBytes', 'JSON 数据文件'],
  ['upload.imageMaxBytes', '图片文件'],
  ['upload.pcdMaxBytes', '点云文件'],
  ['upload.mapMaxBytes', '地图文件'],
  ['upload.yamlMaxBytes', 'YAML 配置文件'],
  ['upload.logMaxBytes', '日志与文本文件'],
] as const;

export function SystemSettings() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ['system', 'configuration'], queryFn: api.systemConfiguration });
  const save = useMutation({
    mutationFn: api.updateSettings,
    onError: (error: Error) => message.error(error.message),
    onSuccess: async () => {
      message.success('系统设置已保存');
      await client.invalidateQueries({ queryKey: ['system'] });
    },
  });

  return <section className="page-stack admin-workspace settings-workspace">
    <PageHeader title="系统设置" description="管理文件上传限制与后台运行策略。" />
    {config.isError ? <Alert type="error" showIcon message="加载设置失败" description={config.error.message}
      action={<Button onClick={() => void config.refetch()}>重试</Button>} /> : !config.data ? <Card loading className="admin-panel" /> :
      <Card variant="borderless" className="panel-card admin-panel settings-panel">
        <Form layout="vertical" initialValues={config.data.settings} onFinish={next => save.mutate(next)}>
          <section className="settings-section" aria-labelledby="upload-settings-title">
            <div className="settings-section-heading"><span className="admin-section-icon"><CloudUploadOutlined /></span><div>
              <h3 id="upload-settings-title">上传限制</h3><p>单文件上限以字节为单位，0 表示不限制。</p>
            </div></div>
            <div className="settings-fields-grid">
              {uploadFields.map(([key, label]) => <Form.Item key={key} name={key} label={label}>
                <InputNumber min={0} max={Number.MAX_SAFE_INTEGER} precision={0} style={{ width: '100%' }} />
              </Form.Item>)}
            </div>
          </section>
          <section className="settings-section" aria-labelledby="operation-settings-title">
            <div className="settings-section-heading"><span className="admin-section-icon"><ControlOutlined /></span><div>
              <h3 id="operation-settings-title">运营策略</h3>
            </div></div>
            <div className="settings-fields-grid settings-policy-grid">
              <Form.Item name="download.presignedTtlSeconds" label="下载链接有效期（秒）">
                <InputNumber min={60} max={3600} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="jobs.workerConcurrency" label="后台任务并发数">
                <InputNumber min={1} max={8} style={{ width: '100%' }} />
              </Form.Item>
              <Form.Item name="notifications.jobSuccessEnabled" label="任务成功通知" valuePropName="checked">
                <Switch />
              </Form.Item>
            </div>
          </section>
          <div className="settings-save-bar">
            <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={save.isPending}>保存设置</Button>
          </div>
        </Form>
      </Card>}
  </section>;
}
