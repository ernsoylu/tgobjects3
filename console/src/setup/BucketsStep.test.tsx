/**
 * Buckets step behaviour + accessibility (admin-dashboard Requirements 3.2, 3.7, 10).
 *
 * These component tests cover the observable behaviour of {@link BucketsStep}:
 *
 *  - **3.2 (assignment)** — a plain bucket→channel assignment submits the full
 *    set to `POST /admin/setup/buckets` and, when the backend accepts it with no
 *    outstanding choice, completes the step with the committed rows.
 *  - **3.7 (adopt vs reinitialise)** — when the backend flags a channel as
 *    already holding a manifest (`requires_choice`), the step renders an
 *    adopt/reinitialise choice; adopt re-submits with `on_existing: "adopt"`.
 *  - **3.7 (reinitialise gate)** — the reinitialise path surfaces the backend
 *    warning in a `role="alert"` region and keeps the submit disabled until the
 *    operator ticks the explicit confirmation, then re-submits with
 *    `on_existing: "reinitialise"` and does not complete until the backend
 *    clears `requires_choice`.
 *
 * The data hook (`../api/setup-buckets`) is mocked so the mutation can be
 * scripted to return a `requires_choice` result then an accepted one, without a
 * backend or React Query network call. Interactions use `fireEvent`, matching
 * the rest of the console suite.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BucketsStepBody, BucketsStepResult } from "../api/setup-buckets";
import { BucketsStep, type BucketRow } from "./BucketsStep";

// The mutate function the mocked hook returns; captures the body + onSuccess so
// a test can drive the scripted result through the component's callback.
type MutateOptions = { onSuccess?: (result: BucketsStepResult) => void };
const mutateMock = vi.fn<(body: BucketsStepBody, options?: MutateOptions) => void>();
let mutationState: { isPending: boolean; isError: boolean; error?: Error };

vi.mock("../api/setup-buckets", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/setup-buckets")>();
  return {
    ...actual,
    useSubmitBuckets: () => ({
      mutate: mutateMock,
      ...mutationState,
    }),
  };
});

/** The single seed row assigning `photos` → channel 42. */
const PHOTOS: readonly BucketRow[] = [{ bucketName: "photos", channelId: 42 }];

/** The recorded mutate call at `index`'s body + options, asserting it exists. */
function callAt(index: number): [BucketsStepBody, MutateOptions | undefined] {
  const call = mutateMock.mock.calls[index];
  expect(call).toBeDefined();
  return call as [BucketsStepBody, MutateOptions | undefined];
}

beforeEach(() => {
  mutateMock.mockReset();
  mutationState = { isPending: false, isError: false };
});

afterEach(() => {
  vi.clearAllMocks();
});

function accepted(): BucketsStepResult {
  return {
    buckets: [
      {
        bucket_name: "photos",
        channel_id: 42,
        existing_manifest: false,
        requires_choice: false,
        warning: null,
      },
    ],
    requires_choice: false,
  };
}

function requiresChoice(): BucketsStepResult {
  return {
    buckets: [
      {
        bucket_name: "photos",
        channel_id: 42,
        existing_manifest: true,
        requires_choice: true,
        warning: "Reinitialising this channel will make its existing bucket data inaccessible.",
      },
    ],
    requires_choice: true,
  };
}

describe("BucketsStep", () => {
  it("submits an assignment and completes when the backend accepts it (Req 3.2)", () => {
    const onComplete = vi.fn();
    render(<BucketsStep initialAssignments={PHOTOS} onComplete={onComplete} />);

    fireEvent.click(screen.getByRole("button", { name: "Assign buckets" }));

    expect(mutateMock).toHaveBeenCalledTimes(1);
    const [body, options] = callAt(0);
    expect(body).toEqual({ assignments: [{ bucket_name: "photos", channel_id: 42 }] });

    // Backend accepts with no outstanding choice: the step completes.
    act(() => options?.onSuccess?.(accepted()));
    expect(onComplete).toHaveBeenCalledWith([{ bucketName: "photos", channelId: 42 }]);
  });

  it("offers adopt vs reinitialise when a channel already has a manifest (Req 3.7)", () => {
    const onComplete = vi.fn();
    render(<BucketsStep initialAssignments={PHOTOS} onComplete={onComplete} />);

    fireEvent.click(screen.getByRole("button", { name: "Assign buckets" }));
    act(() => callAt(0)[1]?.onSuccess?.(requiresChoice()));

    // The adopt/reinitialise choice now appears and the step did not complete.
    expect(onComplete).not.toHaveBeenCalled();
    expect(screen.getByRole("radiogroup")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /adopt/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /reinitialise/i })).toBeInTheDocument();

    // Choosing adopt re-submits with on_existing: "adopt".
    fireEvent.click(screen.getByRole("radio", { name: /adopt/i }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm choices" }));

    expect(mutateMock).toHaveBeenCalledTimes(2);
    expect(callAt(1)[0]).toEqual({
      assignments: [{ bucket_name: "photos", channel_id: 42, on_existing: "adopt" }],
    });
  });

  it("gates reinitialise behind a warning and explicit confirmation (Req 3.7)", () => {
    const onComplete = vi.fn();
    render(<BucketsStep initialAssignments={PHOTOS} onComplete={onComplete} />);

    fireEvent.click(screen.getByRole("button", { name: "Assign buckets" }));
    act(() => callAt(0)[1]?.onSuccess?.(requiresChoice()));

    // Selecting reinitialise surfaces the data-loss warning as a live region.
    fireEvent.click(screen.getByRole("radio", { name: /reinitialise/i }));
    const warning = screen.getByRole("alert");
    expect(warning).toHaveTextContent(/inaccessible/i);

    // The confirm control is associated with the warning for assistive tech.
    const confirm = screen.getByRole("checkbox");
    expect(confirm).toHaveAttribute("aria-describedby", warning.id);

    // Submit stays disabled until the confirmation is ticked.
    const submit = screen.getByRole("button", { name: "Confirm choices" });
    expect(submit).toBeDisabled();

    // After explicit confirmation the step re-submits with reinitialise.
    fireEvent.click(confirm);
    expect(submit).not.toBeDisabled();
    fireEvent.click(submit);

    expect(mutateMock).toHaveBeenCalledTimes(2);
    expect(callAt(1)[0]).toEqual({
      assignments: [{ bucket_name: "photos", channel_id: 42, on_existing: "reinitialise" }],
    });

    // Still does not complete until the backend clears requires_choice.
    expect(onComplete).not.toHaveBeenCalled();
    act(() => callAt(1)[1]?.onSuccess?.(accepted()));
    expect(onComplete).toHaveBeenCalledWith([{ bucketName: "photos", channelId: 42 }]);
  });

  it("resets the reinitialise confirmation when switching back to adopt (Req 3.7)", () => {
    render(<BucketsStep initialAssignments={PHOTOS} onComplete={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Assign buckets" }));
    act(() => callAt(0)[1]?.onSuccess?.(requiresChoice()));

    fireEvent.click(screen.getByRole("radio", { name: /reinitialise/i }));
    fireEvent.click(screen.getByRole("checkbox"));
    // Switch to adopt and back: the earlier confirmation must not carry over.
    fireEvent.click(screen.getByRole("radio", { name: /adopt/i }));
    fireEvent.click(screen.getByRole("radio", { name: /reinitialise/i }));

    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Confirm choices" })).toBeDisabled();
  });
});
