import { robotFromSearch, selectedRobot } from '../services/robot-selection';
import './data-workspace.css';
import { FileDeleteDialog, type FileTarget } from "../components/DeleteFilesButton";
import { RelocateFileButton } from "../components/RelocateFileButton";
import { FileReferencesButton } from "../components/FileReferencesButton";
import {
  AppstoreOutlined,
  DeleteOutlined,
  DownloadOutlined,
  DownOutlined,
  EyeOutlined,
  EditOutlined,
  ReloadOutlined,
  SearchOutlined,
  TableOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  DatePicker,
  Grid,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  AddToDatasetDrawer,
  BulkActionBar,
} from "../components/FileBulkActions";
import { FilePreview } from "../components/FilePreview";
import { downloadFile } from "../services/file-download";
import { fileKindLabel } from "../services/file-kind";
import { DataState, PageHeader } from "../components/ui";
import { SavedViews } from "../components/SavedViews";
import { api, type FileQuery, type RobotFile } from "../services/api";
import { bytes, date, shortHash } from "../services/format";
import { useOptionalAuth } from "../auth/AuthContext";

export function Files() {
  const auth = useOptionalAuth();
  const screens = Grid.useBreakpoint();
  const [advancedExpanded, setAdvancedExpanded] = useState<boolean>();
  const showAdvanced = advancedExpanded ?? Boolean(screens.md);
  const canMutate = auth ? auth.hasRole("ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN", "TENANT_ADMIN", "PROJECT_ADMIN", "OPERATOR") : true;
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<RobotFile>();
  const [editType, setEditType] = useState('');
  const [editMetadata, setEditMetadata] = useState('{}');
  const edit = useMutation({ mutationFn: async () => { const metadata: unknown = JSON.parse(editMetadata); if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('元信息必须为 JSON 对象'); return api.updateFile(editing!.id, { recognitionType: editType.trim(), metadata: metadata as Record<string, unknown> }); }, onSuccess: async () => { setEditing(undefined); message.success('文件信息已保存'); await queryClient.invalidateQueries({ queryKey: ['files'] }); await queryClient.invalidateQueries({ queryKey: ['file'] }); }, onError: (error: Error) => message.error(error.message) });
  const [deleteTargets, setDeleteTargets] = useState<FileTarget[]>();
  const [selected, setSelected] = useState<string[]>([]);
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [tagOpen, setTagOpen] = useState(false);
  const [tagIds, setTagIds] = useState<string[]>([]);
  const view = params.get("view") === "grid" ? "grid" : "table";
  const page = Number(params.get("page") ?? 1);
  const pageSize = Number(params.get("pageSize") ?? 20);
  const query = useMemo<FileQuery>(
    () => ({
      page,
      pageSize,
      search: params.get("search") ?? undefined,
      robotId: selectedRobot() || robotFromSearch(params.toString()) || undefined,
      fileType: params.get("fileType") ?? undefined,
      recognitionType: params.get("recognitionType") ?? undefined,
      schemaVersion: params.get("schemaVersion") ?? undefined,
      tagId: params.get("tagId") ?? undefined,
      startTime: params.get("startTime") ?? undefined,
      endTime: params.get("endTime") ?? undefined,
      uploadedStartTime: params.get("uploadedStartTime") ?? undefined,
      uploadedEndTime: params.get("uploadedEndTime") ?? undefined,
      minSize: params.get("minSize") ?? undefined,
      maxSize: params.get("maxSize") ?? undefined,
      sortBy: params.get("sortBy") ?? "createdAt",
      sortOrder: params.get("sortOrder") ?? "desc",
    }),
    [params, page, pageSize],
  );
  const files = useQuery({
    queryKey: ["files", query],
    queryFn: () => api.files(query),
  });
  const advancedFilterCount = ['recognitionType', 'schemaVersion', 'tagId', 'startTime', 'endTime', 'uploadedStartTime', 'uploadedEndTime', 'minSize', 'maxSize'].filter(key => params.has(key)).length;
  const robots = useQuery({
    queryKey: ["robots", "selector"],
    queryFn: api.robots,
  });
  const tags = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const update = (values: FileQuery) =>
    setParams((current) => {
      for (const [key, value] of Object.entries(values)) {
        if (value === undefined || value === "") current.delete(key);
        else current.set(key, String(value));
      }
      return current;
    });
  const applyTags = useMutation({ mutationFn: () => api.bulkTagFiles(selected, tagIds), onSuccess: async () => { message.success("标签已应用"); setTagOpen(false); setTagIds([]); await queryClient.invalidateQueries({ queryKey: ["files"] }); } });
  const selectedRows = (files.data?.items ?? []).filter((file) =>
    selected.includes(file.id),
  );
  const confirmDelete = (ids = selected) => setDeleteTargets(ids.map(id => ({ id, originalName: files.data?.items.find(file => file.id === id)?.originalName ?? id })));
  const download = async (file: RobotFile) => {
    try {
      await downloadFile(file);
    } catch (reason) {
      message.error((reason as Error).message);
    }
  };
  return (
    <section className="data-workspace files-page">
      <PageHeader
        title="文件资产"
        description="检索、预览和整理已入仓的原始文件。"
        actions={
          <>
            <div className="header-count">
              <strong>{files.data?.total ?? "--"}</strong>
              <span>已管理文件</span>
            </div>
            {canMutate && <Button
              type="primary"
              icon={<UploadOutlined />}
              onClick={() => navigate("/upload")}
            >
              上传文件
            </Button>}
          </>
        }
      />
      <Card variant="borderless" className="filter-bar">
        <div className="filter-section-heading"><strong>查找文件</strong></div>
        <div className="file-filter-fields file-filter-primary">
          <Input.Search
            allowClear
            prefix={<SearchOutlined />}
            defaultValue={String(query.search ?? "")}
            placeholder="文件名、存储路径或校验和"
            onSearch={(value) => update({ search: value, page: 1 })}
          />
          <Select
            allowClear
            value={query.robotId}
            disabled={Boolean(selectedRobot())}
            placeholder="数据来源"
            showSearch
            optionFilterProp="label"
            onChange={(value) => update({ robotId: value, page: 1 })}
            options={robots.data?.map((robot) => ({
              value: robot.id,
              label: `${robot.externalId ?? robot.id.slice(0, 8)} · ${robot.name}`,
            }))}
          />
          <Select
            allowClear
            value={query.fileType}
            placeholder="文件类型"
            onChange={(value) => update({ fileType: value, page: 1 })}
            options={["json", "image", "text", "log"].map((value) => ({
              value,
              label: value.toUpperCase(),
            }))}
          />
        </div>
        <Button type="text" className="advanced-filter-toggle" aria-expanded={showAdvanced} aria-controls="file-advanced-filters" onClick={() => setAdvancedExpanded(!showAdvanced)}>
          <DownOutlined rotate={showAdvanced ? 180 : 0} />
          {showAdvanced ? '收起高级筛选' : '展开高级筛选'}
          {advancedFilterCount > 0 && <Tag bordered={false}>{advancedFilterCount} 项已启用</Tag>}
        </Button>
        <div id="file-advanced-filters" hidden={!showAdvanced}>
        <div className="file-filter-fields">
          <Input
            allowClear
            value={query.recognitionType}
            placeholder="识别类型"
            onChange={(event) =>
              update({ recognitionType: event.target.value, page: 1 })
            }
          />
          <Select allowClear mode="multiple" maxTagCount={1} value={query.tagId ? [query.tagId] : []} placeholder="标签" options={tags.data?.map((tag) => ({ value: tag.id, label: tag.name }))} onChange={(values) => update({ tagId: values.at(-1), page: 1 })} />
          <Input
            allowClear
            value={query.schemaVersion}
            placeholder="格式版本"
            onChange={(event) =>
              update({ schemaVersion: event.target.value, page: 1 })
            }
          />
        </div>
        <div className="filter-section-heading filter-section-heading-secondary"><strong>时间与大小</strong></div>
        <div className="file-filter-ranges">
          <div className="file-filter-field" role="group" aria-label="采集时间筛选">
          <span className="file-filter-label">采集时间</span>
          <DatePicker.RangePicker
            showTime
            format="YYYY-MM-DD HH:mm"
            placeholder={["开始时间", "结束时间"]}
            onChange={(range) =>
              update({
                startTime: range?.[0]?.toISOString(),
                endTime: range?.[1]?.toISOString(),
                page: 1,
              })
            }
          />
          </div>
          <div className="file-filter-field" role="group" aria-label="上传时间筛选">
          <span className="file-filter-label">上传时间</span>
          <DatePicker.RangePicker
            showTime
            format="YYYY-MM-DD HH:mm"
            placeholder={["开始时间", "结束时间"]}
            onChange={(range) =>
              update({
                uploadedStartTime: range?.[0]?.toISOString(),
                uploadedEndTime: range?.[1]?.toISOString(),
                page: 1,
              })
            }
          />
          </div>
          <div className="file-filter-field" role="group" aria-label="文件大小筛选">
          <span className="file-filter-label">文件大小（字节）</span>
          <Space.Compact className="file-size-range" block>
            <InputNumber
              min={0}
              aria-label="最小字节数"
              placeholder="最小值"
              value={query.minSize ? Number(query.minSize) : null}
              onChange={(value) =>
                update({ minSize: value ?? undefined, page: 1 })
              }
            />
            <InputNumber
              min={0}
              aria-label="最大字节数"
              placeholder="最大值"
              value={query.maxSize ? Number(query.maxSize) : null}
              onChange={(value) =>
                update({ maxSize: value ?? undefined, page: 1 })
              }
            />
          </Space.Compact>
          </div>
        </div>
        </div>
        <Space wrap className="filter-footer">
          <Select
            value={`${query.sortBy}:${query.sortOrder}`}
            onChange={(value) => {
              const [sortBy, sortOrder] = value.split(":");
              update({ sortBy, sortOrder, page: 1 });
            }}
            options={[
              { value: "createdAt:desc", label: "上传时间从新到旧" },
              { value: "capturedAt:desc", label: "采集时间从新到旧" },
              { value: "fileSize:desc", label: "文件大小降序" },
              { value: "originalName:asc", label: "文件名升序" },
            ]}
          />
          <Button
            onClick={() =>
              setParams(params.get("file") ? { file: params.get("file")! } : {})
            }
          >
            重置
          </Button>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => void files.refetch()}
          >
            刷新
          </Button>
          <Segmented
            value={view}
            onChange={(value) => update({ view: value })}
            options={[
              { value: "table", icon: <TableOutlined /> },
              { value: "grid", icon: <AppstoreOutlined /> },
            ]}
          />
          <SavedViews resource="FILES" query={{ ...query, view }} onApply={(saved) => setParams(Object.fromEntries(Object.entries(saved).map(([key, value]) => [key, String(value)])))} />
        </Space>
      </Card>
      <DataState
        loading={files.isLoading}
        error={files.error instanceof Error ? files.error.message : undefined}
        empty={!files.data?.items.length}
        emptyTitle="暂无文件"
        action={canMutate ? (
          <Button
            type="primary"
            icon={<UploadOutlined />}
            onClick={() => navigate("/upload")}
          >
            上传文件
          </Button>
        ) : undefined}
      >
        {view === "table" ? (
          <Card variant="borderless" className="table-panel">
            <Table
              rowKey="id"
              dataSource={files.data?.items ?? []}
              rowSelection={canMutate ? {
                selectedRowKeys: selected,
                onChange: (keys) => setSelected(keys.map(String)),
                preserveSelectedRowKeys: true,
              } : undefined}
              scroll={{ x: 1420 }}
              onRow={(row) => ({
                onClick: () => update({ file: row.id }),
                className: "clickable-row",
              })}
              pagination={{
                current: page,
                pageSize,
                total: files.data?.total,
                showSizeChanger: true,
                pageSizeOptions: [10, 20, 50, 100],
                showTotal: (total) => `共 ${total} 个文件`,
                onChange: (nextPage, nextSize) =>
                  update({ page: nextPage, pageSize: nextSize }),
              }}
              columns={[
                {
                  title: "文件",
                  width: 260,
                  fixed: screens.lg ? "left" : undefined,
                  render: (_, row) => (
                    <div>
                      <Typography.Text strong className="asset-file-name" title={row.originalName}>
                        {row.originalName}
                      </Typography.Text>
                      <small className="table-secondary mono-value asset-object-key" title={row.objectKey}>
                        {row.objectKey}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "数据来源",
                  width: 150,
                  render: (_, row) =>
                    row.robot?.externalId ??
                    row.robot?.name ??
                    row.robotId.slice(0, 8),
                },
                {
                  title: "类型",
                  dataIndex: "fileType",
                  width: 85,
                  render: (_, row) => <Tag>{fileKindLabel(row).toUpperCase()}</Tag>,
                },
                {
                  title: "识别类型",
                  dataIndex: "recognitionType",
                  width: 150,
                },
                { title: "数据格式", dataIndex: "schemaVersion", width: 90 },
                {
                  title: "采集时间",
                  dataIndex: "capturedAt",
                  width: 175,
                  render: date,
                },
                {
                  title: "上传时间",
                  dataIndex: "createdAt",
                  width: 175,
                  render: date,
                },
                {
                  title: "大小",
                  dataIndex: "fileSize",
                  width: 100,
                  align: "right",
                  render: bytes,
                },
                {
                  title: "校验和",
                  dataIndex: "checksum",
                  width: 145,
                  render: (value) => (
                    <Tooltip
                      title={
                        <Typography.Text copyable>{value}</Typography.Text>
                      }
                    >
                      <Typography.Text className="mono-value">
                        {shortHash(value)}
                      </Typography.Text>
                    </Tooltip>
                  ),
                },
                {
                  title: "操作",
                  width: canMutate ? 365 : 100,
                  fixed: screens.lg ? "right" : undefined,
                  render: (_, row) => (
                    <Space onClick={(event) => event.stopPropagation()}>
                      <Tooltip title="预览">
                        <Button
                          type="text"
                          icon={<EyeOutlined />}
                          onClick={() => update({ file: row.id })}
                        />
                      </Tooltip>
                      <Tooltip title="下载">
                        <Button
                          type="text"
                          icon={<DownloadOutlined />}
                          onClick={() => void download(row)}
                        />
                      </Tooltip>
                      {canMutate && <Tooltip title="编辑文件信息"><Button type="text" aria-label="编辑文件信息" icon={<EditOutlined />} onClick={() => { setEditing(row); setEditType(row.recognitionType); setEditMetadata(JSON.stringify(row.metadata ?? {}, null, 2)); }} /></Tooltip>}
                      <RelocateFileButton file={row} /><FileReferencesButton file={row} />{canMutate && <Tooltip title="删除">
                        <Button
                          danger
                          type="text"
                          icon={<DeleteOutlined />}
                          onClick={() => confirmDelete([row.id])}
                        />
                      </Tooltip>}
                    </Space>
                  ),
                },
              ]}
            />
          </Card>
        ) : (
          <div className="file-grid">
            {files.data?.items.map((file) => (
              <FileGridCard
                key={file.id}
                file={file}
                selected={selected.includes(file.id)}
                onSelect={() =>
                  setSelected((current) =>
                    current.includes(file.id)
                      ? current.filter((id) => id !== file.id)
                      : [...current, file.id],
                  )
                }
                onOpen={() => update({ file: file.id })}
              />
            ))}
          </div>
        )}
      </DataState>
      {canMutate && <BulkActionBar
        count={selected.length}
        onAddDataset={() => setDatasetOpen(true)}
        onTag={() => setTagOpen(true)}
        onExportCsv={() => exportRows(selectedRows, "csv")}
        onExportJson={() => exportRows(selectedRows, "json")}
        onDelete={() => confirmDelete()}
        onClear={() => setSelected([])}
      />}
      {canMutate && <AddToDatasetDrawer
        open={datasetOpen}
        fileIds={selected}
        onClose={() => setDatasetOpen(false)}
        onDone={() => {
          setDatasetOpen(false);
          setSelected([]);
        }}
      />}
      <Modal open={Boolean(editing)} title="编辑文件信息" okText="保存" cancelText="取消" confirmLoading={edit.isPending} okButtonProps={{ disabled: !editType.trim() }} onCancel={() => setEditing(undefined)} onOk={() => edit.mutate()}><p>冻结版本引用的文件不可修改。</p><label>识别分类<Input value={editType} onChange={event => setEditType(event.target.value)} /></label><label>元信息（JSON 对象）<Input.TextArea rows={8} value={editMetadata} onChange={event => setEditMetadata(event.target.value)} /></label></Modal>
      {deleteTargets && <FileDeleteDialog files={deleteTargets} onClose={() => setDeleteTargets(undefined)} onDeleted={ids => setSelected(current => current.filter(id => !ids.includes(id)))} />}
      <FilePreview
        fileId={params.get("file") ?? undefined}
        onDeleted={(ids) => setSelected(current => current.filter(id => !ids.includes(id)))}
        onClose={() => update({ file: undefined })}
      />
      {canMutate && <Modal open={tagOpen} title={`为 ${selected.length} 个文件添加标签`} okText="应用标签" okButtonProps={{ disabled: !tagIds.length }} confirmLoading={applyTags.isPending} onCancel={() => setTagOpen(false)} onOk={() => applyTags.mutate()}><Select mode="multiple" value={tagIds} onChange={setTagIds} style={{ width: "100%" }} placeholder="选择标签" options={tags.data?.map((tag) => ({ value: tag.id, label: tag.name }))} /></Modal>}
    </section>
  );
}

