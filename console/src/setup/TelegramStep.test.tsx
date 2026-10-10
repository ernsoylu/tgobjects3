/**
 * Telegram step behaviour + accessibility (admin-dashboard Requirements 2.1,
 * 2.3, 2.4, 2.7, 3.3, 10).
 *
 * These are complementary, light component tests for {@link TelegramStep}; the
 * formal component tests belong to task 7.3. They cover the observable
 * behaviour of the needs_code / 2FA loop:
 *
 *  - **2.1 / 2.3 (needs_code → code → success)** — submitting credentials POSTs
 *    them with no `code`; a `needs_code` result moves to the code prompt showing
 *    `attempts_remaining`; a correct code POSTs with `code` set and, on
 *    `authenticated`, completes the step.
 *  - **2.4 (3-wrong-code abort)** — a 401 from the third wrong code surfaces the
 *    authentication error and returns to credential entry.
 *  - **2.7 (Telegram unavailable)** — a 503 shows the error, preserves the
 *    entered `api_id`/`api_hash` in the form, and offers retry.
 *
 * The data hook (`../api/setup-telegram`) is mocked so the mutation can be
 * scripted without a backend or React Query network call, matching the console
 * suite convention (see `BucketsStep.test.tsx`).
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
    apiId: "12345",
    apiHash: "deadbeef",
    login: "+15551234567",
    code: "",
    attemptsRemaining: 3,
    ...overrides,
  };
}

/**
 * Render the step with controlled retained state: `onPatch` merges into local
 * state and re-renders, mimicking the wizard shell so phase transitions are
 * observable.
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
  return { onPatch, onComplete, getPhase: (): TelegramPhase => current.phase };
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

beforeEach(() => {
  mutateMock.mockReset();
  mutationState = { isPending: false, isError: false };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("TelegramStep", () => {
  it("drives needs_code → code → success (Req 2.1, 2.3)", () => {
    const onComplete = vi.fn();
    renderStep(stateWith(), onComplete);

    // Credential submit POSTs with no code.
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
    expect(callAt(0)[0]).toEqual({
      api_id: 12345,
      api_hash: "deadbeef",
      login: "+15551234567",
    });

    // needs_code moves to the code prompt showing attempts remaining.
    act(() => callAt(0)[1]?.onSuccess?.(needsCode(3)));
    expect(screen.getByLabelText("Login code")).toBeInTheDocument();
    // The remaining-attempts count is shown (rendered in its own <strong>).
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/attempts/i)).toBeInTheDocument();

    // A correct code POSTs with the code set and completes on authenticated.
    fireEvent.change(screen.getByLabelText("Login code"), { target: { value: "11111" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit code" }));
    expect(callAt(1)[0]).toEqual({
      api_id: 12345,
      api_hash: "deadbeef",
      login: "+15551234567",
      code: "11111",
    });

    act(() => callAt(1)[1]?.onSuccess?.(authed()));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it("returns to credential entry with an error on the 3rd wrong code (Req 2.4)", () => {
    const { getPhase } = renderStep(stateWith({ phase: "code", attemptsRemaining: 1 }));

    fireEvent.change(screen.getByLabelText("Login code"), { target: { value: "00000" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit code" }));

    // The 3rd failure aborts with 401; the step returns to credential entry
    // and surfaces the mapped authentication error (Req 2.4).
    act(() => callAt(0)[1]?.onError?.(axiosErrorWithStatus(401, "authentication failed")));

    expect(getPhase()).toBe("creds");
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer valid/i);
    expect(screen.getByLabelText("API ID")).toBeInTheDocument();
  });

  it("preserves credentials and offers retry on Telegram unavailable (Req 2.7)", () => {
    renderStep(stateWith());

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    act(() =>
      callAt(0)[1]?.onError?.(axiosErrorWithStatus(503, "The gateway is temporarily unavailable.")),
    );

    // The error is shown and the entered credentials are still in the form.
    expect(screen.getByRole("alert")).toHaveTextContent(/unavailable/i);
    expect(screen.getByLabelText("API ID")).toHaveValue("12345");
    expect(screen.getByLabelText("API hash")).toHaveValue("deadbeef");

    // Retry: the credentials resubmit unchanged.
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(mutateMock).toHaveBeenCalledTimes(2);
    expect(callAt(1)[0]).toEqual({
      api_id: 12345,
      api_hash: "deadbeef",
      login: "+15551234567",
    });
  });
});
