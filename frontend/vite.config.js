import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The third argument is an empty prefix, so the dev server's own settings (API_TARGET) can come
// from a .env file too rather than only from the shell.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  build: {
    // Hidden: the maps are emitted for the error tracker to consume at deploy time, and no
    // `sourceMappingURL` comment is written, so they are not fetched by a visitor's browser and
    // the application source is not published alongside the bundle.
    sourcemap: 'hidden',
    // The budget in package.json is checked against these files by scripts/check-bundle-size.mjs.
    rollupOptions: {
      output: {
        // The error tracker is loaded only when a DSN is configured, so it has to be its own
        // chunk -- bundled into the entry it would be downloaded by everyone regardless.
        manualChunks(id) {
          if (id.includes('node_modules/@sentry')) return 'observability'
          if (id.includes('node_modules/react') || id.includes('node_modules/scheduler')) {
            return 'react'
          }
          return undefined
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        // Overridable so a second backend can be run alongside one that is already on 8000.
        target: loadEnv(mode, process.cwd(), '').API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
}))
