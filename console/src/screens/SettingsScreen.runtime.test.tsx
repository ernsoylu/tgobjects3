/**
 * Runtime Settings panel behaviour tests (task 10.2; Requirement 4).
 *
 * Complements the a11y assertions in {@link SettingsScreen.test} and the
 * formal range-validation property test (task 10.3) by covering the two
 * clauses that are easy to regress:
 *
 *  - **4.4 (unsaved-changes affordance)** — editing a field surfaces an
 *    explicit "unsaved changes" status region and enables both Save (submit)
 *    and Reset (discard); Reset restores the last-displayed values and clears
 *    the indicator without calling the API.
 *  - **4.7 (409 staging below reserved)** — a 409 is NOT presented as applied:
 *    no success notice, the form stays dirty, and the error names the minimum
 *    permitted value parsed from the backend `detail`.
 *  - **4.8 (success)** — a successful save clears the unsaved indicator once the
 *    applied values are re-displayed.
 *
 * The `../api/settings` hooks are mocked so the panel renders a known snapshot
 * and a controllable mutation, matching the console suite's convention.
 *
 * Validates: Requirements 4.4, 4.7, 4.8.
 */
import { AxiosError, AxiosHeaders } from "axios";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsValues } from "../api/settings";
import { SettingsScreen } from "./SettingsScreen";

const SETTINGS: SettingsValues = {
  staging_max_size: 1_024,
  cache_max_size: 2_048,
  cache_half_life: 3_600,
  read_ahead: 8,
  upload_concurrency: 4,
};

let settingsResult: {
  data?: SettingsValues;
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => void;
};

// A controllable mutation: `mutate(body, { onSuccess, onError })` dispatches to
// whichever callback the current test armed, mirroring React Query's shape.
type MutateCallbacks = {
  onSuccess?: () => void;
  onError?: (error: unknown) => void;
};
let mutateImpl: (body: SettingsValues, cbs: MutateCallbacks) => void;
const mutateMock = vi.fn((body: SettingsValues, cbs: MutateCallbacks) => mutateImpl(body, cbs));

vi.mock("../api/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/settings")>();
  return {
    ...actual,
    useSettings: () => settingsResult,
    useUpdateSettings: () => ({ mutate: mutateMock, isPending: false }),
  };
});

function loadedState(): typeof settingsResult {
  return { data: SETTINGS, isPending: false, isError: false, refetch: vi.fn() };
}

/** Build an axios 409 error carrying the backend's staging-below-reserved detail. */
function conflict409(reserved: number): AxiosError {
  const error = new AxiosError("Request failed with status code 409");
  error.response = {
    status: 409,
    statusText: "Conflict",
    data: {
      detail:
        `staging_max_size cannot be reduced below the currently reserved ` +
        `${reserved} bytes; minimum permitted value is ${reserved}`,
    },
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

beforeEach(() => {
  mutateMock.mockClear();
  mutateImpl = () => undefined;
  settingsResult = loadedState();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("Runtime Settings panel behaviour", () => {
  it("shows an unsaved-changes indicator and enables submit + discard when edited (Req 4.4)", () => {
    render(<SettingsScreen />);

    // Clean form: no unsaved indicator, Save and Reset disabled.
    expect(screen.queryByTestId("unsaved-indicator")).not.toBeVisible();
    expect(screen.getByRole("button", { name: /save changes/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^reset$/i })).toBeDisabled();

    // Edit a field.
    const cacheSize = screen.getByLabelText<HTMLInputElement>("Cache size (bytes)");
    fireEvent.change(cacheSize, { target: { value: "4096" } });

    // Unsaved affordance appears with both choices enabled.
    const indicator = screen.getByTestId("unsaved-indicator");
    expect(indicator).toBeVisible();
    expect(indicator).toHaveTextContent(/unsaved changes/i);
    expect(screen.getByRole("button", { name: /save changes/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^reset$/i })).toBeEnabled();
  });

  it("discards edits via Reset without calling the API (Req 4.4)", () => {
    render(<SettingsScreen />);

    const cacheSize = screen.getByLabelText<HTMLInputElement>("Cache size (bytes)");
    fireEvent.change(cacheSize, { target: { value: "4096" } });
    expect(cacheSize.value).toBe("4096");

    fireEvent.click(screen.getByRole("button", { name: /^reset$/i }));

    // Last-displayed value restored, indicator gone, no mutation issued.
    expect(cacheSize.value).toBe("2048");
    expect(screen.queryByTestId("unsaved-indicator")).not.toBeVisible();
    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("on 409 shows the minimum permitted value and keeps the form dirty (Req 4.7)", () => {
    mutateImpl = (_body, cbs) => cbs.onError?.(conflict409(5000));
    render(<SettingsScreen />);

    // Reduce staging below the reserved amount, then submit.
    const staging = screen.getByLabelText<HTMLInputElement>("Staging size (bytes)");
    fireEvent.change(staging, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    // The error names the minimum permitted value from the detail.
    expect(screen.getByText(/smallest value you can set is 5000/i)).toBeInTheDocument();
    // Not presented as applied: no success text, form stays dirty.
    expect(screen.queryByText(/now in effect/i)).not.toBeInTheDocument();
    expect(screen.getByTestId("unsaved-indicator")).toBeVisible();
    expect(screen.getByRole("button", { name: /save changes/i })).toBeEnabled();
  });

  it("clears the unsaved indicator once a successful save re-displays applied values (Req 4.8)", () => {
    mutateImpl = (body, cbs) => {
      // Simulate the query invalidation re-seeding the form with applied values.
      settingsResult = { ...loadedState(), data: body };
      cbs.onSuccess?.();
    };
    const { rerender } = render(<SettingsScreen />);

    const cacheSize = screen.getByLabelText<HTMLInputElement>("Cache size (bytes)");
    fireEvent.change(cacheSize, { target: { value: "4096" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    // Re-render so the effect re-seeds the form from the new applied snapshot.
    rerender(<SettingsScreen />);

    expect(screen.getByText(/now in effect/i)).toBeInTheDocument();
    expect(screen.queryByTestId("unsaved-indicator")).not.toBeVisible();
    expect(cacheSize.value).toBe("4096");
  });
});
