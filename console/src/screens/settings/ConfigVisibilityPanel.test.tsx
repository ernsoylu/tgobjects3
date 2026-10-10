/**
 * Configuration Visibility panel behaviour (admin-dashboard task 10.6;
 * Requirements 6.2–6.4, 11.1).
 *
 * Component coverage of the panel's observable behaviour:
 *
 *  - non-secret Environment_Settings render read-only, each labelled as
 *    requiring a redeploy or restart, with no editing control (6.2, 6.4);
 *  - secret Environment_Settings render only as "Set" / "Not set" and never
 *    carry their value (6.3, 11.1);
 *  - loading and error (with retry) states render.
 *
 * The data layer (`../../api/config`) is mocked so the panel renders a known
 * query result without a backend or React Query network call. Interactions use
 * `fireEvent`, matching the rest of the console suite.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConfigVisibility } from "../../api/config";
import { ConfigVisibilityPanel } from "./ConfigVisibilityPanel";

const CONFIG: ConfigVisibility = {
  environment: [
    { name: "S3_PORT", value: "9000" },
    { name: "ADMIN_PORT", value: "8080" },
    { name: "CHUNK_RECORDS", value: "512" },
    { name: "CACHE_ENCRYPT", value: "false" },
  ],
  secrets: [
    { name: "TGOS3_MASTER_KEY", is_set: true },
    { name: "DATABASE_URL", is_set: true },
  ],
};

// Mutable query state the mocked hook returns; reset per test.
let configResult: {
  data?: ConfigVisibility;
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => void;
};

vi.mock("../../api/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/config")>();
  return {
    ...actual,
    useConfig: () => configResult,
  };
});

function loadedConfig(data: ConfigVisibility): typeof configResult {
  return { data, isPending: false, isError: false, refetch: vi.fn() };
}

beforeEach(() => {
  configResult = loadedConfig(CONFIG);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ConfigVisibilityPanel", () => {
  it("renders non-secret settings read-only with no editing control (Req 6.2, 6.4)", () => {
    render(<ConfigVisibilityPanel />);

    // Each non-secret setting and its value is shown.
    expect(screen.getByText("S3_PORT")).toBeInTheDocument();
    expect(screen.getByText("9000")).toBeInTheDocument();
    expect(screen.getByText("CHUNK_RECORDS")).toBeInTheDocument();
    expect(screen.getByText("512")).toBeInTheDocument();

    // The redeploy/restart requirement is surfaced per non-secret row.
    expect(screen.getAllByText(/redeploy or restart/i).length).toBeGreaterThanOrEqual(
      CONFIG.environment.length,
    );

    // There is NO editing control: no inputs and no submit/save button (6.4).
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  });

  it("shows secret settings as set/not set and never their value (Req 6.3, 11.1)", () => {
    render(<ConfigVisibilityPanel />);

    expect(screen.getByText("TGOS3_MASTER_KEY")).toBeInTheDocument();
    expect(screen.getByText("DATABASE_URL")).toBeInTheDocument();
    // Both secrets are configured here, so each reads "Set".
    expect(screen.getAllByText(/^set$/i)).toHaveLength(2);
  });

  it('renders "Not set" for an unconfigured secret (Req 6.3)', () => {
    configResult = loadedConfig({
      environment: [],
      secrets: [{ name: "TGOS3_MASTER_KEY", is_set: false }],
    });
    render(<ConfigVisibilityPanel />);

    // Match the status cell exactly ("Not set"), not the panel description prose
    // that also contains the phrase "set or not set".
    expect(screen.getByText(/^not set$/i)).toBeInTheDocument();
  });

  it("renders a loading state while the query is pending", () => {
    configResult = { isPending: true, isError: false, refetch: vi.fn() };
    render(<ConfigVisibilityPanel />);

    expect(screen.getByText(/loading configuration/i)).toBeInTheDocument();
  });

  it("announces a load failure via role=alert with a retry control", () => {
    const refetch = vi.fn();
    configResult = {
      isPending: false,
      isError: true,
      error: new Error("gateway unreachable"),
      refetch,
    };
    render(<ConfigVisibilityPanel />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/could not load configuration/i);

    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});
