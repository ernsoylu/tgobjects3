/**
 * System Health panel rendering (admin-dashboard task 10.8; Requirements
 * 7.2–7.5, 7.7).
 *
 * Light component coverage of how the panel renders each derived
 * {@link HealthSnapshot}, complementing the exhaustive pure-derivation matrix in
 * `../../api/health.test.ts`. Asserts the labelled, not-hue-only rendering:
 *
 *  - a ready snapshot → "Ready" with both PostgreSQL and Telegram "Healthy"
 *    (Req 7.2);
 *  - a not-ready snapshot naming PostgreSQL → "Not ready" + PostgreSQL
 *    "Unavailable", Telegram "Healthy" (Req 7.3);
 *  - a not-ready snapshot naming Telegram → "Not ready" + Telegram
 *    "Unavailable", PostgreSQL "Healthy" (Req 7.4);
 *  - an unknown snapshot → "Unknown" with both dependencies shown "Unknown"
 *    (neither asserted) (Req 7.5);
 *  - the Setup_Required gate → "Readiness pending" with no dependency table
 *    (Req 7.7).
 *
 * The data hooks (`../../api/health`, `../../api/setup`) are mocked so the panel
 * renders a known snapshot without a backend or React Query network call,
 * matching the sibling panel suites.
 *
 * Validates: Requirements 7.2, 7.3, 7.4, 7.5.
 */
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HealthSnapshot } from "../../api/health";
import type { SetupStatus } from "../../api/setup";
import { SystemHealthPanel } from "./SystemHealthPanel";

// Mutable hook state the mocked hooks return; reset per test.
let readinessResult: { data?: HealthSnapshot; isPending: boolean };
let setupResult: { data?: SetupStatus };

vi.mock("../../api/health", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/health")>();
  return {
    ...actual,
    useReadiness: () => readinessResult,
    useMetrics: () => ({ mutate: vi.fn(), isPending: false, isError: false, data: undefined }),
  };
});

vi.mock("../../api/setup", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/setup")>();
  return {
    ...actual,
    useSetupStatus: () => setupResult,
  };
});

function loadedReadiness(data: HealthSnapshot): typeof readinessResult {
  return { data, isPending: false };
}

/** Read the status cell text of a named dependency row ("PostgreSQL" / "Telegram session"). */
function dependencyStatus(rowName: string | RegExp): string {
  const row = screen.getByRole("row", { name: new RegExp(rowName, "i") });
  const cells = within(row).getAllByRole("cell");
  const statusCell = cells.at(-1);
  return statusCell?.textContent?.trim() ?? "";
}

beforeEach(() => {
  // Default: not in setup-required state, so the readiness + deps regions render.
  setupResult = { data: { setup_required: false } };
  readinessResult = { isPending: true };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SystemHealthPanel", () => {
  it("renders ready with both dependencies healthy on a 200 snapshot (Req 7.2)", () => {
    readinessResult = loadedReadiness({
      readiness: "ready",
      dependencies: { postgres: true, telegram: true },
    });
    render(<SystemHealthPanel />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/ready/i);

    expect(dependencyStatus("PostgreSQL")).toMatch(/healthy/i);
    expect(dependencyStatus("Telegram session")).toMatch(/healthy/i);
  });

  it("renders not ready + PostgreSQL unavailable on a postgres 503 (Req 7.3)", () => {
    readinessResult = loadedReadiness({
      readiness: "not_ready",
      dependencies: { postgres: false, telegram: true },
    });
    render(<SystemHealthPanel />);

    expect(screen.getByRole("status")).toHaveTextContent(/not ready/i);
    expect(dependencyStatus("PostgreSQL")).toMatch(/unavailable/i);
    expect(dependencyStatus("Telegram session")).toMatch(/healthy/i);
  });

  it("renders not ready + Telegram unavailable on a telegram 503 (Req 7.4)", () => {
    readinessResult = loadedReadiness({
      readiness: "not_ready",
      dependencies: { postgres: true, telegram: false },
    });
    render(<SystemHealthPanel />);

    expect(screen.getByRole("status")).toHaveTextContent(/not ready/i);
    expect(dependencyStatus("Telegram session")).toMatch(/unavailable/i);
    expect(dependencyStatus("PostgreSQL")).toMatch(/healthy/i);
  });

  it("renders readiness unknown with dependencies unasserted on a failed poll (Req 7.5)", () => {
    readinessResult = loadedReadiness({
      readiness: "unknown",
      dependencies: { postgres: false, telegram: false },
    });
    render(<SystemHealthPanel />);

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/unknown/i);
    expect(status).toHaveTextContent(/could not be reached/i);

    // Neither dependency is asserted either way: both show "Unknown", not
    // a misleading "Unavailable".
    expect(dependencyStatus("PostgreSQL")).toMatch(/unknown/i);
    expect(dependencyStatus("Telegram session")).toMatch(/unknown/i);
  });

  it("shows readiness pending and hides the dependency table during setup (Req 7.7)", () => {
    setupResult = { data: { setup_required: true } };
    readinessResult = loadedReadiness({
      readiness: "not_ready",
      dependencies: { postgres: true, telegram: false },
    });
    render(<SystemHealthPanel />);

    expect(screen.getByRole("status")).toHaveTextContent(/readiness pending/i);
    // While setup is required the dependency table is not meaningful and is hidden.
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
