import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/auth': 'http://localhost:8050',
      '/data': 'http://localhost:8050',
      '/premium': 'http://localhost:8050',
      '/admin': 'http://localhost:8050',
    }
  }
})
