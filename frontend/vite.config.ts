import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Dev only: proxy API calls to a locally running backend (production uses nginx, same origin).
const target = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5174, proxy: { '/api': { target, changeOrigin: false } } },
})
