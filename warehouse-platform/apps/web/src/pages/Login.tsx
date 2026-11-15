import { LockOutlined, UserOutlined, DatabaseOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { defaultWarehousePath } from '../auth/permissions';

export function Login() {
  const auth = useAuth(); const navigate = useNavigate(); const location = useLocation(); const [error, setError] = useState<string>(); const [busy, setBusy] = useState(false);
  const submit = async (values: { email: string; password: string }) => { setBusy(true); setError(undefined); try { const user = await auth.login(values.email, values.password); navigate(user.mustChangePassword ? '/change-password' : ((location.state as { from?: string } | null)?.from ?? defaultWarehousePath(user.role)), { replace: true }); } catch (reason) { setError(reason instanceof Error ? reason.message : '登录失败，请稍后重试'); } finally { setBusy(false); } };
  return <main className="login-shell"><section className="login-brand"><div className="login-mark"><DatabaseOutlined /></div><Typography.Title>内部数仓平台</Typography.Title></section><Card className="login-card" variant="borderless"><Typography.Title level={2}>登录</Typography.Title>{error && <Alert type="error" showIcon message="登录失败" description={error} />}<Form layout="vertical" onFinish={(values) => void submit(values)}><Form.Item label="账号或邮箱" name="email" rules={[{ required: true, whitespace: true, message: '请输入账号或邮箱' }]}><Input size="large" prefix={<UserOutlined />} autoComplete="username" /></Form.Item><Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}><Input.Password size="large" prefix={<LockOutlined />} autoComplete="current-password" /></Form.Item><Button size="large" type="primary" htmlType="submit" block loading={busy}>进入数仓平台</Button></Form></Card></main>;
}
