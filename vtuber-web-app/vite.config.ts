import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, '..'), '')
  return {
    envDir: '..',
    envPrefix: ['VITE_', 'BACKEND_'],
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
        '@framework': path.resolve(__dirname, './src/live2d/framework'),
        '@components': path.resolve(__dirname, './src/components'),
        '@store': path.resolve(__dirname, './src/store'),
        '@utils': path.resolve(__dirname, './src/utils'),
        '@types': path.resolve(__dirname, './src/types'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: env.FRONTEND_PORT ? parseInt(env.FRONTEND_PORT, 10) : 5173,
      // HTTPS 配置（麥克風需要）
      // 開發時使用 HTTP 也可以，因為 localhost 視為安全環境
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
      rollupOptions: {
        output: {
          manualChunks: {
            'live2d-core': ['./public/Core/live2dcubismcore.js'],
          },
        },
      },
    },
  }
})
