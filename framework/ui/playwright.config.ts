import { defineConfig, devices } from '@playwright/test'

// Tests run against the running framework UI (default localhost:8091).
// Override via TEST_UI_URL env var.
const UI = process.env.TEST_UI_URL || 'http://localhost:8091'

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,           // serial: tests share registry state
  retries: 0,
  workers: 1,
  timeout: 60_000,
  reporter: [['list']],
  use: {
    baseURL: UI,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    headless: true,
    viewport: { width: 1400, height: 900 },
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
