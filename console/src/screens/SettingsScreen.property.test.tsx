/**
 * Property test for Runtime Settings range validation (task 10.3).
 *
 * Formalises **Property 6** from the admin-dashboard design: the Runtime
 * Settings panel submits a runtime-settings edit to the Admin API *if and only
 * if* every field is in its backend-mirrored range — `read_ahead` is 0–32
 * inclusive, every other field is an integer ≥ 1 — and an out-of-range field is
 * reported with its accepted range while the mutation is **not** called.
 *
 * The property is checked with `fast-check` over arbitrary integer tuples for
 * the five fields (`staging_max_size`, `cache_max_size`, `cache_half_life`,
 * `read_ahead`, `upload_concurrency`). Two complementary generators drive the
 * two sides of the "iff":
 *
 *  - an **all-in-range** generator (every field inside its range) must submit
 *    exactly the entered values and surface no field error;
 *  - an **at-least-one-out-of-range** generator (at least one field below its
 *    min, or `read_ahead` above 32) must NOT submit and must report every
 *    offending field with its accepted range.
 *
 * The `../api/settings` hooks are mocked so no backend or React Query network
 * call happens: `useSettings` returns a fixed snapshot and `useUpdateSettings`
 * returns a spy mutation, matching the console suite's convention
 * (`SettingsScreen.runtime.test.tsx`). Each generated case renders a fresh tree
 * and cleans it up, so the mutation spy's call count reflects that one case.
 *
 * Validates: Requirements 4.2, 4.3.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import fc from "fast-check";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsValues } from "../api/settings";
import { SettingsScreen } from "./SettingsScreen";

/**
 * The seed snapshot the mocked `useSettings` returns. Deliberately distinct
 * from every generated tuple below (see the generators) so the dirty-check is
 * always satisfied and submission depends purely on range validity — the
 * invariant Property 6 is about.
 */
const SEED: SettingsValues = {
  staging_max_size: 1_000_000,
  cache_max_size: 1_000_000,
  cache_half_life: 1_000_000,
  read_ahead: 7,
  upload_concurrency: 1_000_000,
};

let settingsResult: {
  data?: SettingsValues;
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => void;
};
const mutateMock = vi.fn();

vi.mock("../api/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/settings")>();
  return {
    ...actual,
    useSettings: () => settingsResult,
    useUpdateSettings: () => ({ mutate: mutateMock, isPending: false }),
  };
});

beforeEach(() => {
  mutateMock.mockReset();
  settingsResult = { data: SEED, isPending: false, isError: false, refetch: vi.fn() };
});

afterEach(() => {
  vi.clearAllMocks();
});

/** The five editable fields with their label and backend-mirrored range. */
const FIELDS = [
  { key: "staging_max_size", label: "Staging size (bytes)", min: 1, max: undefined },
  { key: "cache_max_size", label: "Cache size (bytes)", min: 1, max: undefined },
  { key: "cache_half_life", label: "Cache half-life (seconds)", min: 1, max: undefined },
  { key: "read_ahead", label: "Read-ahead count", min: 0, max: 32 },
  { key: "upload_concurrency", label: "Upload concurrency", min: 1, max: undefined },
] as const satisfies readonly {
  key: keyof SettingsValues;
  label: string;
  min: number;
  max: number | undefined;
}[];

type FieldValues = Record<keyof SettingsValues, number>;

/** True when `value` is inside the accepted range for `field`. */
function inRange(field: (typeof FIELDS)[number], value: number): boolean {
  if (value < field.min) {
    return false;
  }
  return field.max === undefined || value <= field.max;
}

/** The expected inline error range phrase for an out-of-range field value. */
function expectedRangeMessage(field: (typeof FIELDS)[number], value: number): string {
  if (value < field.min) {
    return `Must be at least ${field.min}.`;
  }
  // read_ahead is the only bounded field, so an above-range value is an upper-bound hit.
  return `Must be at most ${String(field.max)}.`;
}

/**
 * Type a tuple of values into the five numeric inputs, then submit the form.
 * Returns nothing; assertions read the mutation spy and the rendered errors.
 */
