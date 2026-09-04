import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 临时验证配置：将 /api 代理到本地当前后端 (:9101)，用于真实浏览器端到端联调。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:9101', changeOrigin: true, ws: false },
    },
  },
})
