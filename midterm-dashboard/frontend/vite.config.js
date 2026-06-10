import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // recharts is ~half the bundle and only some pages chart; splitting
        // vendors lets the app shell load without it and caches them
        // independently of app code.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
          icons: ['lucide-react'],
        },
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/auth': 'http://localhost:8050',
      '/data': 'http://localhost:8050',
      '/premium': 'http://localhost:8050',
      '/admin': 'http://localhost:8050',
      '/api': 'http://localhost:8050',
    }
  }
})
