/**
 * Accessibility assertions for the login screen (task 14.2; Requirement 10).
 *
 * These component tests cover the parts of Requirement 10 that are observable
 * in the rendered DOM for {@link LoginScreen}:
 *
 *  - **10.3 (programmatic labels)** — every interactive control is reachable by
 *    its accessible role + name, which proves a programmatic label is exposed to
 *    assistive technology (the password/identifier fields via `<label htmlFor>`,
 *    the submit button via its text).
 *  - **10.5 (focus management on open)** — the screen is the first thing shown
 *    on the unauthenticated route, so on mount focus lands on the first editable
 *    control (the password field; the identifier is read-only). This mirrors the
 *    "move focus to the first interactive control" rule for an opening view.
 *  - **10.6 (status/error announced without moving focus)** — a rejected sign-in
 *    renders the inline error in a `role="alert"` live region, and focus stays on
 *    the password field rather than jumping to the message.
 *  - **10.2 (keyboard operability)** — the controls are native `<input>` /
 *    `<button>` elements reachable in tab order, asserted here via role queries.
 *
 * The auth layer is mocked at `../auth/context` so the test can script `signIn`
 * to resolve or reject and drive the observable `AuthState` without a backend.
 * Interactions use `fireEvent` + React's `act` (the console suite does not ship
 * `@testing-library/user-event`), matching the existing `AuthProvider` test.
 *
 * Validates: Requirements 10.1, 10.3, 10.5, 10.6.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuthContextValue, AuthState } from "../auth/context";
import { LoginScreen } from "./LoginScreen";

// Mock the auth context hook so the screen renders with a scripted `signIn`
// and a controllable `state`. `useAuth` is the only context member the screen
// touches, so a minimal fake value is enough.
const signInMock = vi.fn<(password: string) => Promise<void>>();
const signOutMock = vi.fn();
let authState: AuthState;

vi.mock("../auth/context", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../auth/context")>();
  return {
    ...actual,
    useAuth: (): AuthContextValue => ({
      state: authState,
      signIn: signInMock,
      signOut: signOutMock,
    }),
  };
});

beforeEach(() => {
  signInMock.mockReset();
  signOutMock.mockReset();
  authState = { status: "unauthenticated", endedReason: null };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("LoginScreen accessibility", () => {
  it("exposes a programmatic label for every interactive control (Req 10.3)", () => {
    render(<LoginScreen />);

    // Fields are reachable by their accessible name, which only works when a
    // label is programmatically associated (<label htmlFor> here).
    expect(screen.getByLabelText("Admin identifier")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    // The submit control is reachable by role + accessible name.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("renders native, keyboard-operable controls in the tab order (Req 10.1/10.2)", () => {
    render(<LoginScreen />);

    const password = screen.getByLabelText("Password");
    const submit = screen.getByRole("button", { name: "Sign in" });

    // Native input/button elements are keyboard-focusable with no negative
    // tabindex removing them from the natural tab order.
    expect(password.tagName).toBe("INPUT");
    expect(password).not.toHaveAttribute("tabindex", "-1");
    expect(submit.tagName).toBe("BUTTON");
    expect(submit).not.toHaveAttribute("tabindex", "-1");
  });

  it("moves focus to the password field on mount (Req 10.5)", () => {
    render(<LoginScreen />);

    // The read-only identifier is first in the DOM, but the first *editable*
    // control (password) receives focus so the operator can type immediately.
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  it("announces a rejected sign-in via role=alert without moving focus (Req 10.6)", async () => {
    signInMock.mockRejectedValueOnce(new Error("nope"));
    render(<LoginScreen />);

    const password = screen.getByLabelText<HTMLInputElement>("Password");
    fireEvent.change(password, { target: { value: "wrong-password" } });

    await act(async () => {
      fireEvent.submit(password.closest("form") as HTMLFormElement);
    });

    // The error surfaces in an assertive live region (role="alert").
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/authentication failed/i);

    // Focus is returned to the password field, not pulled onto the message, so
    // the announcement happens without a focus move away from the input.
    await waitFor(() => expect(password).toHaveFocus());
    expect(password).toHaveValue("");
    // The error region is associated with the field for AT.
    expect(password).toHaveAttribute("aria-describedby", alert.id);
  });

  it("announces a session-expired redirect via role=alert (Req 10.6)", async () => {
    authState = { status: "unauthenticated", endedReason: "expired" };
    render(<LoginScreen />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/no longer valid/i);
  });

  it("has no alert region when there is nothing to announce", () => {
    render(<LoginScreen />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
