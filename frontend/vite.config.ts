import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置
// 关键点：SSE 流式接口必须关闭代理缓冲，否则答案无法逐字显示。
export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,

    watch: {
      // 关键：忽略编辑器/工具产生的临时目录。
      //
      // 现象：代码编辑工具写入文件时会创建 `.xxx.ts.<pid>.<uuid>.tmpdir/`
      // 目录并在其中写临时文件，写完删除。而 Vite 的 FSWatcher 会尝试
      // 监听这个瞬间存在的目录，遇到文件锁后抛出**未捕获异常**，
      // 直接导致 dev server 进程退出（EBUSY: resource busy or locked）。
      //
      // 该崩溃与业务代码无关，但会反复中断开发。这里显式忽略。
      ignored: [
        '**/*.tmpdir/**',
        '**/.git/**',
        '**/node_modules/**',
        '**/dist/**',
      ],
    },

    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
        // SSE 需要禁用缓冲：Vite 的 http-proxy 默认会缓冲响应体，
        // 导致流式答案一次性到达而非逐字输出。
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            // 对流式响应关闭 Nagle 算法与缓冲
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['x-accel-buffering'] = 'no'
              proxyRes.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 手动分包：Element Plus 与 highlight.js 体积较大，单独打包便于缓存
        manualChunks: {
          'element-plus': ['element-plus', '@element-plus/icons-vue'],
          highlight: ['highlight.js'],
          markdown: ['markdown-it', 'dompurify'],
        },
      },
    },
  },
})
