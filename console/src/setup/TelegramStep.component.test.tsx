/**
 * Formal component tests for the Telegram setup step (admin-dashboard task 7.3;
 * Requirements 2.4, 2.7).
 *
 * The light suite in `TelegramStep.test.tsx` (task 7.2) sketches each scenario
 * with a single transition. This suite is the formal component-test coverage:
 * it drives the three required flows end-to-end with stronger assertions and
 * deliberately avoids duplicating the exact cases there.
 *
 *  1. **needs_code → code → success (full happy loop)** — the credentials POST
 *     carries no `code`; the code prompt reports `attempts_remaining`; the code
 *     POST carries the entered `code`; `onComplete` fires exactly once on
 *     `authenticated`; the in-progress indicator disables the controls while a
 *     submit is in flight and a stale error is cleared on success.
 *  2. **3-wrong-code abort/return (Req 2.4)** — the attempt counter is driven
 *     all the way down (3 → 2 → 1) with the displayed count asserted at each
 *     wrong code, then the 3rd failure's 401 returns to credential entry with
 *     the authentication error and resets `attemptsRemaining` to the maximum via
 *     the retained slice.
 *  3. **Telegram-unavailable (503) preservation + retry (Req 2.7)** — a 503
 *     shows the error and stays on the credential step with every entered field
 *     (`api_id`, `api_hash`, `login`) preserved; a retry resubmits the identical
 *     body and clears the stale error on success.
 *
 * The data hook (`../api/setup-telegram`) is mocked so the scripted mutation
 * runs with no backend/React Query network call, matching the console suite
 * convention (see `TelegramStep.test.tsx`, `BucketsStep.test.tsx`).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { AxiosError, AxiosHeaders } from "axios";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TelegramStepBody, TelegramStepResult } from "../api/setup-telegram";
import { TelegramStep } from "./TelegramStep";
import type { StepComponentProps, TelegramPhase, TelegramStepState } from "./types";

type MutateOptions = {
  onSuccess?: (result: TelegramStepResult) => void;
  onError?: (error: unknown) => void;
};
const mutateMock = vi.fn<(body: TelegramStepBody, options?: MutateOptions) => void>();
let mutationState: { isPending: boolean; isError: boolean; error?: Error };

vi.mock("../api/setup-telegram", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/setup-telegram")>();
  return {
    ...actual,
    useTelegramLogin: () => ({ mutate: mutateMock, ...mutationState }),
  };
});

/** The recorded mutate call at `index`, asserting it exists. */
function callAt(index: number): [TelegramStepBody, MutateOptions | undefined] {
  const call = mutateMock.mock.calls[index];
  expect(call).toBeDefined();
  return call as [TelegramStepBody, MutateOptions | undefined];
}

function stateWith(overrides: Partial<TelegramStepState> = {}): TelegramStepState {
  return {
    phase: "creds",
    apiId: "987654",
    apiHash: "cafef00d",
    login: "+15557654321",
    code: "",
    attemptsRemaining: 3,
    ...overrides,
  };
}

/**
 * Render the step with controlled retained state. `onPatch` merges patches into
 * the local slice and re-renders (mimicking the wizard shell) so phase and
 * `attemptsRemaining` transitions are observable; `getState` exposes the latest
 * retained slice so patch-only effects (e.g. the 401 reset) can be asserted.
 */
function renderStep(initial: TelegramStepState, onComplete = vi.fn()) {
  let current = initial;
  const onPatch = vi.fn((patch: Partial<TelegramStepState>) => {
    current = { ...current, ...patch };
    rerender(
      <TelegramStep state={current} onPatch={onPatch} onComplete={onComplete} onBack={null} />,
    );
  });
  const props: StepComponentProps<TelegramStepState> = {
    state: current,
    onPatch,
    onComplete,
    onBack: null,
  };
  const { rerender } = render(<TelegramStep {...props} />);
  return {
    onPatch,
    onComplete,
    getPhase: (): TelegramPhase => current.phase,
    getState: (): TelegramStepState => current,
  };
}

function needsCode(attempts: number): TelegramStepResult {
  return { authenticated: false, needs_code: true, attempts_remaining: attempts };
}

function authed(): TelegramStepResult {
  return { authenticated: true, needs_code: false, attempts_remaining: 3 };
}

