import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * Vitest configuration for the tgobjects3 admin console unit/component tests.
 *
 * Component tests render React 18 trees with Testing Library, so a DOM is
 * required: the `jsdom` environment supplies `window`/`document`. The setup
 * file registers `@testing-library/jest-dom` matchers and auto-cleanup. Only
 * co-located `src/**` test files are picked up here; the Playwright e2e suite
 * under `tests/e2e` has its own runner (`npm run test:e2e`) and is excluded.
 *
 * `fileParallelism` is disabled so test files run one at a time rather than
 * interleaved in a shared pool. Several suites render Radix portals (dialogs,
 * toasts, alert regions) into `document.body` outside the React root, and the
 * property suites (`*.property.test.tsx`) mount hundreds of trees per run;
 * running files concurrently in one jsdom lets a portal/alert from one file
 * leak into another's `getByRole` queries, which made unrelated suites flake.
 * Serial file execution keeps each file's DOM isolated (Testing Library's
 * per-test `cleanup` already handles within-file isolation).
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    fileParallelism: false,
  },
});