function FileGridCard({
  file,
  selected,
  onSelect,
  onOpen,
}: {
  file: RobotFile;
  selected: boolean;
  onSelect: () => void;
  onOpen: () => void;
}) {
  return (
    <Card
      className={`file-grid-card ${selected ? "selected" : ""}`}
      variant="borderless"
    >
      <button
        className="grid-select"
        onClick={onSelect}
        aria-label={`选择 ${file.originalName}`}
      >
        {selected ? "✓" : ""}
      </button>
      <button className="file-grid-main" onClick={onOpen}>
        <span className="file-type-icon">
          {file.mimeType.startsWith("image/") ? <AppstoreOutlined /> : "{ }"}
        </span>
        <Typography.Text strong ellipsis={{ tooltip: file.originalName }}>
          {file.originalName}
        </Typography.Text>
        <small>
          {file.robot?.externalId ?? file.robot?.name ?? "数据来源"} ·{" "}
          {bytes(file.fileSize)}
        </small>
        <div>
          <Tag>{fileKindLabel(file).toUpperCase()}</Tag>
          <Tag>{file.recognitionType}</Tag>
        </div>
        <Typography.Text type="secondary">
          {date(file.createdAt)}
        </Typography.Text>
      </button>
    </Card>
  );
}
function exportRows(rows: RobotFile[], format: "csv" | "json") {
  const fields = rows.map((row) => ({
    id: row.id,
    fileName: row.originalName,
    robot: row.robot?.externalId ?? row.robot?.name ?? row.robotId,
    objectKey: row.objectKey,
    fileType: row.fileType,
    mimeType: row.mimeType,
    fileSize: row.fileSize,
    checksum: row.checksum,
    recognitionType: row.recognitionType,
    schemaVersion: row.schemaVersion,
    capturedAt: row.capturedAt,
    uploadedAt: row.createdAt,
    metadata: row.metadata ?? {},
  }));
  const content =
    format === "json" ? JSON.stringify(fields, null, 2) : toCsv(fields);
  const blob = new Blob([content], {
    type: format === "json" ? "application/json" : "text/csv;charset=utf-8",
  });
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(blob);
  anchor.download = `robot-files-${new Date().toISOString().slice(0, 10)}.${format}`;
  anchor.click();
  URL.revokeObjectURL(anchor.href);
}
function toCsv(rows: Array<Record<string, unknown>>) {
  if (!rows.length) return "";
  const headers = Object.keys(rows[0]);
  const escape = (value: unknown) =>
    `"${String(typeof value === "object" ? JSON.stringify(value) : (value ?? "")).replaceAll('"', '""')}"`;
  return [
    headers.join(","),
    ...rows.map((row) =>
      headers.map((header) => escape(row[header])).join(","),
    ),
  ].join("\n");
}
