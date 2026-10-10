/**
 * Property test for Config-step range validation (admin-dashboard task 8.3).
 *
 * Formalises **Property 5** from the admin-dashboard design: for any storage-config
 * field tuple, the Config step POSTs and advances *if and only if* every field is
 * in its stated range — staging max size within [1 MiB, 1 TiB], cache max size
 * within [1 MiB, 100 GiB] (both inclusive), master key ≥ 16 characters, and both
 * paths non-empty. When any field is out of range the step reports that field with
 * its accepted range in a `role="alert"` region and does **not** call the mutation
 * or advance.
 *
 * The property is checked with `fast-check` over arbitrary field tuples. Two
 * complementary generators drive the two sides of the "iff":
 *
 *  - an **all-in-range** generator (every field inside its range) must POST exactly
 *    the collected body, advance via `onComplete` on backend acceptance, and surface
 *    no field error;
 *  - an **at-least-one-out-of-range** generator (at least one size below its min or
 *    above its max, or a short master key, or an empty path) must NOT POST, must NOT
 *    advance, and must report every offending field with its accepted range.
 *
 * `../api/setup-config` is mocked so no backend or React Query network call happens:
 * `useSubmitConfig` returns a spy mutation whose `onSuccess` is invoked synchronously
 * for in-range cases, matching the `ConfigStep.test` / `SettingsScreen.property.test`
 * convention. Each generated case renders a fresh tree and cleans it up, so the
 * mutation spy's call count reflects that one case.
 *
 * Validates: Requirements 3.4, 3.5, 12.6.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import fc from "fast-check";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConfigStepBody, ConfigStepResult } from "../api/setup-config";
import {
  CACHE_MAX_SIZE,
  CACHE_MIN_SIZE,
  ConfigStep,
  MASTER_KEY_MIN_LENGTH,
  STAGING_MAX_SIZE,
  STAGING_MIN_SIZE,
} from "./ConfigStep";
import type { ConfigStepState } from "./types";

type MutateOptions = { onSuccess?: (result: ConfigStepResult) => void };
const mutateMock = vi.fn<(body: ConfigStepBody, options?: MutateOptions) => void>();

vi.mock("../api/setup-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/setup-config")>();
  return {
    ...actual,
    useSubmitConfig: () => ({ mutate: mutateMock, isPending: false, isError: false }),
  };
});

beforeEach(() => {
  mutateMock.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

/**
 * The generated form input for one case. Sizes and the master key are raw strings,
 * exactly as the step retains them; paths are strings that may be empty.
 */
interface Generated {
  readonly stagingPath: string;
  readonly stagingMaxSize: number;
  readonly cachePath: string;
  readonly cacheMaxSize: number;
  readonly masterKeyLength: number;
}

/** Build the retained {@link ConfigStepState} from a generated tuple. */
function toState(gen: Generated): ConfigStepState {
  return {
    stagingPath: gen.stagingPath,
    stagingMaxSize: String(gen.stagingMaxSize),
    cachePath: gen.cachePath,
    cacheMaxSize: String(gen.cacheMaxSize),
    masterKey: "k".repeat(gen.masterKeyLength),
  };
}

/** Whether every field in the generated tuple is within its accepted range. */
function allInRange(gen: Generated): boolean {
  return (
    gen.stagingPath.trim() !== "" &&
    gen.stagingMaxSize >= STAGING_MIN_SIZE &&
    gen.stagingMaxSize <= STAGING_MAX_SIZE &&
    gen.cachePath.trim() !== "" &&
    gen.cacheMaxSize >= CACHE_MIN_SIZE &&
    gen.cacheMaxSize <= CACHE_MAX_SIZE &&
    gen.masterKeyLength >= MASTER_KEY_MIN_LENGTH
  );
}

/**
 * Render the step with the given state and submit the form once. The step reveals
 * field errors only after a submit attempt, so every case clicks "Save
 * configuration" before assertions read the mutation spy or the error regions.
 */
function renderAndSubmit(state: ConfigStepState, onComplete: () => void): void {
  render(<ConfigStep state={state} onPatch={vi.fn()} onComplete={onComplete} onBack={null} />);
  fireEvent.click(screen.getByRole("button", { name: "Save configuration" }));
}

/** A non-empty path, used wherever a case needs an in-range path value. */
const pathArb = fc.string({ minLength: 1, maxLength: 40 }).filter((value) => value.trim() !== "");