function axiosErrorWithStatus(status: number, detail: string): AxiosError {
  const error = new AxiosError(detail);
  error.response = {
    status,
    statusText: "",
    data: { detail },
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

/** Submit the current code form with `value`. */
function submitCode(value: string): void {
  fireEvent.change(screen.getByLabelText("Login code"), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Submit code" }));
}

beforeEach(() => {
  mutateMock.mockReset();
  mutationState = { isPending: false, isError: false };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("TelegramStep (component)", () => {
  it("drives the full needs_code → code → success loop with in-progress gating", () => {
    const onComplete = vi.fn();
    const { onPatch, getPhase } = renderStep(stateWith(), onComplete);

    // Leg 1: credentials POST carries NO code (begin login).
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
    const credentialsBody = callAt(0)[0];
    expect(credentialsBody).toEqual({
      api_id: 987654,
      api_hash: "cafef00d",
      login: "+15557654321",
    });
    expect(credentialsBody).not.toHaveProperty("code");

    // needs_code moves to the code prompt and records the remaining attempts.
    act(() => callAt(0)[1]?.onSuccess?.(needsCode(3)));
    expect(getPhase()).toBe("code");
    const prompt = screen.getByLabelText("Login code").closest("form");
    expect(prompt).not.toBeNull();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/attempts\s+remaining/i)).toBeInTheDocument();

    // Leg 2: the correct code POSTs WITH the code set.
    fireEvent.change(screen.getByLabelText("Login code"), { target: { value: "246810" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit code" }));
    expect(callAt(1)[0]).toEqual({
      api_id: 987654,
      api_hash: "cafef00d",
      login: "+15557654321",
      code: "246810",
    });

    // authenticated completes the step exactly once and marks the slice done.
    act(() => callAt(1)[1]?.onSuccess?.(authed()));
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({ phase: "done" }));
  });

  it("disables the controls while a code submit is in flight (Req 2.3 gating)", () => {
    // A pending mutation must block a re-submit so a double submit cannot race
    // the backend wizard session underpinning the 2.4/2.7 flows.
    mutationState = { isPending: true, isError: false };
    renderStep(stateWith({ phase: "code", attemptsRemaining: 3 }));

    expect(screen.getByRole("button", { name: "Verifying…" })).toBeDisabled();
    expect(screen.getByLabelText("Login code")).toBeDisabled();
  });

  it("counts attempts down and aborts to credential entry on the 3rd wrong code (Req 2.4)", () => {
    const { getPhase, getState } = renderStep(stateWith({ phase: "code", attemptsRemaining: 3 }));

    // Wrong code #1: backend decrements to 2 and keeps prompting.
    expect(screen.getByText("3")).toBeInTheDocument();
    submitCode("00000");
    expect(callAt(0)[0]).toHaveProperty("code", "00000");
    act(() => callAt(0)[1]?.onSuccess?.(needsCode(2)));
    expect(getPhase()).toBe("code");
    expect(screen.getByText("2")).toBeInTheDocument();

    // Wrong code #2: backend decrements to 1 and keeps prompting.
    submitCode("11111");
    act(() => callAt(1)[1]?.onSuccess?.(needsCode(1)));
    expect(getPhase()).toBe("code");
    expect(screen.getByText("1")).toBeInTheDocument();
    // Singular copy at the last attempt.
    expect(screen.getByText(/attempt\s+remaining/i)).toBeInTheDocument();

    // Wrong code #3: the backend aborts with 401 and resets the login.
    submitCode("22222");
    act(() => callAt(2)[1]?.onError?.(axiosErrorWithStatus(401, "authentication failed")));

    // The step returns to credential entry, surfaces the auth error, and resets
    // the retained attempt counter to the maximum (Req 2.4).
    expect(getPhase()).toBe("creds");
    expect(screen.getByLabelText("API ID")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer valid/i);
    expect(getState().attemptsRemaining).toBe(3);
    expect(getState().code).toBe("");
    // No stray extra submit was issued by the abort.
    expect(mutateMock).toHaveBeenCalledTimes(3);
  });

  it("preserves every credential field and resubmits unchanged on 503 retry (Req 2.7)", () => {
    renderStep(stateWith());

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    act(() =>
      callAt(0)[1]?.onError?.(axiosErrorWithStatus(503, "The gateway is temporarily unavailable.")),
    );

    // Error shown, still on the credential step, every entered field preserved.
    expect(screen.getByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.getByLabelText("API ID")).toHaveValue("987654");
    expect(screen.getByLabelText("API hash")).toHaveValue("cafef00d");
    expect(screen.getByLabelText("Phone number or bot token")).toHaveValue("+15557654321");

    // Retry resubmits the identical body (nothing discarded by the failure).
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(mutateMock).toHaveBeenCalledTimes(2);
    expect(callAt(1)[0]).toEqual({
      api_id: 987654,
      api_hash: "cafef00d",
      login: "+15557654321",
    });

    // A successful retry clears the stale 503 error and advances via needs_code.
    act(() => callAt(1)[1]?.onSuccess?.(needsCode(3)));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Login code")).toBeInTheDocument();
  });
});
