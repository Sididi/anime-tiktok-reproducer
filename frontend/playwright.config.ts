import { defineConfig, devices } from "@playwright/test";

// Override both to run the suite against a second dev stack on other ports
// (e.g. a worktree's Vite on 5174 proxying to its backend on 8001) without
// touching the servers a developer already has running on 5173/8000.
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:5173";
const backendHealthURL =
  process.env.PLAYWRIGHT_BACKEND_HEALTH_URL ?? "http://localhost:8000/health";

/**
 * Playwright configuration for E2E tests
 * @see https://playwright.dev/docs/test-configuration
 */
export default defineConfig({
  testDir: "./e2e",
  /* Run tests in files in parallel */
  fullyParallel: false, // Sequential for our workflow
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 2 : 0,
  /* Opt out of parallel tests on CI. */
  workers: 1,
  /* Reporter to use. See https://playwright.dev/docs/test-reporters */
  reporter: "html",
  /* Shared settings for all the projects below. */
  use: {
    /* Base URL to use in actions like `await page.goto('/')`. */
    baseURL,

    /* Collect trace when retrying the failed test. */
    trace: "on-first-retry",

    /* Take screenshot on failure */
    screenshot: "only-on-failure",

    /* Record video on failure */
    video: "retain-on-failure",
  },

  /* Configure projects for major browsers */
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  /* Run your local dev server before starting the tests */
  webServer: [
    {
      command: "npm run dev",
      url: baseURL,
      reuseExistingServer: !process.env.CI,
      timeout: 120000,
    },
    {
      command: "cd .. && pixi run backend",
      url: backendHealthURL,
      reuseExistingServer: !process.env.CI,
      timeout: 120000,
    },
  ],

  /* Global timeout for each test */
  timeout: 120000, // 2 minutes - downloads can be slow

  /* Expect timeout */
  expect: {
    timeout: 30000,
  },
});
