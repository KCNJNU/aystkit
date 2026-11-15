import { defaultWarehousePath, readRoles, globalRoles, writeRoles } from './auth/permissions';
import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { RequireAuth, RequireRole } from './auth/RouteGuards';
import { useAuth } from './auth/AuthContext';
import { AppLayout } from './components/AppLayout';
import { UnsavedChangesProvider } from './components/UnsavedChanges';
import { robotFromSearch, robotPath, selectedRobot } from './services/robot-selection';

const AIAssistant = lazy(() => import('./pages/AIAssistant').then(m => ({ default: m.AIAssistant })));
const SystemStatus = lazy(() => import('./pages/WarehouseStatus').then(m => ({ default: m.WarehouseStatus })));
const Activity = lazy(() => import('./pages/Activity').then((module) => ({ default: module.Activity })));
const TasksAndAudit = lazy(() => import('./pages/TasksAndAudit').then((module) => ({ default: module.TasksAndAudit })));
const ChangePassword = lazy(() => import('./pages/ChangePassword').then((module) => ({ default: module.ChangePassword })));
const Datasets = lazy(() => import('./pages/Datasets').then((module) => ({ default: module.Datasets })));
const DatasetVersionsWorkspace = lazy(() => import('./pages/DatasetVersionsWorkspace').then(module => ({ default: module.DatasetVersionsWorkspace })));
const DatasetWorkspace = lazy(() => import('./pages/DatasetWorkspace').then((module) => ({ default: module.DatasetWorkspace })));
const RecycleBin = lazy(() => import('./pages/RecycleBin').then(m => ({ default: m.RecycleBin })));
const Files = lazy(() => import('./pages/Files').then((module) => ({ default: module.Files })));
const JobDetail = lazy(() => import('./pages/JobDetail').then((module) => ({ default: module.JobDetail })));
const Login = lazy(() => import('./pages/Login').then((module) => ({ default: module.Login })));
const Storage = lazy(() => import('./pages/Storage').then((module) => ({ default: module.Storage })));
const SystemSettings = lazy(() => import('./pages/SystemSettings').then((module) => ({ default: module.SystemSettings })));
const UploadCenter = lazy(() => import('./pages/UploadCenter').then((module) => ({ default: module.UploadCenter })));
const Users = lazy(() => import('./pages/Users').then((module) => ({ default: module.Users })));
const Versions = lazy(() => import('./pages/Versions').then((module) => ({ default: module.Versions })));

function LandingRedirect() {
  const auth = useAuth();
  return <Navigate to={auth.hasRole(...readRoles) ? '/datasets' : '/ai'} replace />;
}

function RetiredGarageRedirect() {
  const auth = useAuth();
  const location = useLocation();
  const robotId = robotFromSearch(location.search) ?? selectedRobot();
  const destination = auth.hasRole(...writeRoles) ? '/upload' : defaultWarehousePath(auth.user?.role);
  return <Navigate to={robotPath(destination, robotId)} replace />;
}

export function App() {
  return <UnsavedChangesProvider><Suspense fallback={<main className="page-stack" aria-busy="true">正在加载页面…</main>}><Routes>
    <Route path="/login" element={<Login />} />
    <Route element={<RequireAuth />}>
      <Route path="/change-password" element={<ChangePassword />} />
      <Route path="/garage-archive/*" element={<RetiredGarageRedirect />} />
      <Route element={<AppLayout />}>
        <Route path="/dashboard" element={<LandingRedirect />} />
        <Route path="/ai" element={<AIAssistant />} />
        <Route path="/api-status" element={<Navigate to="/system-status" replace />} />
        <Route element={<RequireRole roles={readRoles} />}><Route path="/files" element={<Files />} />
        <Route element={<RequireRole roles={['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN', 'PROJECT_ADMIN', 'OPERATOR']} />}><Route path="/recycle-bin" element={<RecycleBin />} /><Route path="/upload" element={<UploadCenter />} /></Route>
        <Route path="/datasets" element={<Datasets />} />
        <Route path="/datasets/:datasetId" element={<DatasetWorkspace />} />
        <Route path="/datasets/:datasetId/history" element={<DatasetVersionsWorkspace />} />
        <Route path="/datasets/:datasetId/versions/:versionId" element={<DatasetVersionsWorkspace />} />
        <Route path="/datasets/:datasetId/compare" element={<DatasetVersionsWorkspace />} />
        <Route path="/dataset-versions" element={<Versions />} />
        <Route path="/jobs" element={<TasksAndAudit />} />
        <Route path="/jobs/:id" element={<JobDetail />} />
        <Route path="/activity" element={<Activity />} /></Route>
        <Route element={<RequireRole roles={['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN']} />}><Route path="/storage" element={<Storage />} /></Route>
        <Route element={<RequireRole roles={globalRoles} />}><Route path="/system-settings" element={<SystemSettings />} /></Route>
        <Route path="/system-status" element={<SystemStatus />} />
        <Route element={<RequireRole roles={['ADMIN', 'SUPER_ADMIN', 'SYSTEM_ADMIN', 'TENANT_ADMIN']} />}>

          <Route path="/users" element={<Users />} />
          <Route path="/audit-logs" element={<TasksAndAudit />} />
        </Route>
        <Route path="*" element={<LandingRedirect />} />
      </Route>
    </Route>
  </Routes></Suspense></UnsavedChangesProvider>;
}
