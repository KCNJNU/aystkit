import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const workspaceRoot = fileURLToPath(new URL('../..', import.meta.url));

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, workspaceRoot, '');
  const webPort = Number(env.WEB_PORT || '5173');
  const hmrHost = env.WEB_HMR_HOST?.trim();
  return {
    base: env.VITE_APP_BASE_PATH?.trim() || '/',
    envDir: workspaceRoot,
    plugins: [react()],
    server: {
      host: env.WEB_HOST || '0.0.0.0',
      port: webPort,
      strictPort: true,
      hmr: hmrHost ? { host: hmrHost, clientPort: webPort, protocol: 'ws' } : undefined,
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            const path = id.replaceAll('\\', '/');
            if (path.includes('/node_modules/react/') || path.includes('/node_modules/react-dom/') || path.includes('/node_modules/scheduler/')) return 'react-vendor';
            if (path.includes('/node_modules/react-router') || path.includes('/node_modules/@remix-run/')) return 'router-vendor';
            if (path.includes('/node_modules/@tanstack/')) return 'query-vendor';
            if (path.includes('/node_modules/@ant-design/icons')) return 'antd-icons';
            if (path.includes('/node_modules/@ant-design/colors') || path.includes('/node_modules/@ant-design/fast-color')) return 'antd-color';
            if (path.includes('/node_modules/dayjs/')) return 'date-vendor';
            if (path.includes('/node_modules/hash-wasm/') || path.includes('/node_modules/idb/')) return 'upload-vendor';
            const rcPackage = /\/node_modules\/(rc-[^/]+)/.exec(path)?.[1];
            if (rcPackage === 'rc-picker') return 'antd-picker-engine';
            if (rcPackage || path.includes('/node_modules/@ant-design/cssinjs')) return 'antd-engine';
          },
        },
      },
    },
  };
});
