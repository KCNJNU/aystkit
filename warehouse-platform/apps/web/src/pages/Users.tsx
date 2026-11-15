import { PlusOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, message } from 'antd';
import { useRef, useState } from 'react';
import { useOptionalAuth } from '../auth/AuthContext';
import { assignableRoles, globalRoles, roleDescriptions, roleNames } from '../auth/permissions';
import { DataState, PageHeader } from '../components/ui';
import { api, request, type UserRole } from '../services/api';
import { date } from '../services/format';
import './admin-workspace.css';

type ManagedUser = { id: string; email: string; displayName: string; role: UserRole; tenantId?: string; projectAccess?: { projectId: string }[]; isActive: boolean; mustChangePassword: boolean; lastLoginAt?: string; createdAt: string };
type UserForm = { email: string; displayName: string; role: UserRole; tenantId?: string; projectIds?: string[]; temporaryPassword?: string; isActive?: boolean };
export function Users() {
  const auth = useOptionalAuth(); const client = useQueryClient(); const [form] = Form.useForm<UserForm>();
  const [open, setOpen] = useState(false); const [editing, setEditing] = useState<ManagedUser>();
  const [resetting, setResetting] = useState<ManagedUser>();
  const [password, setPassword] = useState(''); const [resetError, setResetError] = useState<string>();
  const [pendingAction, setPendingAction] = useState<{ id: string; kind: 'delete' | 'reset' }>();
  const acting = useRef(false);
  const [page, setPage] = useState(1); const [search, setSearch] = useState('');
  const role = Form.useWatch('role', form); const tenantId = Form.useWatch('tenantId', form) ?? auth?.user?.tenantId;
  const global = Boolean(auth?.user && globalRoles.includes(auth.user.role));
  const superAdmin = auth?.user?.role === 'SUPER_ADMIN' || auth?.user?.role === 'ADMIN';
  const roles = assignableRoles(auth?.user?.role);
  const own = Boolean(editing && editing.id === auth?.user?.id);
  const users = useQuery({ queryKey: ['users', page, search], queryFn: () => request<{ items: ManagedUser[]; total: number }>(`/v1/users/page?page=${page}&pageSize=20&search=${encodeURIComponent(search)}`) });
  const scopes = useQuery({ queryKey: ['user-scopes'], queryFn: () => request<{ tenants: { id: string; name: string }[]; projects: { id: string; name: string; tenantId: string }[] }>('/v1/users/scope-options'), enabled: open });
  const refresh = () => client.invalidateQueries({ queryKey: ['users'] });
  const save = useMutation({ mutationFn: (values: UserForm) => {
    const data = { ...values, email: values.email.trim(), displayName: values.displayName.trim() };
    if (own) { delete data.projectIds; delete data.tenantId; delete data.isActive; }
    else if (globalRoles.includes(data.role)) { delete data.tenantId; data.projectIds = []; }
    else if (data.role === 'TENANT_ADMIN' || data.role === 'BASIC') data.projectIds = [];
    if (editing) { delete data.temporaryPassword; return request(`/v1/users/${editing.id}`, { method: 'PATCH', body: JSON.stringify(data) }); }
    return api.createUser({ ...data, temporaryPassword: data.temporaryPassword! });
  }, onSuccess: async () => { message.success(editing ? '用户信息已保存，权限变化后需要重新登录' : '用户已创建，首次登录需修改临时密码'); setOpen(false); form.resetFields(); await refresh(); }, onError: (error: Error) => message.error(error.message) });
  const show = (user?: ManagedUser) => { setEditing(user); form.resetFields(); form.setFieldsValue(user ? { ...user, projectIds: user.projectAccess?.map(item => item.projectId) ?? [] } : { role: 'BASIC', isActive: true }); setOpen(true); };
  const mayManage = (user: ManagedUser) => superAdmin || roles.includes(user.role) || user.id === auth?.user?.id;
  const remove = async (user: ManagedUser) => {
    if (acting.current || user.id === auth?.user?.id || !mayManage(user)) return;
    acting.current = true; setPendingAction({ id: user.id, kind: 'delete' });
    try { await request(`/v1/users/${user.id}`, { method: 'DELETE' }); await refresh(); message.success('用户已删除'); }
    catch (error) { message.error((error as Error).message); }
    finally { acting.current = false; setPendingAction(undefined); }
  };
  const showReset = (user: ManagedUser) => { if (acting.current || !mayManage(user)) return; setResetting(user); setPassword(''); setResetError(undefined); };
  const reset = async () => {
    if (!resetting || acting.current || !mayManage(resetting)) return;
    if (password.length < 12) { setResetError('临时密码至少 12 位'); return; }
    acting.current = true; setPendingAction({ id: resetting.id, kind: 'reset' }); setResetError(undefined);
    try { await request(`/v1/users/${resetting.id}/reset-password`, { method: 'POST', body: JSON.stringify({ temporaryPassword: password }) }); await refresh(); message.success('密码已重置'); setResetting(undefined); setPassword(''); }
    catch (error) { setResetError((error as Error).message); }
    finally { acting.current = false; setPendingAction(undefined); }
  };
  const options = [...new Set([...roles, ...(editing ? [editing.role] : [])])];
  return <div className="page-stack admin-workspace">
    <PageHeader title="用户与角色" description="管理平台账号、角色权限与数据访问范围。" actions={<Button type="primary" icon={<PlusOutlined />} onClick={() => show()}>添加用户</Button>} />
    <Card variant="borderless" className="filter-bar admin-panel admin-filter-bar"><div className="admin-filter-content"><Input.Search aria-label="搜索用户" placeholder="搜索姓名或邮箱" allowClear onSearch={value => { setSearch(value); setPage(1); }} /></div></Card>
    <DataState loading={users.isLoading} error={users.error?.message} empty={!users.data?.items.length} emptyTitle="暂无用户">
      <Card className="table-panel admin-panel admin-table-panel"><Table<ManagedUser> rowKey="id" dataSource={users.data?.items} scroll={{ x: 1100 }} pagination={{ current: page, total: users.data?.total, pageSize: 20, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '用户', render: (_, user) => <><strong>{user.displayName}</strong><small className="table-secondary">{user.email}</small></> },
        { title: '角色', dataIndex: 'role', render: (value: UserRole) => <Tag color={globalRoles.includes(value) ? 'red' : 'blue'}>{roleNames[value]}</Tag> },
        { title: '权限说明', dataIndex: 'role', width: 290, render: (value: UserRole) => roleDescriptions[value] },
        { title: '状态', dataIndex: 'isActive', render: value => <Tag color={value ? 'success' : 'default'}>{value ? '已启用' : '已停用'}</Tag> },
        { title: '密码状态', dataIndex: 'mustChangePassword', render: value => value ? '首次登录需修改' : '已设置' },
        { title: '最近登录', dataIndex: 'lastLoginAt', render: value => value ? date(value) : '尚未登录' },
        { title: '操作', fixed: 'right', width: 210, render: (_, user) => superAdmin || roles.includes(user.role) || user.id === auth?.user?.id ? <Space wrap><Button size="small" disabled={Boolean(pendingAction)} onClick={() => show(user)}>编辑</Button><Button size="small" disabled={Boolean(pendingAction)} onClick={() => showReset(user)}>重置密码</Button>{user.id !== auth?.user?.id && <Button aria-label={`删除用户 ${user.displayName}`} danger size="small" loading={pendingAction?.id === user.id && pendingAction.kind === 'delete'} disabled={Boolean(pendingAction && pendingAction.id !== user.id)} onClick={() => void remove(user)}>删除</Button>}</Space> : <span>无管理权限</span> },
      ]} /></Card>
    </DataState>
    <Modal open={Boolean(resetting)} title={`重置“${resetting?.displayName ?? ''}”的密码`} okText="重置密码" cancelText="取消"
      confirmLoading={pendingAction?.kind === 'reset'} okButtonProps={{ disabled: password.length < 12 || Boolean(pendingAction) }}
      cancelButtonProps={{ disabled: Boolean(pendingAction) }} closable={!pendingAction} maskClosable={!pendingAction} keyboard={!pendingAction}
      onCancel={() => { if (!acting.current) setResetting(undefined); }} onOk={() => void reset()}>
      <p>输入至少 12 位临时密码。该用户重新登录后必须修改密码。</p>
      {resetError && <Alert showIcon type="error" message={resetError} />}
      <Input.Password aria-label="新临时密码" autoComplete="new-password" value={password} disabled={Boolean(pendingAction)} onChange={event => setPassword(event.target.value)} onPressEnter={() => void reset()} />
    </Modal>
    <Modal open={open} title={editing ? '编辑用户' : '创建用户'} okText={editing ? '保存修改' : '创建用户'} cancelText="取消" confirmLoading={save.isPending} onCancel={() => setOpen(false)} onOk={() => { void form.validateFields().then(values => save.mutate(values)).catch(() => undefined); }}>
      <Form form={form} layout="vertical">
        <Form.Item name="email" label="登录邮箱" rules={[{ required: true, type: 'email', message: '请输入有效邮箱' }]}><Input /></Form.Item>
        <Form.Item name="displayName" label="用户姓名" rules={[{ required: true, min: 2, message: '姓名至少 2 个字符' }]}><Input /></Form.Item>
        <Form.Item name="role" label="用户角色" rules={[{ required: true, message: '请选择角色' }]} extra={role ? roleDescriptions[role] : undefined}><Select disabled={own} options={options.map(value => ({ value, label: roleNames[value] }))} onChange={() => form.setFieldValue('projectIds', [])} /></Form.Item>
        {role && !globalRoles.includes(role) && global && <Form.Item name="tenantId" label="所属数据空间" rules={[{ required: true, message: '请选择数据空间' }]}><Select disabled={own} loading={scopes.isLoading} options={scopes.data?.tenants.map(item => ({ value: item.id, label: item.name }))} onChange={() => form.setFieldValue('projectIds', [])} /></Form.Item>}
        {role && !globalRoles.includes(role) && !['TENANT_ADMIN', 'BASIC'].includes(role) && <Form.Item name="projectIds" label="授权数据项目" rules={[{ required: true, message: '请选择至少一个数据项目' }]}><Select mode="multiple" disabled={own} options={scopes.data?.projects.filter(item => item.tenantId === tenantId).map(item => ({ value: item.id, label: item.name }))} /></Form.Item>}
        {editing ? <Form.Item name="isActive" label="账号状态"><Select disabled={own} options={[{ value: true, label: '启用' }, { value: false, label: '停用' }]} /></Form.Item> : <Form.Item name="temporaryPassword" label="临时密码" rules={[{ required: true, min: 12, message: '临时密码至少 12 位' }]}><Input.Password autoComplete="new-password" /></Form.Item>}
      </Form>
    </Modal>
  </div>;
}
