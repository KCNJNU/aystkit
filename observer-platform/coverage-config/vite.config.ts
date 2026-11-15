import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], base: '/coverage-assets/', worker: { format: 'es', rollupOptions: { output: { entryFileNames: 'assets/camera-optimizer-[hash].js', chunkFileNames: 'assets/camera-worker-[hash].js' } } }, build: { outDir: '../cloud/frontend/coverage', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 700, rollupOptions: { output: { manualChunks: { three: ['three', 'three/addons/controls/OrbitControls.js'] } } } } });
