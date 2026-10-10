/**
 * Routing-guard behaviour for the setup-required gate (task 5.3).
 *
 * This is the focused Vitest + Testing Library half of **Property 9: Setup-required
 * state routes into the wizard** (design: Correctness Property 9; Error Handling —
 * "Setup-required routing"; the Console SPA component diagram's
 * `RequireAuth / RequireSetup`). The full Playwright UI exercise of the same
 * property is task 15.1; this test stays self-contained with no live backend by
 * mocking `useAuth` and the `useSetupStatus` query and rendering the real
 * `RequireAuth`/`RequireSetup` guards inside a `MemoryRouter`.
 *
 * The three branches covered:
 *  - `setup_required = true` (authenticated): navigation to a guarded admin route
 *    lands on the Setup Wizard (`/setup`), never the admin screen.
 *  - `setup_required = false` (authenticated): navigation reaches the admin screen.
 *  - probe in flight (loading): neither the admin screen nor a wizard flash is
 *    shown while the setup gate is still undecided.
 *
 * Validates: Requirements 3.1, 3.12, 8.6, 12.5.
 */
import { render, screen } from "@testing-library/react";
import type { UseQueryResult } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SetupStatus } from "../api/setup";
import type { AuthState } from "./context";
import { RequireAuth, RequireSetup } from "./guards";

// Mock the auth context so the guards see a scripted session status without an
// AuthProvider or any credentials.
const authState = vi.hoisted(() => ({ current: null as AuthState | null }));
vi.mock("./context", () => ({
  useAuth: () => {
    if (authState.current === null) {
      throw new Error("test must set the auth state before rendering");
    }
    return { state: authState.current, signIn: vi.fn(), signOut: vi.fn() };
  },
}));

// Mock the setup-status query so the guard sees a scripted probe result without
// hitting `adminClient` / the network.
const setupResult = vi.hoisted(() => ({
  current: null as Partial<UseQueryResult<SetupStatus, Error>> | null,
}));
vi.mock("../api/setup", () => ({
  useSetupStatus: () => {
    if (setupResult.current === null) {
      throw new Error("test must set the setup-status result before rendering");
    }
    return setupResult.current;
  },
}));

/** Marker screens so a test can assert which route the guards resolved to. */
const ADMIN_TEXT = "admin screen";
const WIZARD_TEXT = "setup wizard";
const LOGIN_TEXT = "login screen";

function renderGuardedTree(initialPath: string): void {
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/login" element={<p>{LOGIN_TEXT}</p>} />
        <Route path="/setup" element={<p>{WIZARD_TEXT}</p>} />
        <Route
          path="/buckets"
          element={
            <RequireAuth>
              <RequireSetup>
                <p>{ADMIN_TEXT}</p>
              </RequireSetup>
            </RequireAuth>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

/** A resolved setup-status query reporting the given flag. */
function settledStatus(setupRequired: boolean): Partial<UseQueryResult<SetupStatus, Error>> {
  return { data: { setup_required: setupRequired }, isLoading: false };
}

beforeEach(() => {
  authState.current = { status: "authenticated", endedReason: null };
  setupResult.current = null;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("RequireSetup (Property 9: setup-required routing)", () => {
  it("routes an authenticated operator to the wizard while setup_required is true", () => {
    setupResult.current = settledStatus(true);

    renderGuardedTree("/buckets");

    expect(screen.getByText(WIZARD_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(ADMIN_TEXT)).not.toBeInTheDocument();
  });

  it("admits the admin screens once setup_required is false", () => {
    setupResult.current = settledStatus(false);

    renderGuardedTree("/buckets");

    expect(screen.getByText(ADMIN_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(WIZARD_TEXT)).not.toBeInTheDocument();
  });

  it("shows neither the admin screen nor a wizard flash while the probe is in flight", () => {
    setupResult.current = { data: undefined, isLoading: true };

    renderGuardedTree("/buckets");

    expect(screen.queryByText(ADMIN_TEXT)).not.toBeInTheDocument();
    // The in-flight state renders nothing in place, so navigation has not yet
    // redirected to the wizard either — no flash before the gate is known.
    expect(screen.queryByText(WIZARD_TEXT)).not.toBeInTheDocument();
  });

  it("treats a failed probe conservatively and routes to the wizard", () => {
    // No data and not loading models a settled-but-failed query; the guard
    // defaults to "setup still required" to keep the operator out of a
    // half-configured dashboard.
    setupResult.current = {
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("probe failed"),
    };

    renderGuardedTree("/buckets");

    expect(screen.getByText(WIZARD_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(ADMIN_TEXT)).not.toBeInTheDocument();
  });
});

describe("RequireAuth gating the setup-required flow", () => {
  it("diverts to login when the session is not authenticated, before any setup probe", () => {
    authState.current = { status: "unauthenticated", endedReason: null };
    // Even if the probe would report setup complete, an unauthenticated request
    // must land on login — RequireAuth runs outside RequireSetup.
    setupResult.current = settledStatus(false);

    renderGuardedTree("/buckets");

    expect(screen.getByText(LOGIN_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(ADMIN_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByText(WIZARD_TEXT)).not.toBeInTheDocument();
  });
});
