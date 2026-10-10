/**
 * `AuthProvider` login + session-end behaviour (task 4.3, Testing Library half).
 *
 * Renders a small consumer of `useAuth`, drives `signIn`/`signOut`, and asserts
 * the observable `status`/`endedReason` transitions. The shared `adminClient`
 * module is mocked so the bootstrap `GET /admin/buckets` can be scripted to
 * succeed or 401 without a live backend, and so the test can capture the global
 * unauthorized handler the provider registers and fire it directly.
 *
 * This is the component half of **Property 3**'s surrounding login flow: a
 * successful bootstrap flips to `authenticated` (the CSRF token is cached by the
 * client interceptor); a 401 bootstrap leaves the session `unauthenticated`
 * (rejected login, not an expired session); and a 401 from any *non-bootstrap*
 * request ends the session via `signOut("expired")`.
 *
 * Validates: Requirements 1.5, 11.5, 11.6.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { AxiosError } from "axios";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "./AuthProvider";
import { useAuth } from "./context";

// Mock the shared client module. The provider calls `adminClient.get`,
// `setCredentials`, `clearCredentials`, and registers via
// `setUnauthorizedHandler`; we capture the handler so a test can invoke it.
const getMock = vi.fn();
let registeredHandler: (() => void) | null = null;

vi.mock("../api/client", () => ({
  adminClient: {
    get: (...args: unknown[]) => getMock(...args),
  },
  setCredentials: vi.fn(),
  clearCredentials: vi.fn(),
  setUnauthorizedHandler: (handler: (() => void) | null) => {
    registeredHandler = handler;
    return () => {
      if (registeredHandler === handler) {
        registeredHandler = null;
      }
    };
  },
}));

/** A consumer that surfaces auth state and exposes the actions as buttons. */
function AuthProbe(): JSX.Element {
  const { state, signIn, signOut } = useAuth();
  return (
    <div>
      <output data-testid="status">{state.status}</output>
      <output data-testid="reason">{state.endedReason ?? "none"}</output>
      <button type="button" onClick={() => void signIn("hunter2").catch(() => undefined)}>
        sign-in
      </button>
      <button type="button" onClick={() => signOut("logout")}>
        sign-out
      </button>
    </div>
  );
}

function renderProvider(): void {
  render(
    <AuthProvider>
      <AuthProbe />
    </AuthProvider>,
  );
}

function status(): string {
  return screen.getByTestId("status").textContent ?? "";
}

function reason(): string {
  return screen.getByTestId("reason").textContent ?? "";
}

/** Build an AxiosError whose response status is `code`. */
function axiosErrorWithStatus(code: number): AxiosError {
  const error = new AxiosError(`Request failed with status ${code}`);
  // Minimal response object is enough for the status checks under test.
  error.response = {
    status: code,
    statusText: String(code),
    data: {},
    headers: {},
    config: { headers: {} as never },
  };
  return error;
}

beforeEach(() => {
  getMock.mockReset();
  registeredHandler = null;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AuthProvider", () => {
  it("starts unauthenticated with no ended reason", () => {
    renderProvider();
    expect(status()).toBe("unauthenticated");
    expect(reason()).toBe("none");
  });

  it("flips to authenticated when the bootstrap read succeeds", async () => {
    getMock.mockResolvedValueOnce({ status: 200, headers: {}, data: {} });
    renderProvider();

    await act(async () => {
      screen.getByText("sign-in").click();
    });

    await waitFor(() => expect(status()).toBe("authenticated"));
    expect(getMock).toHaveBeenCalledWith("/buckets");
    expect(reason()).toBe("none");
  });

  it("stays unauthenticated when the bootstrap read is rejected with 401", async () => {
    getMock.mockRejectedValueOnce(axiosErrorWithStatus(401));
    renderProvider();

    await act(async () => {
      screen.getByText("sign-in").click();
    });

    await waitFor(() => expect(status()).toBe("unauthenticated"));
    // A rejected login is not an expired session, so no ended reason surfaces.
    expect(reason()).toBe("none");
  });

  it("ends the session as expired when the global 401 handler fires", async () => {
    getMock.mockResolvedValueOnce({ status: 200, headers: {}, data: {} });
    renderProvider();

    await act(async () => {
      screen.getByText("sign-in").click();
    });
    await waitFor(() => expect(status()).toBe("authenticated"));

    // Simulate a 401 from a later, non-bootstrap authenticated request.
    expect(registeredHandler).not.toBeNull();
    act(() => {
      registeredHandler?.();
    });

    await waitFor(() => expect(status()).toBe("unauthenticated"));
    expect(reason()).toBe("expired");
  });

  it("signs out with the logout reason on explicit sign-out", async () => {
    getMock.mockResolvedValueOnce({ status: 200, headers: {}, data: {} });
    renderProvider();

    await act(async () => {
      screen.getByText("sign-in").click();
    });
    await waitFor(() => expect(status()).toBe("authenticated"));

    await act(async () => {
      screen.getByText("sign-out").click();
    });

    expect(status()).toBe("unauthenticated");
    expect(reason()).toBe("logout");
  });
});
