import { observerRobotUrl, rememberObserverRobot } from '../services/robot-selection';
import { useRobotSelection } from './useRobotSelection';
import { auditRoles, defaultWarehousePath, readRoles, roleNames } from '../auth/permissions';
import { ApiOutlined, ArrowLeftOutlined, AuditOutlined, BellOutlined, CloudServerOutlined, DatabaseOutlined, DownOutlined, FileOutlined, FolderOutlined, HddOutlined, LogoutOutlined, MenuFoldOutlined, MenuUnfoldOutlined, PlusOutlined, RobotOutlined, SearchOutlined, SettingOutlined, TeamOutlined, UploadOutlined } from '@ant-design/icons';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Avatar, Badge, Button, Drawer, Dropdown, Flex, Layout, List, Menu, Select, Typography } from 'antd';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { api } from '../services/api';
import { warehouseApi } from '../services/warehouse';
import { CommandPalette } from './CommandPalette';
import { CreateDatasetModal } from './DatasetDrawers';
import { useUnsavedChanges } from './UnsavedChanges';
import { useAuth } from '../auth/AuthContext';
import { date } from '../services/format';
import { WorkspaceShortcutsProvider, WorkspaceShortcutsTrigger } from './WorkspaceShortcuts';
import './industrial-shell.css';