function enterAndSubmit(values: FieldValues): void {
  render(<SettingsScreen />);
  for (const field of FIELDS) {
    const input = screen.getByLabelText<HTMLInputElement>(field.label);
    fireEvent.change(input, { target: { value: String(values[field.key]) } });
  }
  fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
}

describe("Property 6: runtime-settings validation mirrors the backend ranges", () => {
  it("submits exactly the entered values when every field is in range", () => {
    // In-range generators per field, each kept distinct from the seed so the
    // form is dirty (SEED uses large/odd values the small in-range samples avoid).
    const inRangeArb = fc.record<FieldValues>({
      staging_max_size: fc.integer({ min: 1, max: 10_000 }),
      cache_max_size: fc.integer({ min: 1, max: 10_000 }),
      cache_half_life: fc.integer({ min: 1, max: 10_000 }),
      read_ahead: fc.integer({ min: 0, max: 32 }),
      upload_concurrency: fc.integer({ min: 1, max: 10_000 }),
    });

    fc.assert(
      fc.property(inRangeArb, (values) => {
        mutateMock.mockReset();
        enterAndSubmit(values);

        // No field reports an error.
        expect(screen.queryByRole("alert")).toBeNull();
        // Submitted exactly once with exactly the entered integers.
        expect(mutateMock).toHaveBeenCalledTimes(1);
        expect(mutateMock.mock.calls[0]?.[0]).toEqual(values);

        cleanup();
      }),
      { numRuns: 200 },
    );
  });

  it("does not submit and reports each offending field with its range when any is out of range", () => {
    // A base in-range tuple plus a non-empty set of fields pushed out of range:
    // below-min for any field, or above-32 for read_ahead. This guarantees at
    // least one invalid field so the "only-if" direction is exercised.
    const base = fc.record<FieldValues>({
      staging_max_size: fc.integer({ min: 1, max: 10_000 }),
      cache_max_size: fc.integer({ min: 1, max: 10_000 }),
      cache_half_life: fc.integer({ min: 1, max: 10_000 }),
      read_ahead: fc.integer({ min: 0, max: 32 }),
      upload_concurrency: fc.integer({ min: 1, max: 10_000 }),
    });

    // For each field choose how to break it (or leave it valid); force at least one break.
    const breakers = fc.record<Record<keyof SettingsValues, number | null>>({
      // null => leave in range; number => the out-of-range override to apply.
      staging_max_size: fc.option(fc.integer({ min: -10_000, max: 0 }), { nil: null }),
      cache_max_size: fc.option(fc.integer({ min: -10_000, max: 0 }), { nil: null }),
      cache_half_life: fc.option(fc.integer({ min: -10_000, max: 0 }), { nil: null }),
      // read_ahead can break low (<0) or high (>32).
      read_ahead: fc.option(
        fc.oneof(fc.integer({ min: -10_000, max: -1 }), fc.integer({ min: 33, max: 10_000 })),
        { nil: null },
      ),
      upload_concurrency: fc.option(fc.integer({ min: -10_000, max: 0 }), { nil: null }),
    });

    fc.assert(
      fc.property(base, breakers, (baseValues, overrides) => {
        // Ensure at least one field is actually broken; if the generator chose
        // none, break read_ahead high so the invalid side is always exercised.
        const anyBroken = Object.values(overrides).some((v) => v !== null);
        const effective: Record<keyof SettingsValues, number | null> = anyBroken
          ? overrides
          : { ...overrides, read_ahead: 33 };

        const values: FieldValues = { ...baseValues };
        for (const field of FIELDS) {
          const override = effective[field.key];
          if (override !== null) {
            values[field.key] = override;
          }
        }

        mutateMock.mockReset();
        enterAndSubmit(values);

        // The mutation is never called while any field is out of range.
        expect(mutateMock).not.toHaveBeenCalled();

        // Every out-of-range field reports its accepted range in an alert.
        for (const field of FIELDS) {
          if (!inRange(field, values[field.key])) {
            const errorId = `settings-${field.key}-error`;
            const node = document.getElementById(errorId);
            expect(node).not.toBeNull();
            expect(node?.getAttribute("role")).toBe("alert");
            expect(node?.textContent).toBe(expectedRangeMessage(field, values[field.key]));
          }
        }

        cleanup();
      }),
      { numRuns: 200 },
    );
  });
});
