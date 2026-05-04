import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: '0.0.0.0',
  },
  build: {
    outDir: 'dist',
    // Sourcemaps are 6+ MB and auto-fetched by any browser with devtools
    // open — net 6 MB extra transfer per session. Disable for prod; flip
    // back via VITE_SOURCEMAP=1 when debugging a specific deploy.
    sourcemap: process.env.VITE_SOURCEMAP === '1',
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        // Split stable vendor chunks so common libs are cached across
        // releases. App code rebuilds + invalidates often; react/react-
        // dom/react-router rarely change — splitting them out turns
        // repeat visits into ~20 KB instead of ~500 KB downloads.
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})