const { Header, Content, Sider } = Layout;
const labels: Record<string, string> = { '/ai': 'AI 助手', '/recycle-bin': '文件回收站', '/files': '文件资产', '/upload': '上传中心', '/datasets': '数据集', '/dataset-versions': '数据集版本', '/jobs': '任务与审计', '/activity': '活动记录', '/storage': '存储管理', '/system-status': '系统状态', '/system-settings': '系统设置', '/users': '用户管理', '/audit-logs': '任务与审计' };
const pageIcons: Record<string, ReactNode> = { '/ai': <ApiOutlined />, '/recycle-bin': <FolderOutlined />, '/files': <FileOutlined />, '/upload': <UploadOutlined />, '/datasets': <FolderOutlined />, '/dataset-versions': <DatabaseOutlined />, '/jobs': <AuditOutlined />, '/storage': <HddOutlined />, '/system-status': <CloudServerOutlined />, '/system-settings': <SettingOutlined />, '/users': <TeamOutlined /> };
const navigationIcon = (icon: ReactNode) => <span className="navigation-icon" aria-hidden="true">{icon}</span>;

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem('warehouse-sidebar-collapsed') === 'true'; } catch { return false; } });
  useEffect(() => { try { localStorage.setItem('warehouse-sidebar-collapsed', String(collapsed)); } catch { /* Storage can be unavailable in private browsing. */ } }, [collapsed]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [createDatasetOpen, setCreateDatasetOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)');
    const update = () => { setMobile(media.matches); if (!media.matches) setMobileMenuOpen(false); };
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  const auth = useAuth();
  const canRead = auth.hasRole(...readRoles);
  const canAudit = auth.hasRole(...auditRoles);
  const canCreate = auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR');
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { robotId, transitioning, switchRobot: selectRobotRoute } = useRobotSelection();
  const fleet = useQuery({ queryKey: ['fleet-robots'], queryFn: api.fleetRobots, enabled: canRead });
  useQuery({ queryKey: ['observer-fleet-links'], enabled: canRead && import.meta.env.BASE_URL !== '/', retry: false,
    queryFn: async () => {
      const response = await fetch('/api/robots', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) return null;
      const value = await response.json() as { robots?: Array<{ robotId?: string; warehouseId?: string }> };
      if (Array.isArray(value.robots)) for (const robot of value.robots) {
        if (typeof robot.robotId === 'string' && typeof robot.warehouseId === 'string') rememberObserverRobot(robot.warehouseId, robot.robotId);
      }
      return null;
    },
  });
  const observerReturn = observerRobotUrl(robotId, fleet.data)
    ?? (fleet.data?.some(robot => robot.id === robotId && !robot.observerId) ? '/' : undefined);
  const { confirmDiscard } = useUnsavedChanges();
  const healthQuery = useQuery({ queryKey: ['warehouse-status'], queryFn: warehouseApi.status, refetchInterval: 30_000, retry: 1 });
  const health = healthQuery.data ?? { services: { api: { status: 'not_checked' }, postgresql: { status: 'not_checked' } } };
  const jobs = useQuery({ queryKey: ['jobs', 'header'], queryFn: () => api.jobs({ page: 1, pageSize: 1, status: 'RUNNING' }), refetchInterval: 10_000, enabled: canRead });
  const notifications = useQuery({ queryKey: ['notifications', 'header'], queryFn: () => api.notifications({ page: 1, pageSize: 20 }), refetchInterval: 10_000 });
  const menuItems = useMemo(() => [
    ...(canRead ? [{ type: 'group' as const, label: '数据工作区', children: [
      { key: '/datasets', icon: navigationIcon(<FolderOutlined />), label: '数据集' },
      { key: '/files', icon: navigationIcon(<FileOutlined />), label: '文件' },
      ...(canCreate ? [
        { key: '/upload', icon: navigationIcon(<UploadOutlined />), label: '上传中心' },
        { key: '/recycle-bin', icon: navigationIcon(<FolderOutlined />), label: '回收站' },
      ] : []),
      { key: '/dataset-versions', icon: navigationIcon(<DatabaseOutlined />), label: '数据版本' },
    ] }] : []),
    { key: '/ai', icon: navigationIcon(<ApiOutlined />), label: 'AI 助手' },
    ...(auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN') ? [{ type: 'group' as const, label: '平台管理', children: [
      { key: '/users', icon: navigationIcon(<TeamOutlined />), label: '用户管理' },
      ...(auth.hasRole('ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN') ? [
        { key: '/storage', icon: navigationIcon(<HddOutlined />), label: '存储管理' },
        { key: '/system-settings', icon: navigationIcon(<SettingOutlined />), label: '系统设置' },
      ] : []),
    ] }] : []),
  ], [auth, canRead, canCreate]);
  const utilityItems = [
    ...(canRead ? [{ key: '/jobs', icon: navigationIcon(<AuditOutlined />), label: canAudit ? '任务与审计' : '数据处理任务' }] : []),
    { key: '/system-status', icon: navigationIcon(<CloudServerOutlined />), label: '系统状态' },
  ];
  const isTaskPage = location.pathname === '/jobs' || location.pathname.startsWith('/jobs/') || location.pathname === '/audit-logs';
  const pageLabel = (isTaskPage ? (canAudit ? '任务与审计' : '数据处理任务') : labels[location.pathname]) ?? (location.pathname.startsWith('/datasets/') ? '数据集详情' : '内部数仓');
  const selectedKey = isTaskPage ? '/jobs' : location.pathname.startsWith('/datasets/') ? '/datasets' : location.pathname;
  const goTo = (path: string) => confirmDiscard(() => { navigate(path); setMobileMenuOpen(false); });
  const switchRobot = (value: string) => confirmDiscard(() => selectRobotRoute(value));
  const compact = !mobile && collapsed;
  const healthy = !healthQuery.isError && health.services.api.status === 'healthy';
  const serviceState = healthQuery.isPending ? 'checking' : healthy ? 'online' : 'attention';
  const selectedRobot = fleet.data?.find(robot => robot.id === robotId);
  const robotLabel = robotId ? selectedRobot?.name || selectedRobot?.externalId || '当前机器人' : '全部机器人';
  const workspaceSection = ['/jobs', '/system-status'].includes(selectedKey) ? '运行与记录'
    : ['/users', '/storage', '/system-settings'].includes(selectedKey) ? '平台管理' : selectedKey === '/ai' ? 'AI 助手' : '数据工作区';
  const runningJobCount = jobs.isError || !jobs.data ? '—' : String(jobs.data.total);
  const navigation = <div className={`navigation-shell ${compact ? 'is-compact' : ''}`}>
    <button className="brand" onClick={() => goTo(defaultWarehousePath(auth.user?.role))} aria-label="返回数仓">
      <span className="brand-mark"><DatabaseOutlined /></span>
      {!compact && <span className="brand-copy"><strong>内部数仓平台</strong></span>}
    </button>
    <nav className="navigation-main" aria-label="数仓主导航">
      {canRead && <WorkspaceShortcutsTrigger compact={compact} onOpen={() => setMobileMenuOpen(false)} />}
      <Menu theme="light" mode="inline" inlineCollapsed={compact} selectedKeys={[selectedKey]} items={menuItems} onClick={({ key }) => goTo(key)} />
    </nav>
    <div className="navigation-bottom">
      {!compact && <div className="navigation-section-label">运行与记录</div>}
      <nav aria-label="数仓运行与记录"><Menu theme="light" mode="inline" inlineCollapsed={compact} selectedKeys={[selectedKey]} items={utilityItems} onClick={({ key }) => goTo(key)} /></nav>
      {import.meta.env.BASE_URL !== '/' && <a className="observer-return" href={observerReturn} aria-disabled={!observerReturn} aria-label="返回观测平台"><ArrowLeftOutlined />{!compact && <span>返回观测平台</span>}</a>}
      <button type="button" className={`navigation-health is-${serviceState}`} onClick={() => goTo('/system-status')} aria-label="打开系统状态" title={healthQuery.isPending ? '正在检查服务状态' : healthy ? '服务在线' : '服务状态异常，请查看系统状态'}>
        <span className="navigation-health-dot" />{!compact && <span>{healthQuery.isPending ? '检查中' : healthy ? '服务在线' : '服务异常'}</span>}
        {!compact && <span className="navigation-health-arrow" aria-hidden="true">↗</span>}
      </button>
    </div>
  </div>;
  return <WorkspaceShortcutsProvider userId={auth.user?.id} robots={fleet.data}><Layout className="app-shell warehouse-shell industrial-shell">
    <a className="workspace-skip-link" href="#warehouse-main">跳至内容</a>
    {!mobile && <Sider className="app-sider" width={240} collapsedWidth={76} collapsible collapsed={collapsed} trigger={null} breakpoint="lg" onBreakpoint={(broken) => { if (broken) setCollapsed(true); }}>{navigation}</Sider>}
    {mobile && <Drawer className="navigation-drawer warehouse-shell industrial-shell" placement="left" width={280} title="数仓导航" open={mobileMenuOpen} onClose={() => setMobileMenuOpen(false)} styles={{ body: { padding: 0 } }}>{navigation}</Drawer>}
    <Layout>
      <Header className="app-header"><Flex align="center" justify="space-between" gap={12}>
        <Flex align="center" gap={12} className="header-leading">
          <Button aria-label={(mobile ? !mobileMenuOpen : collapsed) ? '展开导航' : '收起导航'} aria-expanded={mobile ? mobileMenuOpen : !collapsed} type="text" className="collapse-button" icon={(mobile ? !mobileMenuOpen : collapsed) ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => mobile ? setMobileMenuOpen((value) => !value) : setCollapsed((value) => !value)} />
          <div className="module-context"><span className="module-context-icon" aria-hidden="true">{pageIcons[selectedKey] ?? <DatabaseOutlined />}</span><strong>{pageLabel}</strong></div>
        </Flex>
        <div className="header-tools">
          {canRead && <div className={`header-fleet-context ${robotId ? 'is-selected' : ''}`}><RobotOutlined aria-hidden="true" /><Select className="robot-fleet-select" aria-label="选择机器人" value={robotId || ''}
            loading={fleet.isFetching || transitioning} disabled={fleet.isError || transitioning} showSearch optionFilterProp="label"
            options={[{ value: '', label: '全部机器人' }, ...(fleet.data ?? []).map(robot => ({ value: robot.id, label: robot.name || robot.externalId || robot.id }))]}
            onChange={switchRobot} /></div>}

          {canRead && <Button aria-label="全局搜索" className="global-search-button" icon={<SearchOutlined />} onClick={() => setPaletteOpen(true)}><span className="search-label">搜索数据、文件…</span><kbd>Ctrl K</kbd></Button>}
          {canCreate && <Dropdown trigger={['click']} menu={{ items: [
            { key: 'upload', icon: <UploadOutlined />, label: '上传文件', onClick: () => goTo('/upload') },
            { key: 'dataset', icon: <FolderOutlined />, label: '创建数据集', onClick: () => setCreateDatasetOpen(true) },
          ] }}><Button className="header-create" icon={<PlusOutlined />}>新建<DownOutlined /></Button></Dropdown>}
          <Badge count={notifications.data?.unread ?? 0} size="small"><Button type="text" className="notification-button" aria-label="通知" icon={<BellOutlined />} onClick={() => setNotificationsOpen(true)} /></Badge>
          <span className="header-divider" />
          <Dropdown trigger={['click']} menu={{ items: [{ key: 'role', label: `${auth.user ? roleNames[auth.user.role] : '--'} 角色`, disabled: true }, { type: 'divider' }, { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', onClick: () => void auth.logout().then(() => navigate('/login')) }] }}>
            <button className="system-entry" aria-label="账户菜单"><Avatar size={34}>{auth.user?.displayName?.slice(0, 1).toUpperCase()}</Avatar><span className="account-copy"><Typography.Text strong>{auth.user?.displayName}</Typography.Text><small>{auth.user && roleNames[auth.user.role]}</small></span><DownOutlined className="account-chevron" /></button>
          </Dropdown>
        </div>
      </Flex></Header>
      <div className="workspace-contextbar" aria-label="工作区上下文">
        <nav className="workspace-breadcrumb" aria-label="当前位置">{workspaceSection !== pageLabel && <><span>{workspaceSection}</span><span className="workspace-breadcrumb-separator" aria-hidden="true">/</span></>}<span aria-current="page">{pageLabel}</span></nav>
        <div className="workspace-context-actions">
          {canRead && <span className="workspace-scope" title={robotLabel}><RobotOutlined aria-hidden="true" /><span>{robotLabel}</span>{!robotId && fleet.data && <b>{fleet.data.length}</b>}</span>}
          {canRead && <button type="button" className={`workspace-running-tasks ${(jobs.data?.total ?? 0) > 0 ? 'has-running-tasks' : ''}`} onClick={() => goTo('/jobs')} title="当前账号可见的全部运行任务" aria-label={jobs.isError ? '任务状态暂不可用，打开任务列表' : jobs.data ? `${jobs.data.total} 个任务运行中，打开任务列表` : '正在查询运行任务'}><span className="workspace-task-indicator" aria-hidden="true" /><span>运行任务</span><b>{runningJobCount}</b><DownOutlined aria-hidden="true" /></button>}
        </div>
      </div>
      <Content id="warehouse-main" tabIndex={-1} aria-busy={transitioning} className={`app-content ${location.pathname === '/ai' ? 'ai-route-content' : ''} ${location.pathname.startsWith('/datasets') ? 'dataset-route-content' : ''}`}>{transitioning && <div className="workspace-route-progress" role="progressbar" aria-label="切换机器人" />}<div className="route-stage" key={location.pathname}>{!transitioning && <Outlet key={robotId} />}</div></Content>
    </Layout>
    {canRead && <CommandPalette key={robotId} open={paletteOpen && !transitioning} onClose={() => setPaletteOpen(false)} onCreateDataset={() => { setPaletteOpen(false); setCreateDatasetOpen(true); }} />}
    <CreateDatasetModal key={robotId} open={createDatasetOpen && !transitioning} onClose={() => setCreateDatasetOpen(false)} onDone={() => { setCreateDatasetOpen(false); void queryClient.invalidateQueries({ queryKey: ['datasets'] }); void queryClient.invalidateQueries({ queryKey: ['dashboard'] }); }} />
    <Drawer open={notificationsOpen} onClose={() => setNotificationsOpen(false)} title="平台通知" extra={<Button type="link" onClick={() => void api.readAllNotifications().then(() => queryClient.invalidateQueries({ queryKey: ['notifications'] }))}>全部标记已读</Button>}><List dataSource={notifications.data?.items ?? []} locale={{ emptyText: '暂无通知' }} renderItem={(item) => <List.Item className={item.readAt ? '' : 'notification-unread'} onClick={() => { void api.readNotification(item.id).then(() => queryClient.invalidateQueries({ queryKey: ['notifications'] })); if (item.resourceUrl) navigate(item.resourceUrl); setNotificationsOpen(false); }}><List.Item.Meta title={item.title} description={<>{item.message}<small className="table-secondary">{date(item.createdAt)}</small></>} /></List.Item>} /></Drawer>
  </Layout></WorkspaceShortcutsProvider>;
}
