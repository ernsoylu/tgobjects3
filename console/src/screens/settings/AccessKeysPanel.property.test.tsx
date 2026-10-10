/**
 * Property 7 — an access-key secret is shown exactly once and is absent after
 * dismissal (admin-dashboard task 10.5; Requirements 5.3, 11.3).
 *
 * The design's Correctness Property 7 states: for any access-key creation the
 * plaintext `secret_access_key` appears in the DOM exactly while the creation
 * result is displayed, and is absent from the DOM — and from any persistent
 * store — after the operator dismisses or navigates away. The design's
 * client-side secret rule (Data Models section) is stricter still: an
 * access-key secret lives only in component state for the lifetime of the
 * dialog and is never written to `localStorage`, `sessionStorage`, cookies, the
 * query cache, or logs.
 *
 * This is a true property test: `fast-check` drives arbitrary non-empty secret
 * strings through a mocked create mutation, and for every generated secret the
 * invariant is checked end to end:
 *
 *   1. before create, the secret is nowhere;
 *   2. after a successful create, the secret appears in the DOM exactly once
 *      (in the one-time result dialog);
 *   3. after the operator dismisses the dialog, the secret is gone from the DOM;
 *   4. after unmounting the panel (navigate away) the secret is likewise gone;
 *   5. throughout, the secret never reaches `localStorage`, `sessionStorage`,
 *      `document.cookie`, or the React Query cache.
 *
 * The data layer (`../../api/keys`) is mocked so the panel renders known
 * mutation state without a backend or any network call — the mocked
 * `useCreateKey` resolves `onSuccess` with the fast-check-generated
 * `CreatedKey`. A real `QueryClient` is mounted so the cache can be inspected
 * for leaks; the real panel code is exercised unchanged.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import fc from "fast-check";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CreatedKey, KeyEntry } from "../../api/keys";
import { AccessKeysPanel } from "./AccessKeysPanel";

/**
 * An arbitrary access-key secret.
 *
 * A real `secret_access_key` is an opaque, high-entropy token (base64url / hex
 * alphabet), not free-form prose. Modelling it that way is both faithful and
 * necessary for the DOM assertions: a one- or two-character free-form string
 * (e.g. a lone letter or space) would coincidentally be a substring of the
 * panel's static copy and defeat an exact-occurrence count without saying
 * anything about secret handling. We therefore draw ≥ 20 chars from the token
 * alphabet, which keeps the value arbitrary while guaranteeing it cannot
 * collide with the panel's fixed chrome.
 */
const secretArbitrary = fc
  .array(
    fc.constantFrom(
      ..."ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".split(""),
    ),
    { minLength: 20, maxLength: 80 },
  )
  .map((chars) => chars.join(""));

// Mutable hook state the mocked hooks return; reset per generated case.
let keysResult: {
  data?: readonly KeyEntry[];
  isPending: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => void;
};
const createMutate =
  vi.fn<
    (
      body: unknown,
      opts: { onSuccess: (r: CreatedKey) => void; onError: (e: unknown) => void },
    ) => void
  >();
const deleteMutate = vi.fn();

vi.mock("../../api/keys", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/keys")>();
  return {
    ...actual,
    useKeys: () => keysResult,
    useCreateKey: () => ({ mutate: createMutate, isPending: false }),
    useDeleteKey: () => ({ mutate: deleteMutate, isPending: false }),
  };
});

/**
 * Does the given secret appear anywhere in a persistent client store? The
 * design forbids it in `localStorage`, `sessionStorage`, cookies, and the query
 * cache (Requirement 11.3). This scans every key and value of web storage, the
 * cookie string, and the serialised React Query cache.
 */
function secretInPersistentStore(client: QueryClient, secret: string): boolean {
  const scanStorage = (storage: Storage): boolean => {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key === null) {
        continue;
      }
      if (key.includes(secret)) {
        return true;
      }
      const value = storage.getItem(key);
      if (value !== null && value.includes(secret)) {
        return true;
      }
    }
    return false;
  };

  if (scanStorage(window.localStorage) || scanStorage(window.sessionStorage)) {
    return true;
  }
  if (document.cookie.includes(secret)) {
    return true;
  }

  // The query cache: serialise every cached query's state and scan it. The
  // create hook deliberately never writes the secret here (keys.ts), so the
  // only cached data is the secret-free key list.
  const cacheDump = JSON.stringify(
    client
      .getQueryCache()
      .getAll()
      .map((query) => ({ key: query.queryKey, state: query.state.data })),
  );
  return cacheDump.includes(secret);
}

