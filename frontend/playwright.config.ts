import { defineConfig } from '@playwright/test'
import path from 'node:path'

const python = process.env.RAG_PYTHON || 'python'
export default defineConfig({
  testDir: './e2e', timeout: 60_000, workers: 1, fullyParallel: false,
  reporter: [['list'], ['html', { open: 'never' }]],
  globalTeardown: './e2e/teardown.ts',
  use: { channel: process.env.E2E_BROWSER || 'msedge', baseURL: 'http://127.0.0.1:5174', headless: true, trace: 'retain-on-failure',
    screenshot: 'only-on-failure', viewport: { width: 1440, height: 1000 } },
  webServer: [
    { command: `"${python}" -m tests.e2e_server`, cwd: path.resolve('..', 'backend'),
      url: 'http://127.0.0.1:8011/api/health/live', timeout: 120_000, reuseExistingServer: false,
      env: { PYTHONIOENCODING: 'utf-8', PYTHONPATH: path.resolve('..', 'backend') } },
    { command: 'npm run dev -- --port 5174', url: 'http://127.0.0.1:5174', timeout: 60_000,
      reuseExistingServer: false, env: { VITE_API_TARGET: 'http://127.0.0.1:8011' } },
  ],
})
