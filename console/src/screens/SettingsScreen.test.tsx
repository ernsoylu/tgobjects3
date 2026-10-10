/**
 * Accessibility assertions for the settings screen (task 14.2; Requirement 10).
 *
 * {@link SettingsScreen} is the Radix-`Tabs` host (task 10.1) with four panels;
 * the Runtime Settings panel owns the live form, the other three are
 * placeholder shells here. These tests cover the DOM-observable parts of
 * Requirement 10:
 *
 *  - **10.3 (programmatic labels)** — the tab list is labelled, every tab is a
 *    role `tab` with an accessible name, and each runtime-settings input is
 *    reachable by its `<label htmlFor>` association.
 *  - **10.2 (keyboard operability)** — Radix `Tabs` exposes the roving
 *    `tablist`/`tab`/`tabpanel` structure that makes the tabs arrow-key
 *    operable; the active panel is associated with its tab via `aria-labelledby`.
 *  - **10.6 (status/error announced without moving focus)** — the per-field
 *    validation message and the load-failure region both use `role="alert"` so
 *    a change is announced in place.
 *
 * The data layer (`../api/settings`) is mocked so the panel renders a known
 * settings snapshot (or a load error) without a backend or React Query network
 * call. Interactions use `fireEvent`, matching the rest of the console suite.
 *
 * Validates: Requirements 10.1, 10.3, 10.5, 10.6.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsValues } from "../api/settings";
import { SettingsScreen } from "./SettingsScreen";

// A fetched settings snapshot in the valid range for every field.
const SETTINGS: SettingsValues = {
  staging_max_size: 1_024,
  cache_max_size: 2_048,
  cache_half_life: 3_600,
  read_ahead: 8,
  upload_concurrency: 4,
};

// Mutable query/mutation state the mocked hooks return; reset per test.
let settingsResult: {
  data?: SettingsValues;
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => void;
};
const mutateMock = vi.fn();

vi.mock("../api/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/settings")>();
  return {
    ...actual,
    useSettings: () => settingsResult,
    useUpdateSettings: () => ({ mutate: mutateMock, isPending: false }),
  };
});

function loadedState(): typeof settingsResult {
  return {
    data: SETTINGS,
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  };
}

beforeEach(() => {
  mutateMock.mockReset();
  settingsResult = loadedState();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SettingsScreen accessibility", () => {
  it("labels the tab list and every tab (Req 10.3)", () => {
    render(<SettingsScreen />);

    // The tablist carries an accessible name.
    const tablist = screen.getByRole("tablist", { name: "Settings views" });
    expect(tablist).toBeInTheDocument();

    // Each of the four views is a `tab` reachable by its accessible name.
    for (const name of [
      "Runtime Settings",
      "Access Keys",
      "Configuration Visibility",
      "System Health",
    ]) {
      expect(within(tablist).getByRole("tab", { name })).toBeInTheDocument();
    }
  });

  it("exposes an operable tabpanel associated with its tab (Req 10.2/10.5)", () => {
    render(<SettingsScreen />);

    const runtimeTab = screen.getByRole("tab", { name: "Runtime Settings" });
    // The default tab is selected and its panel is exposed to AT.
    expect(runtimeTab).toHaveAttribute("aria-selected", "true");

    const panel = screen.getByRole("tabpanel");
    // Radix wires the active panel back to its controlling tab, so a screen
    // reader announces which tab the panel belongs to.
    expect(panel).toHaveAttribute("aria-labelledby", runtimeTab.id);
  });

  it("associates a programmatic label with every runtime-settings input (Req 10.3)", () => {
    render(<SettingsScreen />);

    for (const label of [
      "Staging size (bytes)",
      "Cache size (bytes)",
      "Cache half-life (seconds)",
      "Read-ahead count",
      "Upload concurrency",
    ]) {
      const input = screen.getByLabelText(label);
      expect(input).toBeInTheDocument();
      expect(input.tagName).toBe("INPUT");
    }

    // The action controls are reachable by role + name as well.
    expect(screen.getByRole("button", { name: /save changes/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^reset$/i })).toBeInTheDocument();
  });

  it("announces a field validation error via role=alert in place (Req 10.6)", () => {
    render(<SettingsScreen />);

    const readAhead = screen.getByLabelText<HTMLInputElement>("Read-ahead count");
    // 33 is out of the backend-mirrored 0–32 range, so an inline error appears.
    fireEvent.change(readAhead, { target: { value: "33" } });

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/at most 32/i);
    // The field points at the error region for assistive tech.
    expect(readAhead).toHaveAttribute("aria-describedby", alert.id);
    expect(readAhead).toHaveAttribute("aria-invalid", "true");
  });

  it("announces a load failure via role=alert (Req 10.6)", () => {
    settingsResult = {
      isPending: false,
      isError: true,
      error: new Error("gateway unreachable"),
      refetch: vi.fn(),
    };
    render(<SettingsScreen />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/could not load settings/i);
    // The retry control inside the alert is reachable by role + name.
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});
