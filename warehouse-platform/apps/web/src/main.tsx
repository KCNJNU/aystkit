import zhCN from 'antd/locale/zh_CN';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import '@ant-design/v5-patch-for-react-19';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider, theme } from 'antd';
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import './styles.css';
import './studio.css';
import './light.css';
import './refinement.css';
import './workbench.css';
import './industrial.css';
import { TechAtmosphere } from './components/TechAtmosphere';
import { UploadQueueProvider } from './upload/UploadQueueContext';
import { AuthProvider } from './auth/AuthContext';
import { AppErrorBoundary } from './components/AppErrorBoundary';

dayjs.locale('zh-cn');
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 20_000, retry: 1, refetchOnWindowFocus: false } } });
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#2c64d7', colorInfo: '#2c64d7', colorSuccess: '#128363', colorWarning: '#a86809', colorError: '#ce394e',
    colorBgBase: '#ffffff', colorBgLayout: '#f2f5fa', colorBgContainer: '#ffffff', colorBgElevated: '#ffffff',
    colorBorder: '#d8e1ee', colorBorderSecondary: '#e8edf5', colorText: '#24334b', colorTextSecondary: '#67768c',
    colorTextPlaceholder: '#71829a', colorTextDisabled: '#8493a7', colorBgContainerDisabled: '#f0f3f8',
    borderRadius: 7, borderRadiusLG: 11, fontSize: 14, controlHeight: 36, motionDurationMid: '0.18s',
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
  },
  components: {
    Layout: { bodyBg: '#f4f6fa', headerBg: 'rgba(255, 255, 255, .88)', siderBg: '#f8fbff' },
    Menu: { itemBg: 'transparent', subMenuItemBg: '#f5f8ff', itemSelectedBg: '#e7f0ff', itemSelectedColor: '#1855bd', itemColor: '#506780' },
    Table: { headerBg: '#f7f9fc', headerColor: '#53627a', rowHoverBg: '#f5f8ff', borderColor: '#e2eaf4', cellPaddingBlock: 15, cellPaddingInline: 18 },
    Card: { colorBgContainer: '#ffffff', paddingLG: 24, headerBg: 'transparent' },
    Drawer: { colorBgElevated: '#ffffff', paddingLG: 20 }, Modal: { contentBg: '#ffffff', headerBg: '#ffffff' },
    Button: { defaultBg: '#ffffff', defaultBorderColor: '#cedced', defaultColor: '#334f70' },
    Input: { activeBg: '#ffffff', hoverBg: '#ffffff' }, Select: { selectorBg: '#ffffff' },
    Pagination: { itemBg: '#ffffff' }, Tabs: { itemColor: '#596f89', itemSelectedColor: '#2467df' },
    Tooltip: { colorBgSpotlight: '#ffffff', colorTextLightSolid: '#19314f' },
  },
}}><AntApp><TechAtmosphere /><AppErrorBoundary><QueryClientProvider client={queryClient}><BrowserRouter basename={import.meta.env.BASE_URL}><AuthProvider><UploadQueueProvider><App /></UploadQueueProvider></AuthProvider></BrowserRouter></QueryClientProvider></AppErrorBoundary></AntApp></ConfigProvider></React.StrictMode>);
