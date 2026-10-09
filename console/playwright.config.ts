import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the tgobjects3 admin console end-to-end suite
 * (task 45.1; Requirements 2.1–2.7, 19.1–19.7).
 *
 * The console is a pure client-side SPA that talks to the gateway Admin API
 * (`/admin/*`). These tests exercise the Console's critical paths — screen
 * navigation, the bucket list, the object browser, the jobs retry action, the
 * recovery status view, and the settings read/update flow — WITHOUT a live
 * backend: every `/admin/*` request is intercepted and fulfilled with a
 * deterministic fixture via `page.route` inside each spec. That keeps the suite
 * hermetic and fast, and lets a single run cover both happy paths and error
 * boundaries (e.g. a 409 settings-validation rejection).
 *
 * The `webServer` block builds the console and serves the production bundle
 * with `vite preview`; Playwright waits for the port before running and reuses
 * an already-running server locally. Because the API is mocked at the network
 * layer, the preview server's own `/admin` proxy is never exercised.
 */

/** Port `vite preview` serves the built console on (matches `vite preview`'s default). */
const PREVIEW_PORT = 4173;

/** Base URL the specs navigate against. */
export const BASE_URL = `http://localhost:${PREVIEW_PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  // Fail the build on a stray `test.only` left in a committed spec.
  forbidOnly: !!process.env.CI,
  // Retry only in CI, where flake from cold starts is more likely.
  retries: process.env.CI ? 2 : 0,
  // Keep output deterministic; the suite is small and I/O-light.
  fullyParallel: true,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    // Build then serve the production bundle on a fixed port. `--strictPort`
    // makes a port clash fail loudly instead of silently picking another port.
    command: `npm run build && npx vite preview --port ${PREVIEW_PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