/** How many times `text` occurs in the whole rendered document body. */
function domOccurrences(text: string): number {
  const body = document.body.textContent ?? "";
  if (text.length === 0) {
    return 0;
  }
  let count = 0;
  let from = 0;
  for (;;) {
    const at = body.indexOf(text, from);
    if (at === -1) {
      break;
    }
    count += 1;
    from = at + text.length;
  }
  return count;
}

beforeEach(() => {
  createMutate.mockReset();
  deleteMutate.mockReset();
  keysResult = { data: [], isPending: false, isError: false, refetch: vi.fn() };
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AccessKeysPanel — Property 7: one-time secret (Req 5.3, 11.3)", () => {
  it("shows an arbitrary secret exactly while the dialog is open and never persists it", () => {
    fc.assert(
      fc.property(
        // Arbitrary opaque access-key secrets (see `secretArbitrary`).
        secretArbitrary,
        (secret) => {
          // Fresh state per generated secret so cases never bleed together.
          createMutate.mockReset();
          window.localStorage.clear();
          window.sessionStorage.clear();

          const client = new QueryClient({
            defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
          });

          const created: CreatedKey = {
            access_key_id: "AKIAEXAMPLEEXAMPLE12",
            secret_access_key: secret,
            label: null,
          };

          const view = render(
            <QueryClientProvider client={client}>
              <AccessKeysPanel />
            </QueryClientProvider>,
          );

          try {
            // (1) Before create the secret is nowhere.
            expect(domOccurrences(secret)).toBe(0);
            expect(secretInPersistentStore(client, secret)).toBe(false);

            // (2) A successful create surfaces the secret exactly once, in the
            // one-time result dialog.
            createMutate.mockImplementationOnce((_body, opts) => opts.onSuccess(created));
            fireEvent.click(screen.getByRole("button", { name: /create access key/i }));

            expect(screen.getByRole("dialog")).toBeInTheDocument();
            expect(domOccurrences(secret)).toBe(1);
            // Even while shown, it must not have been written to a store.
            expect(secretInPersistentStore(client, secret)).toBe(false);

            // (3) Dismissing the dialog removes the secret from the DOM.
            fireEvent.click(screen.getByRole("button", { name: /^done$/i }));
            expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
            expect(domOccurrences(secret)).toBe(0);
            expect(secretInPersistentStore(client, secret)).toBe(false);
          } finally {
            // (4) Navigating away (unmount) must also leave no trace.
            view.unmount();
          }

          expect(domOccurrences(secret)).toBe(0);
          expect(secretInPersistentStore(client, secret)).toBe(false);
          client.clear();
        },
      ),
      { numRuns: 100 },
    );
  });

  it("drops an arbitrary secret from the DOM when the panel unmounts without dismiss", () => {
    fc.assert(
      fc.property(secretArbitrary, (secret) => {
        createMutate.mockReset();
        window.localStorage.clear();
        window.sessionStorage.clear();

        const client = new QueryClient({
          defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
        });
        const created: CreatedKey = {
          access_key_id: "AKIAEXAMPLEEXAMPLE12",
          secret_access_key: secret,
          label: null,
        };

        const view = render(
          <QueryClientProvider client={client}>
            <AccessKeysPanel />
          </QueryClientProvider>,
        );

        createMutate.mockImplementationOnce((_body, opts) => opts.onSuccess(created));
        fireEvent.click(screen.getByRole("button", { name: /create access key/i }));
        expect(domOccurrences(secret)).toBe(1);

        // Operator navigates away with the dialog still open: unmount must clear
        // the secret from the DOM and never leave it in a persistent store.
        view.unmount();

        expect(domOccurrences(secret)).toBe(0);
        expect(secretInPersistentStore(client, secret)).toBe(false);
        client.clear();
      }),
      { numRuns: 100 },
    );
  });
});
