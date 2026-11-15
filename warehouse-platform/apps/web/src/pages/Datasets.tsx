import { selectedRobot, robotPath } from '../services/robot-selection';
import { datasetRobotId } from '../services/dataset-robot';
import { DatasetRobotCategories } from '../components/DatasetRobotCategories';
import './data-workspace.css';
import './industrial-datasets.css';
import { statusNames } from '../services/labels';
import { AppstoreOutlined, DatabaseOutlined, FileOutlined, FilterOutlined, HddOutlined, HistoryOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, UnorderedListOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Col, Input, Pagination, Row, Select, Space, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { CreateDatasetModal } from "../components/DatasetDrawers";
import { DatasetCard } from "../components/DatasetCard";
import { DatasetList } from '../components/DatasetList';
import { DataState, PageHeader } from "../components/ui";
import { SavedViews } from "../components/SavedViews";
import { api } from "../services/api";
import { useOptionalAuth } from "../auth/AuthContext";
import { bytes } from '../services/format';

export function Datasets() {
  const auth = useOptionalAuth();
  const canMutate = auth ? auth.hasRole("ADMIN", "SUPER_ADMIN", "SYSTEM_ADMIN", "TENANT_ADMIN", "PROJECT_ADMIN", "OPERATOR") : true;
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [view, setView] = useState<'grid' | 'list'>(() => {
    try { return localStorage.getItem('warehouse-dataset-view') === 'list' ? 'list' : 'grid'; } catch { return 'grid'; }
  });
  const changeView = (next: 'grid' | 'list') => {
    setView(next);
    try { localStorage.setItem('warehouse-dataset-view', next); } catch { /* The current session still keeps its preference. */ }
  };
  const robotId = selectedRobot();
  const robots = useQuery({ queryKey: ['fleet-robots'], queryFn: api.fleetRobots });
  const robotNames = new Map(robots.data?.map(robot => [robot.id, robot.name]));
  const robotName = robotNames.get(robotId);
  const page = Number(params.get("page") ?? 1);
  const pageSize = Number(params.get("pageSize") ?? 12);
  const search = params.get("search") ?? "";
  const status = params.get("status") ?? undefined;
  const tagId = params.get("tagId") ?? undefined;
  const sort = params.get("sort") ?? "updatedAt:desc";
  const [filtersOpen, setFiltersOpen] = useState(Boolean(status || tagId));
  const [searchText, setSearchText] = useState(search);
  useEffect(() => setSearchText(search), [search]);
  const navigationState = { datasetsReturnTo: location.pathname + location.search };
  const legacyDataset = params.get("dataset") ?? undefined;
  const legacyVersion = params.get("version") ?? undefined;
  const legacyVersionQuery = useQuery({
    queryKey: ["datasets", "legacy-version", legacyVersion],
    queryFn: () => api.version(legacyVersion!),
    enabled: Boolean(legacyVersion),
  });
  const datasets = useQuery({
    queryKey: ["datasets", "page", robotId, page, pageSize, search, status, tagId, sort],
    queryFn: () => {
      const [sortBy, sortOrder] = sort.split(":");
      return api.datasetPage({
        page,
        pageSize,
        robotId: robotId || undefined,
        search,
        status,
        tagId,
        sortBy,
        sortOrder,
      });
    },
  });
  const pageItems = datasets.data?.items ?? [];
  const latestVersions = pageItems.flatMap(dataset => dataset.latestVersion ? [dataset.latestVersion] : dataset.versions.slice(0, 1));
  const pageVersionCount = pageItems.reduce((total, dataset) => total + (dataset.versionCount ?? dataset.versions.length), 0);
  const pageFileCount = latestVersions.reduce((total, version) => total + version.fileCount, 0);
  const pageBytes = latestVersions.reduce((total, version) => total + Number(version.totalSize), 0);
  const hasPageData = Boolean(datasets.data) && !datasets.isError;
  const update = (values: Record<string, string | number | undefined>) =>
    setParams((current) => {
      for (const [key, value] of Object.entries(values)) {
        if (value === undefined || value === "") current.delete(key);
        else current.set(key, String(value));
      }
      return current;
    });
  const tags = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const applySavedView = (saved: Record<string, string | number | boolean>) => {
    const next = { ...saved };
    if (next.sortBy && next.sortOrder) next.sort = `${next.sortBy}:${next.sortOrder}`;
    delete next.sortBy;
    delete next.sortOrder;
    const nextParams = new URLSearchParams(Object.fromEntries(Object.entries(next).map(([key, value]) => [key, String(value)])));
    nextParams.set('robotId', robotId || 'all');
    nextParams.set('page', '1');
    setParams(nextParams);
  };
  if (legacyDataset)
    return <Navigate replace to={`/datasets/${legacyDataset}`} />;
  if (legacyVersionQuery.data)
    return (
      <Navigate
        replace
        to={`/datasets/${legacyVersionQuery.data.datasetId}/versions/${legacyVersionQuery.data.id}`}
      />
    );
  return (
    <section className="data-workspace datasets-page industrial-datasets">
      <PageHeader
        title="数据集"
        description="按采集场景组织原始文件，集中管理数据与版本。"
        actions={canMutate ? (
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            创建数据集
          </Button>
        ) : undefined}
      />
      <div className="dataset-inventory-strip" aria-label="数据集概览" aria-busy={datasets.isLoading}>
        <div className="dataset-inventory-cell"><span className="dataset-inventory-icon"><DatabaseOutlined /></span><div><span>筛选结果</span><strong>{hasPageData ? datasets.data!.total.toLocaleString() : '—'}<small>个数据集</small></strong><em>本页 {hasPageData ? pageItems.length : '—'} 项</em></div></div>
        <div className="dataset-inventory-cell"><span className="dataset-inventory-icon"><HistoryOutlined /></span><div><span>本页版本</span><strong>{hasPageData ? pageVersionCount.toLocaleString() : '—'}<small>个版本</small></strong><em>已建版本的数据集 {hasPageData ? latestVersions.length : '—'}</em></div></div>
        <div className="dataset-inventory-cell"><span className="dataset-inventory-icon"><FileOutlined /></span><div><span>本页最新版本文件</span><strong>{hasPageData ? pageFileCount.toLocaleString() : '—'}<small>个文件</small></strong><em>最新版本合计</em></div></div>
        <div className="dataset-inventory-cell"><span className="dataset-inventory-icon"><HddOutlined /></span><div><span>本页最新版本容量</span><strong>{hasPageData ? bytes(pageBytes) : '—'}</strong><em>最新版本合计</em></div></div>
      </div>
      <div className="dataset-control-deck">
      <DatasetRobotCategories robots={robots.data ?? []} selected={robotId} loading={robots.isPending}
        error={robots.isError} onRetry={() => void robots.refetch()} onSelect={id => {
          const next = new URLSearchParams(params); next.delete('page');
          navigate(robotPath(`/datasets?${next.toString()}`, id));
        }} />
      <Card variant="borderless" className="filter-bar dataset-search-bar">
        <div className="dataset-query-heading"><span><SearchOutlined />检索与筛选</span><div className="dataset-saved-views"><SavedViews resource="DATASETS" query={{ search, status, tagId, sortBy: sort.split(":")[0], sortOrder: sort.split(":")[1], pageSize }} onApply={applySavedView} /></div></div>
        <div className="dataset-search-row">
          <Input.Search
            allowClear
            prefix={<SearchOutlined />}
            aria-label="搜索数据集"
            value={searchText}
            onChange={event => setSearchText(event.target.value)}
            placeholder="搜索数据集名称或说明"
            onSearch={(value) => update({ search: value, page: 1 })}
          />
          <Select aria-label="数据集排序" value={sort} onChange={value => update({ sort: value, page: 1 })} options={[
            { value: 'updatedAt:desc', label: '最近更新' },
            { value: 'createdAt:desc', label: '最近创建' },
            { value: 'name:asc', label: '名称升序' },
          ]} />
          <Button icon={<FilterOutlined />} aria-expanded={filtersOpen} aria-controls="dataset-filters" onClick={() => setFiltersOpen(open => !open)}>筛选{status || tagId ? `（${Number(Boolean(status)) + Number(Boolean(tagId))}）` : ''}</Button>
        </div>
        <div id="dataset-filters" className="dataset-secondary-filters" hidden={!filtersOpen}>
          <Select aria-label="版本状态"
            allowClear
            value={status}
            placeholder="版本状态"
            onChange={(value) => update({ status: value, page: 1 })}
            options={["DRAFT", "FROZEN", "PUBLISHED", "ARCHIVED"].map(
              (value) => ({ value, label: statusNames[value] ?? value }),
            )}
          />
          <Select aria-label="标签" allowClear value={tagId} placeholder="标签" onChange={(value) => update({ tagId: value, page: 1 })} options={tags.data?.map((tag) => ({ value: tag.id, label: tag.name }))} />
          {(status || tagId) && <Button type="text" onClick={() => update({ status: undefined, tagId: undefined, page: 1 })}>清除筛选</Button>}
        </div>
        {(search || status || tagId) && <div className="dataset-active-filters" aria-label="已应用筛选">
          <span>已筛选</span>
          {search && <Tag closable onClose={event => { event.preventDefault(); update({ search: undefined, page: 1 }); }}>搜索：{search}</Tag>}
          {status && <Tag closable onClose={event => { event.preventDefault(); update({ status: undefined, page: 1 }); }}>{statusNames[status] ?? status}</Tag>}
          {tagId && <Tag closable onClose={event => { event.preventDefault(); update({ tagId: undefined, page: 1 }); }}>标签：{tags.data?.find(tag => tag.id === tagId)?.name ?? '所选标签'}</Tag>}
          <Button size="small" type="text" onClick={() => update({ search: undefined, status: undefined, tagId: undefined, page: 1 })}>全部清除</Button>
        </div>}
      </Card>
      </div>
      <div className="collection-section-heading dataset-collection-toolbar">
        <div className="dataset-collection-title"><h2>{robotId ? `${robotName || '所选机器人'}的数据集` : '数据集列表'}</h2><span aria-live="polite">{datasets.data ? `${datasets.data.total} 个数据集` : "正在读取数据集"}</span></div>
        <Space size={10}>
          {datasets.data && <span className="dataset-page-position">第 {page} 页 / {Math.max(1, Math.ceil(datasets.data.total / pageSize))} 页</span>}
          <Tooltip title="刷新数据集"><Button type="text" className="dataset-refresh" aria-label="刷新数据集" icon={<ReloadOutlined />} loading={datasets.isFetching} onClick={() => void datasets.refetch()} /></Tooltip>
          <div className="dataset-view-toggle" role="group" aria-label="数据集显示方式">
            <Tooltip title="卡片视图"><Button type="text" icon={<AppstoreOutlined />} aria-label="卡片视图" aria-pressed={view === 'grid'} className={view === 'grid' ? 'is-active' : ''} onClick={() => changeView('grid')} /></Tooltip>
            <Tooltip title="列表视图"><Button type="text" icon={<UnorderedListOutlined />} aria-label="列表视图" aria-pressed={view === 'list'} className={view === 'list' ? 'is-active' : ''} onClick={() => changeView('list')} /></Tooltip>
          </div>
        </Space>
      </div>
      <DataState
        loading={
          datasets.isLoading ||
          Boolean(legacyVersion && legacyVersionQuery.isLoading)
        }
        error={
          (datasets.error as Error | null)?.message ??
          (legacyVersionQuery.error as Error | null)?.message
        }
        empty={!datasets.data?.items.length}
        emptyTitle="未找到数据集"
        action={canMutate ? (
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            创建数据集
          </Button>
        ) : undefined}
      >
        <div className={`dataset-results-workspace is-${view}`}>
        {view === 'grid' ? <Row gutter={[14, 14]} className="dataset-card-grid">
          {datasets.data?.items.map((dataset) => (
            <Col xs={24} md={12} xl={8} xxl={6} key={dataset.id}>
              <DatasetCard
                dataset={dataset}
                robotName={robotNames.get(datasetRobotId(dataset.metadata) ?? '')}
                canDelete={canMutate}
                onOpen={() => navigate(`/datasets/${dataset.id}`, { state: navigationState })}
                onVersion={(id) =>
                  navigate(`/datasets/${dataset.id}/versions/${id}`, { state: navigationState })
                }
              />
            </Col>
          ))}
        </Row> : <DatasetList datasets={datasets.data?.items ?? []} robotNames={robotNames}
          onOpen={id => navigate(`/datasets/${id}`, { state: navigationState })}
          onVersion={(id, versionId) => navigate(`/datasets/${id}/versions/${versionId}`, { state: navigationState })} />}
        <div className="dataset-results-footer"><span className="dataset-visible-range">{datasets.data && `显示 ${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, datasets.data.total)} / ${datasets.data.total}`}</span><Pagination
          className="workspace-pagination"
          current={page}
          pageSize={pageSize}
          total={datasets.data?.total}
          showSizeChanger
          pageSizeOptions={[12, 24, 48]}
          showTotal={(total) => `共 ${total} 个数据集`}
          itemRender={(_, type, original) => type === 'prev' ? <span className="pagination-direction">上一页</span> : type === 'next' ? <span className="pagination-direction">下一页</span> : original}
          onChange={(nextPage, nextSize) =>
            update({ page: nextPage, pageSize: nextSize })
          }
        /></div>
        </div>
      </DataState>
      {canMutate && <CreateDatasetModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onDone={() => {
          setCreateOpen(false);
          void queryClient.invalidateQueries({ queryKey: ["datasets"] });
        }}
      />}
    </section>
  );
}
