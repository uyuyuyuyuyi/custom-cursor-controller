import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建为纯静态文件，由 Python 后端 (gui_server.py) 托管
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
  },
  server: {
    port: 5173,
    strictPort: false,
  },
})
