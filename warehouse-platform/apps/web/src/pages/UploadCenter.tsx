import './data-workspace.css';
import {
  CloseOutlined,
  DeleteOutlined,
  InboxOutlined,
  ReloadOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Grid,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Typography,
  Upload,
} from "antd";
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../services/api';
import { useNavigate, useSearchParams } from "react-router-dom";
import { PageHeader, StatusTag } from "../components/ui";
import { bytes } from "../services/format";
import { isProcessedPackageDestination, PROCESSED_PACKAGE_EXAMPLE, PROCESSED_PACKAGE_LABEL, PROCESSED_PACKAGE_REQUIREMENT } from "../services/processed-package";
import {
  useUploadQueue,
  type UploadQueueItem,
  type UploadStatus,
} from "../upload/UploadQueueContext";

const statusConfig: Record<UploadStatus, { label: string }> = {
  QUEUED: { label: "排队中" }, HASHING: { label: "计算 SHA-256" }, CHECKING_DUPLICATE: { label: "检查重复" },
  UPLOADING: { label: "上传并保存" }, FINALIZING: { label: "正在归档" }, COMPLETED: { label: "已完成" },
  FAILED: { label: "失败" }, CANCELLED: { label: "已取消" }, DUPLICATE: { label: "重复文件" },
};
export function UploadCenter() {
  const { message } = App.useApp();
  const screens = Grid.useBreakpoint();
  const queue = useUploadQueue();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [datasetId, setDatasetId] = useState<string | undefined>(params.get('dataset') ?? undefined);
  const [folderPath, setFolderPath] = useState(params.get('path') ?? '');
  const folders = useQuery({ queryKey: ['datasets', 'folders', datasetId], queryFn: () => api.datasetFolders(datasetId!), enabled: Boolean(datasetId) });
  const folderValid = !folderPath || Boolean(folders.data?.some(folder => folder.path === folderPath));
  const datasets = useQuery({ queryKey: ['datasets', 'upload-options'], queryFn: api.datasets });
  const target = datasets.data?.find(item => item.id === datasetId);
  const processedDirectory = Boolean(target) && isProcessedPackageDestination(folderPath);
  const addFiles = (files: File[]) => { if (target && folderValid) queue.addFiles(files, { ...target, folderPath }); };
  const queued = queue.items.filter((item) => item.status === "QUEUED").length;
  return (
    <div className="page-stack data-workspace upload-studio">
      <PageHeader
        title="上传中心"
        description="选择数据集与目录后添加文件，在队列中跟踪上传结果。"
        actions={
          <Button onClick={queue.clearCompleted}>清除已完成</Button>
        }
      />
      <Row gutter={[20, 20]}>
        <Col span={24}>
          <Card variant="borderless" className="panel-card upload-source-panel">
            <div className="upload-step-heading"><div><h2>选择存储位置</h2></div></div>
            <div className="upload-destination">
              <div className="upload-target-grid">
                <div className="data-filter-field"><label htmlFor="upload-dataset">目标数据集</label><Select id="upload-dataset" aria-label="目标数据集" showSearch optionFilterProp="label" placeholder="请选择已创建的数据集" loading={datasets.isLoading} value={datasetId} onChange={value => { setDatasetId(value); setFolderPath(''); }} options={datasets.data?.map(item => ({ value: item.id, label: item.name }))} /></div>
                <div className="data-filter-field"><label htmlFor="upload-folder">目标文件夹</label><Select id="upload-folder" aria-label="目标文件夹" showSearch optionFilterProp="label" value={folderPath} onChange={setFolderPath} disabled={!target} loading={folders.isLoading} options={[{ value: '', label: '根目录' }, ...(folders.data ?? []).map(folder => ({ value: folder.path, label: folder.path }))]} /></div>
              </div>
              <Space wrap className="upload-destination-actions">
                <Button onClick={() => navigate('/datasets')}>创建 / 管理数据集</Button>
                {target && <Button type="link" onClick={() => navigate(`/datasets/${target.id}?path=${encodeURIComponent(folderPath)}`)}>查看数据集</Button>}
              </Space>
              {target && <div className="upload-destination-path"><span>存储目录</span><code>{target.name}/{folderPath ? `${folderPath}/` : ''}</code></div>}
            </div>
            {folders.isError && <Alert type="error" message="文件夹列表加载失败" action={<Button onClick={() => void folders.refetch()}>重试</Button>} />}
            {datasets.isError && <Alert type="error" message="数据集列表加载失败" action={<Button onClick={() => void datasets.refetch()}>重试</Button>} />}
            <div className="upload-step-heading upload-add-heading"><div><h2>添加文件</h2></div></div>
            <Alert
              type="info"
              showIcon
              message={target ? `即将上传到 ${target.name}` : "请先选择目标数据集"}
              style={{ marginBottom: 12 }}
            />
            {processedDirectory && <Alert type="info" showIcon message={PROCESSED_PACKAGE_LABEL} description={<pre>{PROCESSED_PACKAGE_EXAMPLE}</pre>} />}
            <Upload.Dragger
              accept={processedDirectory ? ".zip" : undefined}
              onDrop={event => { if (target && folderValid && processedDirectory && Array.from(event.dataTransfer.files).some(file => !file.name.toLowerCase().endsWith(".zip"))) void message.error(PROCESSED_PACKAGE_REQUIREMENT); }}
              disabled={!target || !folderValid}
              multiple
              showUploadList={false}
              beforeUpload={(file) => {
                addFiles([file]);
                return Upload.LIST_IGNORE;
              }}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">拖拽文件到这里或点击选择</p>
            </Upload.Dragger>
            <div className="upload-folder-action">
              <Upload
                disabled={!target || !folderValid || processedDirectory}
                multiple
                directory
                showUploadList={false}
                beforeUpload={(file) => {
                  addFiles([file]);
                  return Upload.LIST_IGNORE;
                }}
              >
                <Button disabled={!target || !folderValid || processedDirectory} icon={<InboxOutlined />}>选择整个文件夹</Button>
              </Upload>
            </div>
          </Card>
        </Col>
        <Col span={24}>
          <Card variant="borderless" className="panel-card upload-queue-panel">
            <div className="upload-step-heading"><div><h2>上传队列</h2></div></div>
            <div className="queue-summary">
              <span>
                <strong>{queue.items.length}</strong> 个文件
              </span>
              <span>
                <strong>{queued}</strong> 等待上传
              </span>
              <span>
                <strong>
                  {
                    queue.items.filter((item) => item.status === "COMPLETED")
                      .length
                  }
                </strong>{" "}
                已完成
              </span>
              <span>
                <strong>
                  {
                    queue.items.filter((item) => item.status === "FAILED")
                      .length
                  }
                </strong>{" "}
                上传失败
              </span>
            </div>
            <Table
              size="small"
              rowKey="id"
              dataSource={queue.items}
              pagination={false}
              scroll={{ x: 850, y: 430 }}
              locale={{ emptyText: "暂无上传任务" }}
              columns={[
                {
                  title: "文件名称",
                  width: 250,
                  render: (_, item) => (
                    <div>
                      <Typography.Text strong ellipsis={{ tooltip: item.fileName }}>
                        {item.fileName}
                      </Typography.Text>
                      <small className="table-secondary">
                        {bytes(item.fileSize)}
                        {!item.file ? " · 重新选择文件以续传" : ""}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "目标数据集",
                  dataIndex: "datasetName",
                  width: 140,
                  render: (value: string, item) => value ? `${value}/${item.folderPath ?? ''}` : "旧任务，请重新选择数据集",
                },
                {
                  title: "当前状态",
                  width: 160,
                  render: (_, item) => (
                    <>
                      <StatusTag status={item.status} label={statusConfig[item.status].label} />
                      {item.error && (
                        <small className="queue-error">{item.error}</small>
                      )}
                    </>
                  ),
                },
                {
                  title: "上传进度",
                  width: 180,
                  render: (_, item) => (
                    <Progress
                      size="small"
                      percent={item.progress}
                      status={
                        item.status === "FAILED"
                          ? "exception"
                          : item.status === "COMPLETED"
                            ? "success"
                            : "active"
                      }
                    />
                  ),
                },
                {
                  title: "校验 / 归档结果",
                  render: (_, item) => (
                    <Typography.Text className="mono-value" ellipsis>
                      {item.existing
                        ? `已存在：${item.existing.originalName}`
                        : (item.result?.objectKey ?? item.checksum ?? "--")}
                    </Typography.Text>
                  ),
                },
                {
                  title: "操作",
                  width: 110,
                  fixed: screens.lg ? "right" : undefined,
                  render: (_, item) => (
                    <QueueActions
                      item={item}
                      onCancel={() => queue.cancel(item.id)}
                      onRetry={() => queue.retry(item.id)}
                      onRemove={() => queue.remove(item.id)}
                      onExisting={() =>
                        item.existing &&
                        navigate(`/files?file=${item.existing.id}`)
                      }
                    />
                  ),
                },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </div>
  );
}
function QueueActions({
  item,
  onCancel,
  onRetry,
  onRemove,
  onExisting,
}: {
  item: UploadQueueItem;
  onCancel: () => void;
  onRetry: () => void;
  onRemove: () => void;
  onExisting: () => void;
}) {
  if (["HASHING", "CHECKING_DUPLICATE", "UPLOADING", "FINALIZING"].includes(item.status))
    return (
      <Button danger type="text" icon={<StopOutlined />} onClick={onCancel}>
        取消
      </Button>
    );
  if (item.status === "FAILED" || item.status === "CANCELLED")
    return (
      <Space.Compact>
        <Button aria-label="重试上传" title="重试上传" type="text" icon={<ReloadOutlined />} onClick={onRetry} />
        <Button aria-label="移除任务" title="移除任务" type="text" icon={<DeleteOutlined />} onClick={onRemove} />
      </Space.Compact>
    );
  if (item.status === "DUPLICATE")
    return (
      <Space.Compact>
        <Button type="link" onClick={onExisting}>
          查看文件
        </Button>
        <Button aria-label="移除任务" title="移除任务" type="text" icon={<CloseOutlined />} onClick={onRemove} />
      </Space.Compact>
    );
  return (
    <Button
      type="text"
      aria-label="移除任务"
      icon={<DeleteOutlined />}
      disabled={item.status === "COMPLETED"}
      onClick={onRemove}
    />
  );
}
