/**
 * Config step behaviour + accessibility (admin-dashboard Requirements 3.4, 3.5,
 * 11.1, 12.6).
 *
 * These are light, complementary component tests; the formal Property 5 range
 * test (every out-of-range field blocks the submit; all-in-range advances) is
 * owned by task 8.3. Here we cover the two observable ends of {@link ConfigStep}:
 *
 *  - **12.6 (out of range)** — an out-of-range size keeps the step put: no POST
 *    is made, `onComplete` is not called, and the offending field names its
 *    accepted range in a `role="alert"` region.
 *  - **3.4/3.5 (all valid)** — every field in range POSTs the collected body and,
 *    on backend acceptance, advances via `onComplete`.
 *  - **11.1 (secret)** — the master-key field is masked (`type="password"`).
 *
 * The data hook (`../api/setup-config`) is mocked so the mutation can be scripted
 * without a backend or React Query network call, matching `BucketsStep.test`.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConfigStepBody, ConfigStepResult } from "../api/setup-config";
import { CACHE_MAX_SIZE, ConfigStep, STAGING_MIN_SIZE } from "./ConfigStep";
import type { ConfigStepState } from "./types";

type MutateOptions = { onSuccess?: (result: ConfigStepResult) => void };
const mutateMock = vi.fn<(body: ConfigStepBody, options?: MutateOptions) => void>();
let mutationState: { isPending: boolean; isError: boolean; error?: Error };

vi.mock("../api/setup-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/setup-config")>();
  return {
    ...actual,
    useSubmitConfig: () => ({ mutate: mutateMock, ...mutationState }),
  };
});

/** A fully in-range config: staging 1 MiB, cache 1 MiB, 16-char master key. */
const VALID: ConfigStepState = {
  stagingPath: "/var/lib/tgo/staging",
  stagingMaxSize: String(STAGING_MIN_SIZE),
  cachePath: "/var/lib/tgo/cache",
  cacheMaxSize: String(STAGING_MIN_SIZE),
  masterKey: "0123456789abcdef",
};

beforeEach(() => {
  mutateMock.mockReset();
  mutationState = { isPending: false, isError: false };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ConfigStep", () => {
  it("masks the master key field (Req 11.1)", () => {
    render(<ConfigStep state={VALID} onPatch={vi.fn()} onComplete={vi.fn()} onBack={null} />);
    expect(screen.getByLabelText("Master key")).toHaveAttribute("type", "password");
  });

  it("blocks an out-of-range size, naming the field and range, without POST or advance (Req 12.6)", () => {
    const onComplete = vi.fn();
    // Cache size one byte over the 100 GiB ceiling; everything else valid.
    const state: ConfigStepState = { ...VALID, cacheMaxSize: String(CACHE_MAX_SIZE + 1) };
    render(<ConfigStep state={state} onPatch={vi.fn()} onComplete={onComplete} onBack={null} />);

    fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(mutateMock).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/cache maximum size/i);
    expect(alert).toHaveTextContent(/between/i);
  });

  it("POSTs the body and advances when every field is in range (Req 3.4/3.5)", () => {
    const onComplete = vi.fn();
    render(<ConfigStep state={VALID} onPatch={vi.fn()} onComplete={onComplete} onBack={null} />);

    fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    expect(mutateMock).toHaveBeenCalledTimes(1);
    const [body, options] = mutateMock.mock.calls[0] as [ConfigStepBody, MutateOptions];
    expect(body).toEqual({
      staging_path: "/var/lib/tgo/staging",
      staging_max_size: STAGING_MIN_SIZE,
      cache_path: "/var/lib/tgo/cache",
      cache_max_size: STAGING_MIN_SIZE,
      master_key: "0123456789abcdef",
    });

    act(() => options.onSuccess?.({ accepted: true }));
    expect(onComplete).toHaveBeenCalledTimes(1);
  });
});
