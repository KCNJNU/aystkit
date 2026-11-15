import { jobNames, stepNames, statusNames, errorText } from '../services/labels';
import {
  ArrowLeftOutlined,
  ReloadOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Progress,
  Space,
  Steps,
  Tag,
  Typography,
  message,
} from "antd";
import { useNavigate, useParams } from "react-router-dom";
import { DataState, PageHeader } from "../components/ui";
import { api } from "../services/api";
import { date } from "../services/format";
import { useOptionalAuth } from "../auth/AuthContext";
import "./admin-workspace.css";

export function JobDetail() {
  const auth = useOptionalAuth();
  const canWrite = auth ? auth.hasRole("ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN", "TENANT_ADMIN", "PROJECT_ADMIN", "OPERATOR") : true;
  const { id } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const job = useQuery({
    queryKey: ["jobs", "detail", id],
    queryFn: () => api.job(id!),
    enabled: Boolean(id),
    refetchInterval: (query) =>
      ["QUEUED", "RUNNING"].includes(query.state.data?.status ?? "")
        ? 2_000
        : false,
  });
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["jobs"] }),
      job.refetch(),
    ]);
  };
  const retry = useMutation({
    mutationFn: () => api.retryJob(id!),
    onSuccess: async () => {
      message.success("任务已加入重试队列");
      await refresh();
    },
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelJob(id!),
    onSuccess: async () => {
      message.success("已提交取消请求");
      await refresh();
    },
  });
  const value = job.data;
  const canOperate = canWrite && !(auth?.user?.role === 'OPERATOR' && ['DATASET_PUBLISH', 'RECOVERY_VERIFY'].includes(value?.type ?? ''));
  return (
    <section className="page-stack admin-workspace">
      <PageHeader
        title={value ? jobNames[value.type] ?? value.type : "任务详情"}
        actions={
          <Space wrap>
            <Button
              icon={<ArrowLeftOutlined />}
              onClick={() => navigate("/jobs")}
            >
              全部任务
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
              刷新
            </Button>
            {canOperate && value?.status === "FAILED" && (
              <Button
                type="primary"
                onClick={() => retry.mutate()}
                loading={retry.isPending}
              >
                重试失败步骤
              </Button>
            )}
            {canOperate &&
              value &&
              value.type !== "DATASET_DELETE" && ["QUEUED", "RUNNING"].includes(value.status) && (
                <Button
                  danger
                  icon={<StopOutlined />}
                  onClick={() => cancel.mutate()}
                >
                  取消
                </Button>
              )}
          </Space>
        }
      />
      <DataState
        loading={job.isLoading}
        error={(job.error as Error | null)?.message}
        empty={!value}
        emptyTitle="任务不存在"
      >
        {value && (
          <div className="page-stack admin-workspace">
            <Card variant="borderless" className="panel-card admin-panel">
              <Descriptions
                column={{ xs: 1, sm: 2, lg: 3 }}
                bordered
                size="small"
                items={[
                  {
                    key: "id",
                    label: "任务编号",
                    children: (
                      <Typography.Text copyable>{value.id}</Typography.Text>
                    ),
                  },
                  {
                    key: "status",
                    label: "状态",
                    children: <Tag>{statusNames[value.status] ?? value.status}</Tag>,
                  },
                  {
                    key: "progress",
                    label: "进度",
                    children: (
                      <Progress percent={value.progress} size="small" />
                    ),
                  },
                  {
                    key: "resource",
                    label: "资源",
                    children: `${value.resourceType ?? "--"} · ${value.resourceId ?? "--"}`,
                  },
                  {
                    key: "created",
                    label: "创建时间",
                    children: date(value.createdAt),
                  },
                  {
                    key: "finished",
                    label: "完成时间",
                    children: value.finishedAt ? date(value.finishedAt) : "--",
                  },
                ]}
              />
              {value.errorMessage && (
                <Alert
                  type="error"
                  showIcon
                  className="job-detail-alert"
                  message="执行失败"
                  description={errorText(value.errorMessage)}
                />
              )}
            </Card>
            <Card
              variant="borderless"
              className="panel-card admin-panel"
              title="执行步骤"
            >
              <Steps
                direction="vertical"
                current={value.steps.findIndex(
                  (step) =>
                    step.status === "RUNNING" || step.status === "FAILED",
                )}
                items={value.steps.map((step) => ({
                  title: stepNames[step.name] ?? step.name,
                  status:
                    step.status === "SUCCEEDED"
                      ? "finish"
                      : step.status === "RUNNING"
                        ? "process"
                        : step.status === "FAILED"
                          ? "error"
                          : "wait",
                  description:
                    step.errorMessage ??
                    (step.finishedAt
                      ? `完成于 ${date(step.finishedAt)}`
                      : "等待后台执行"),
                }))}
              />
            </Card>
          </div>
        )}
      </DataState>
    </section>
  );
}
