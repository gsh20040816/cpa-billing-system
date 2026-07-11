import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          framework: ['vue', 'vue-router', 'vuetify'],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:18417',
      '/auth': 'http://127.0.0.1:18417',
    },
  },
})
