import { statusNames } from '../services/labels';
import {
  DeleteOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ScanOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Input,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { useRef, useState } from "react";
import { useNavigate } from 'react-router-dom';
import { useAuth } from "../auth/AuthContext";
import { DataState, PageHeader } from "../components/ui";
import { api } from "../services/api";
import { bytes, date } from "../services/format";
import "./admin-workspace.css";

export function Storage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [prefix, setPrefix] = useState("");
  const [selectedRun, setSelectedRun] = useState<string>();
  const [selectedIssues, setSelectedIssues] = useState<string[]>([]);
  const removing = useRef(false);
  const objects = useQuery({
    queryKey: ["storage", "objects", prefix],
    queryFn: () => api.storageObjects({ prefix, limit: 100 }),
  });
  const runs = useQuery({
    queryKey: ["storage", "runs"],
    queryFn: () => api.reconciliationRuns({ page: 1, pageSize: 30 }),
    refetchInterval: 10_000,
  });
  const detail = useQuery({
    queryKey: ["storage", "run", selectedRun],
    queryFn: () => api.reconciliationRun(selectedRun!),
    enabled: Boolean(selectedRun),
    refetchInterval: (query) =>
      ["RUNNING", "QUEUED"].includes(query.state.data?.status ?? "")
        ? 5_000
        : false,
  });
  const scan = useMutation({
    mutationFn: api.startReconciliation,
    onSuccess: async (value) => {
      message.success("存储扫描已加入队列");
      await client.invalidateQueries({ queryKey: ["storage", "runs"] });
      if (value.job.id)
        navigate(`/storage?job=${value.job.id}`, { replace: true });
    },
  });
  const remove = useMutation({
    mutationFn: (ids: string[]) => api.deleteOrphans(ids),
    onError: (error: Error) => message.error(error.message),
    onSuccess: (job) => {
      message.success("孤立对象删除任务已加入队列");
      navigate(`/jobs/${job.id}`);
    },
  });
  const openIssues =
    detail.data && detail.data.id === selectedRun
      ? detail.data.issues.filter((item) => item.status === "OPEN")
      : [];
  const actionableIssueIds = openIssues
    .filter((item) => item.type === "ORPHAN_OBJECT" && selectedIssues.includes(item.id))
    .map((item) => item.id);
  const detailUnavailable = detail.isPending || detail.isFetching || detail.isError;
  return (
    <section className="page-stack admin-workspace">
      <PageHeader
        title="存储管理"
        description="浏览原始文件与存储索引，核对数据一致性。"
        actions={
          <Space wrap>
            {auth.hasRole("ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN") && (
              <Button
                type="primary"
                icon={<ScanOutlined />}
                loading={scan.isPending}
                onClick={() => scan.mutate()}
              >
                开始一致性扫描
              </Button>
            )}
            <Button
              icon={<ReloadOutlined />}
              onClick={() => {
                void objects.refetch();
                void runs.refetch();
              }}
            >
              刷新
            </Button>
          </Space>
        }
      />
      <Tabs
        className="admin-tabs"
        items={[
          {
            key: "objects",
            label: "对象浏览",
            children: (
              <div className="page-stack admin-workspace">
                <Card variant="borderless" className="filter-bar admin-panel admin-filter-bar">
                  <div className="admin-filter-content"><Input.Search
                    aria-label="搜索存储路径"
                    allowClear
                    placeholder="存储路径前缀"
                    onSearch={setPrefix}
                  /></div>
                </Card>
                <DataState
                  loading={objects.isLoading}
                  error={(objects.error as Error | null)?.message}
                  empty={!objects.data?.items.length}
                  emptyTitle="暂无存储对象"
                >
                  <Card variant="borderless" className="table-panel admin-panel admin-table-panel">
                    <Table
                      rowKey="objectKey"
                      dataSource={objects.data?.items ?? []}
                      pagination={false}
                      scroll={{ x: 1050 }}
                      columns={[
                        {
                          title: "存储路径",
                          dataIndex: "objectKey",
                          render: (value) => (
                            <Typography.Text className="mono-value" copyable>
                              {statusNames[value] ?? value}
                            </Typography.Text>
                          ),
                        },
                        {
                          title: "大小",
                          dataIndex: "size",
                          width: 120,
                          render: bytes,
                        },
                        {
                          title: "数据库映射",
                          dataIndex: "mapping",
                          width: 180,
                          render: (value) =>
                            value ? (
                              <Tag color="success">已映射</Tag>
                            ) : (
                              <Tag color="warning">未建立索引</Tag>
                            ),
                        },
                        {
                          title: "ETag",
                          dataIndex: "etag",
                          width: 180,
                          ellipsis: true,
                        },
                        {
                          title: "已修改",
                          dataIndex: "lastModified",
                          width: 180,
                          render: (value) => (value ? date(value) : "--"),
                        },
                      ]}
                    />
                  </Card>
                </DataState>
              </div>
            ),
          },
          {
            key: "runs",
            label: "一致性核对",
            children: (
              <div className="storage-review-grid">
                <Card variant="borderless" className="table-panel admin-panel admin-table-panel">
                  <Table
                    rowKey="id"
                    dataSource={runs.data?.items ?? []}
                    scroll={{ x: 620 }}
                    pagination={false}
                    onRow={(row) => ({
                      onClick: () => {
                        if (row.id !== selectedRun) {
                          setSelectedIssues([]);
                          setSelectedRun(row.id);
                        }
                      },
                      className: "clickable-row",
                    })}
                    columns={[
                      { title: "执行", dataIndex: "id", ellipsis: true },
                      {
                        title: "状态",
                        dataIndex: "status",
                        render: (value) => <Tag>{statusNames[value] ?? value}</Tag>,
                      },
                      { title: "存储对象", dataIndex: "scannedObjects" },
                      { title: "问题", dataIndex: "issueCount" },
                      {
                        title: "创建时间",
                        dataIndex: "createdAt",
                        render: date,
                      },
                    ]}
                  />
                </Card>
                <Card
                  variant="borderless"
                  className="panel-card admin-panel"
                  title="问题检查"
                  extra={
                    ["ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN"].includes(auth.user?.role ?? "") && actionableIssueIds.length ? (
                      <Button
                        danger
                        icon={<DeleteOutlined />}
                        loading={remove.isPending}
                        disabled={remove.isPending || detailUnavailable}
                        onClick={() => {
                          if (removing.current || remove.isPending || detailUnavailable || !detail.data || detail.data.id !== selectedRun || !auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN')) return;
                          const ids = detail.data.issues
                            .filter((item) => item.status === 'OPEN' && item.type === 'ORPHAN_OBJECT' && selectedIssues.includes(item.id))
                            .map((item) => item.id);
                          if (!ids.length) return;
                          removing.current = true;
                          remove.mutate(ids, { onSettled: () => { removing.current = false; } });
                        }}
                      >
                        删除选中的孤立对象
                      </Button>
                    ) : null
                  }
                >
                  {!selectedRun ? (
                    <Alert type="info" showIcon message="请选择扫描记录" />
                  ) : (
                    <Table
                      size="small"
                      rowKey="id"
                      loading={detail.isLoading}
                      dataSource={openIssues}
                      scroll={{ x: 620 }}
                      rowSelection={
                        ["ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN"].includes(auth.user?.role ?? "")
                          ? {
                              selectedRowKeys: selectedIssues,
                              onChange: (_keys, rows) =>
                                setSelectedIssues(
                                  rows
                                    .filter(
                                      (row) => row.status === "OPEN" && row.type === "ORPHAN_OBJECT",
                                    )
                                    .map((row) => row.id),
                                ),
                              getCheckboxProps: (row) => ({
                                disabled: remove.isPending || detailUnavailable || row.status !== "OPEN" || row.type !== "ORPHAN_OBJECT",
                              }),
                            }
                          : undefined
                      }
                      pagination={false}
                      columns={[
                        {
                          title: "类型",
                          dataIndex: "type",
                          render: (value) => (
                            <Tag
                              color={
                                value === "ORPHAN_OBJECT" ? "warning" : "error"
                              }
                            >
                              {statusNames[value] ?? value}
                            </Tag>
                          ),
                        },
                        {
                          title: "存储对象",
                          dataIndex: "objectKey",
                          ellipsis: true,
                        },
                        {
                          title: "预期值",
                          dataIndex: "expectedSize",
                          render: (value) => (value ? bytes(value) : "--"),
                        },
                        {
                          title: "实际值",
                          dataIndex: "actualSize",
                          render: (value) => (value ? bytes(value) : "--"),
                        },
                      ]}
                    />
                  )}
                </Card>
              </div>
            ),
          },
          {
            key: "policy",
            label: (
              <span>
                <SafetyCertificateOutlined /> 操作规则
              </span>
            ),
            children: (
              <Alert
                showIcon
                type="info"
                message="仅清理扫描识别的孤立文件"
                description="缺失与大小不一致问题只生成报告。管理员可删除已完成扫描认定的孤立对象；扫描排除正在上传的分片。"
              />
            ),
          },
        ]}
      />
    </section>
  );
}
