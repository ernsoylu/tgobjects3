/**
 * Global Vitest setup for the console component/unit suite.
 *
 * Registers the `@testing-library/jest-dom` custom matchers (`toBeInTheDocument`,
 * `toHaveAttribute`, …) and runs Testing Library's `cleanup` after every test so
 * rendered trees never leak between cases. Imported once via `setupFiles` in
 * `vitest.config.ts`.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
