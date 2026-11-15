import {
  DeleteOutlined,
  ExportOutlined,
  FolderAddOutlined,
  TagOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Drawer,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { useMemo, useState } from "react";
import { api } from "../services/api";

export function BulkActionBar({
  count,
  onAddDataset,
  onTag,
  onExportCsv,
  onExportJson,
  onDelete,
  onClear,
}: {
  count: number;
  onAddDataset: () => void;
  onTag?: () => void;
  onExportCsv: () => void;
  onExportJson: () => void;
  onDelete: () => void;
  onClear: () => void;
}) {
  if (!count) return null;
  return (
    <div className="bulk-action-bar">
      <Typography.Text strong>{count} 个文件已选</Typography.Text>
      <Space wrap>
        <Button
          icon={<FolderAddOutlined />}
          type="primary"
          onClick={onAddDataset}
        >
          添加到数据集
        </Button>
        {onTag && <Button icon={<TagOutlined />} onClick={onTag}>应用标签</Button>}
        <Button icon={<ExportOutlined />} onClick={onExportCsv}>
          CSV
        </Button>
        <Button icon={<ExportOutlined />} onClick={onExportJson}>
          JSON
        </Button>
        <Button danger icon={<DeleteOutlined />} onClick={onDelete}>
          删除
        </Button>
        <Button type="text" onClick={onClear}>
          清空
        </Button>
      </Space>
    </div>
  );
}

type Mode = "EXISTING_DRAFT" | "NEW_DATASET" | "NEW_VERSION";
export function AddToDatasetDrawer({
  open,
  fileIds,
  onClose,
  onDone,
}: {
  open: boolean;
  fileIds: string[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [form] = Form.useForm();
  const [mode, setMode] = useState<Mode>("EXISTING_DRAFT");
  const queryClient = useQueryClient();
  const datasets = useQuery({
    queryKey: ["datasets"],
    queryFn: api.datasets,
    enabled: open,
  });
  const drafts = useMemo(
    () =>
      datasets.data?.flatMap((dataset) =>
        dataset.versions
          .filter((version) => version.status === "DRAFT")
          .map((version) => ({
            value: version.id,
            label: `${dataset.name} · v${version.version}`,
          })),
      ) ?? [],
    [datasets.data],
  );
  const mutation = useMutation({
    mutationFn: async (values: Record<string, string>) => {
      if (mode === "EXISTING_DRAFT")
        return api.addFiles(values.versionId, fileIds);
      if (mode === "NEW_VERSION")
        return api.createVersion(values.datasetId, fileIds);
      const dataset = await api.createDataset({
        name: values.name,
        description: values.description,
      });
      return api.createVersion(dataset.id, fileIds);
    },
    onSuccess: () => {
      message.success(`${fileIds.length} 个文件已添加到草稿版本`);
      void queryClient.invalidateQueries({ queryKey: ["datasets"] });
      void queryClient.invalidateQueries({ queryKey: ["files"] });
      onDone();
    },
    onError: (reason: Error) => message.error(reason.message),
  });
  const submit = async () => mutation.mutate(await form.validateFields());
  return (
    <Drawer
      open={open}
      width={520}
      title="添加文件到数据集"
      onClose={onClose}
      afterOpenChange={(visible) => {
        if (!visible) {
          form.resetFields();
          setMode("EXISTING_DRAFT");
        }
      }}
      extra={
        <Button
          type="primary"
          loading={mutation.isPending}
          onClick={() => void submit()}
        >
          添加文件
        </Button>
      }
    >
      <div className="selection-summary">
        <strong>{fileIds.length}</strong>
        <span>个文件已选择</span>
      </div>
      <Radio.Group
        value={mode}
        onChange={(event) => {
          setMode(event.target.value as Mode);
          form.resetFields();
        }}
        className="stacked-radio"
      >
        <Radio value="EXISTING_DRAFT">添加到已有草稿</Radio>
        <Radio value="NEW_DATASET">新建数据集与草稿版本</Radio>
        <Radio value="NEW_VERSION">创建新版本</Radio>
      </Radio.Group>
      <Form form={form} layout="vertical" className="drawer-form">
        {mode === "EXISTING_DRAFT" && (
          <Form.Item
            name="versionId"
            label="草稿版本"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={drafts}
              placeholder="请选择可编辑版本"
            />
          </Form.Item>
        )}
        {mode === "NEW_VERSION" && (
          <Form.Item
            name="datasetId"
            label="数据集"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={datasets.data?.map((dataset) => ({
                value: dataset.id,
                label: dataset.name,
              }))}
            />
          </Form.Item>
        )}
        {mode === "NEW_DATASET" && (
          <>
            <Form.Item
              name="name"
              label="数据集名称"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item name="description" label="说明">
              <Input.TextArea rows={4} />
            </Form.Item>
          </>
        )}
      </Form>
      <Typography.Paragraph type="secondary">
        仅修改草稿版本的文件列表，已冻结和已发布的版本保持不变。
      </Typography.Paragraph>
    </Drawer>
  );
}
