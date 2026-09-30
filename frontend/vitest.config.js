import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Separate from vite.config.js: the dev server's /api proxy has no meaning in a test run, and a
// test that reached a real backend would not be a unit test.
export default defineConfig({
  plugins: [react()],
  // The React plugin transforms the app's own .jsx; the test files are transformed by esbuild,
  // which defaults to the classic runtime and would need React in scope in every one of them.
  esbuild: { jsx: 'automatic' },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
  },
})