describe("Property 5: Config range validation blocks out-of-range submits", () => {
  it("POSTs the collected body and advances when every field is in range", () => {
    const inRangeArb: fc.Arbitrary<Generated> = fc.record({
      stagingPath: pathArb,
      stagingMaxSize: fc.integer({ min: STAGING_MIN_SIZE, max: STAGING_MAX_SIZE }),
      cachePath: pathArb,
      cacheMaxSize: fc.integer({ min: CACHE_MIN_SIZE, max: CACHE_MAX_SIZE }),
      masterKeyLength: fc.integer({ min: MASTER_KEY_MIN_LENGTH, max: MASTER_KEY_MIN_LENGTH + 32 }),
    });

    fc.assert(
      fc.property(inRangeArb, (gen) => {
        mutateMock.mockReset();
        const onComplete = vi.fn();
        const state = toState(gen);
        renderAndSubmit(state, onComplete);

        // No field is reported out of range.
        expect(screen.queryByRole("alert")).toBeNull();

        // The step POSTs exactly once with the collected, trimmed body.
        expect(mutateMock).toHaveBeenCalledTimes(1);
        const [body, options] = mutateMock.mock.calls[0] as [ConfigStepBody, MutateOptions];
        expect(body).toEqual({
          staging_path: state.stagingPath.trim(),
          staging_max_size: gen.stagingMaxSize,
          cache_path: state.cachePath.trim(),
          cache_max_size: gen.cacheMaxSize,
          master_key: state.masterKey,
        });

        // On backend acceptance it advances exactly once.
        act(() => options.onSuccess?.({ accepted: true }));
        expect(onComplete).toHaveBeenCalledTimes(1);

        cleanup();
      }),
      { numRuns: 200 },
    );
  });

  it("reports each out-of-range field with its range and neither POSTs nor advances", () => {
    // A below-min, above-max, or (for the master key) too-short override per field,
    // each wrapped in fc.option so a field may stay in range; at least one break is
    // forced below so the "only-if" direction is always exercised.
    const outOfRangeArb: fc.Arbitrary<Generated> = fc
      .record({
        stagingPathEmpty: fc.boolean(),
        stagingSizeBreak: fc.option(
          fc.oneof(
            fc.integer({ min: 0, max: STAGING_MIN_SIZE - 1 }),
            fc.integer({ min: STAGING_MAX_SIZE + 1, max: STAGING_MAX_SIZE + 1_000_000 }),
          ),
          { nil: null },
        ),
        cachePathEmpty: fc.boolean(),
        cacheSizeBreak: fc.option(
          fc.oneof(
            fc.integer({ min: 0, max: CACHE_MIN_SIZE - 1 }),
            fc.integer({ min: CACHE_MAX_SIZE + 1, max: CACHE_MAX_SIZE + 1_000_000 }),
          ),
          { nil: null },
        ),
        masterKeyShort: fc.option(fc.integer({ min: 0, max: MASTER_KEY_MIN_LENGTH - 1 }), {
          nil: null,
        }),
      })
      .map((choices) => {
        const gen: Generated = {
          stagingPath: choices.stagingPathEmpty ? "" : "/var/lib/tgo/staging",
          stagingMaxSize: choices.stagingSizeBreak ?? STAGING_MIN_SIZE,
          cachePath: choices.cachePathEmpty ? "" : "/var/lib/tgo/cache",
          cacheMaxSize: choices.cacheSizeBreak ?? CACHE_MIN_SIZE,
          masterKeyLength: choices.masterKeyShort ?? MASTER_KEY_MIN_LENGTH,
        };
        // Force at least one broken field: if the generator left everything in
        // range, break the staging size low so an invalid case is guaranteed.
        return allInRange(gen) ? { ...gen, stagingMaxSize: STAGING_MIN_SIZE - 1 } : gen;
      });

    fc.assert(
      fc.property(outOfRangeArb, (gen) => {
        mutateMock.mockReset();
        const onComplete = vi.fn();
        renderAndSubmit(toState(gen), onComplete);

        // The mutation is never called and the step never advances while invalid.
        expect(mutateMock).not.toHaveBeenCalled();
        expect(onComplete).not.toHaveBeenCalled();

        // Each out-of-range field reports its accepted range (or non-empty rule) in
        // a role="alert" region tied to the control.
        if (gen.stagingPath.trim() === "") {
          const node = document.getElementById("config-staging-path-error");
          expect(node?.getAttribute("role")).toBe("alert");
          expect(node?.textContent).toContain("staging directory path");
        }
        if (gen.stagingMaxSize < STAGING_MIN_SIZE || gen.stagingMaxSize > STAGING_MAX_SIZE) {
          const node = document.getElementById("config-staging-size-error");
          expect(node?.getAttribute("role")).toBe("alert");
          expect(node?.textContent).toContain(`${STAGING_MIN_SIZE}\u2013${STAGING_MAX_SIZE} bytes`);
        }
        if (gen.cachePath.trim() === "") {
          const node = document.getElementById("config-cache-path-error");
          expect(node?.getAttribute("role")).toBe("alert");
          expect(node?.textContent).toContain("cache directory path");
        }
        if (gen.cacheMaxSize < CACHE_MIN_SIZE || gen.cacheMaxSize > CACHE_MAX_SIZE) {
          const node = document.getElementById("config-cache-size-error");
          expect(node?.getAttribute("role")).toBe("alert");
          expect(node?.textContent).toContain(`${CACHE_MIN_SIZE}\u2013${CACHE_MAX_SIZE} bytes`);
        }
        if (gen.masterKeyLength < MASTER_KEY_MIN_LENGTH) {
          const node = document.getElementById("config-master-key-error");
          expect(node?.getAttribute("role")).toBe("alert");
          expect(node?.textContent).toContain(`at least ${MASTER_KEY_MIN_LENGTH} characters`);
        }

        cleanup();
      }),
      { numRuns: 200 },
    );
  });
});
