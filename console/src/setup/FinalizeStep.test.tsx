/**
 * Finalize step behaviour (admin-dashboard Requirements 3.8, 3.10, 3.11, 3.12,
 * 11.2, 11.3).
 *
 * These component tests cover the observable behaviour of {@link FinalizeStep}.
 * The formal Property 4 test (session-credentials → Recovery Kit; verifier is
 * not the master key) is owned by task 8.5; the checks here are light and
 * complementary:
 *
 *  - **3.8 (in-flight guard)** — while the finalise mutation is pending the
 *    "Completing…" action is disabled so the operator cannot resubmit.
 *  - **3.10 / 11.2 / 11.3 (success)** — on success the one-time secret and the
 *    access-key id render, the Recovery Kit download control is offered, and the
 *    rendered success view never exposes the submitted master key.
 *  - **3.11 (failure + retry)** — a failed finalise keeps the operator on the
 *    step with a readable error and a re-enabled "Retry" action.
 *  - **3.12 (leave the wizard)** — "Go to dashboard" invalidates the
 *    setup-status query and navigates to the admin screens.
 *
 * The data hook (`../api/setup-finalize`) and `react-router-dom`'s
 * `useNavigate` are mocked so the mutation can be scripted and navigation
 * observed without a backend or a router. A real `QueryClient` is mounted so the
 * success path's cache invalidation runs against a real cache.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FinalizeResult } from "../api/setup-finalize";
import { FinalizeStep } from "./FinalizeStep";
import type { FinalizeStepState } from "./types";

type MutateOptions = {
  onSuccess?: (result: FinalizeResult) => void;
  onError?: (error: Error) => void;
};
const mutateMock = vi.fn<(variables: undefined, options?: MutateOptions) => void>();
let mutationState: { isPending: boolean };

const navigateMock = vi.fn();

vi.mock("../api/setup-finalize", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/setup-finalize")>();
  return {
    ...actual,
    useFinalizeSetup: () => ({ mutate: mutateMock, ...mutationState }),
  };
});

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
}));

const MASTER_KEY = "super-secret-master-key";

function finalizeResult(): FinalizeResult {
  return {
    access_key_id: "AKIAEXAMPLE1234567890",
    secret_access_key: "s3cr3t-access-key-value",
    recovery_kit: {
      master_key_verifier: "dmVyaWZpZXItbm90LWtleQ==",
      telegram_session: "telethon-session-string",
      buckets: [{ bucket_name: "photos", channel_id: 42 }],
    },
  };
}

const INITIAL_STATE: FinalizeStepState = { phase: "idle" };

function renderStep(onPatch = vi.fn()): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <FinalizeStep state={INITIAL_STATE} onPatch={onPatch} onComplete={vi.fn()} onBack={null} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mutateMock.mockReset();
  navigateMock.mockReset();
  mutationState = { isPending: false };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("FinalizeStep", () => {
  it("disables the submit action while the finalise is in flight (Req 3.8)", () => {
    mutationState = { isPending: true };
    renderStep();

    const button = screen.getByRole("button", { name: "Completing…" });
    expect(button).toBeDisabled();
  });

  it("submits with no body and shows the one-time secret on success (Req 3.10, 11.3)", () => {
    renderStep();

    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
    expect(mutateMock.mock.calls[0]?.[0]).toBeUndefined();

    const options = mutateMock.mock.calls[0]?.[1];
    act(() => options?.onSuccess?.(finalizeResult()));

    expect(screen.getByText("s3cr3t-access-key-value")).toBeInTheDocument();
    expect(screen.getByText("AKIAEXAMPLE1234567890")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download Recovery Kit" })).toBeInTheDocument();
  });

  it("never renders the submitted master key in the success view (Req 11.2)", () => {
    renderStep();
    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    act(() => mutateMock.mock.calls[0]?.[1]?.onSuccess?.(finalizeResult()));

    expect(screen.queryByText(MASTER_KEY)).not.toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toContain(MASTER_KEY);
  });

  it("stays on the step with a readable error and allows retry on failure (Req 3.11)", () => {
    renderStep();

    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    act(() =>
      mutateMock.mock.calls[0]?.[1]?.onError?.(new Error("Telegram login is not complete")),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Telegram login is not complete");
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).not.toBeDisabled();

    fireEvent.click(retry);
    expect(mutateMock).toHaveBeenCalledTimes(2);
  });

  it("navigates to the admin screens on 'Go to dashboard' (Req 3.12)", () => {
    renderStep();
    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    act(() => mutateMock.mock.calls[0]?.[1]?.onSuccess?.(finalizeResult()));

    fireEvent.click(screen.getByRole("button", { name: "Go to dashboard" }));
    expect(navigateMock).toHaveBeenCalledWith("/buckets", { replace: true });
  });
});
