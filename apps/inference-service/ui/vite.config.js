import { defineConfig } from 'vite';

export default defineConfig({
  base: '/dashboard/',
  server: {
    proxy: Object.fromEntries(['/v1', '/internal', '/health'].map(path => [path, {
      target: process.env.INFERENCE_API_TARGET || 'http://localhost:8000',
      changeOrigin: true,
    }])),
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
