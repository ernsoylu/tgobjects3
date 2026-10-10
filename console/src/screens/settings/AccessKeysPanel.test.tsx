/**
 * Access Keys panel behaviour (admin-dashboard task 10.4; Requirements 5.1–5.3,
 * 11.3).
 *
 * Light component coverage of the panel's observable behaviour. The formal
 * Property 7 (secret shown exactly once, absent after dismissal) is owned by
 * sibling task 10.5; these tests keep to the directly-wired behaviour:
 *
 *  - the empty state renders when no keys exist (5.1);
 *  - a successful create surfaces the one-time `secret_access_key` and clears it
 *    from the DOM on dismiss (5.3, 11.3);
 *  - a list entry never renders a secret (5.2).
 *
 * The data layer (`../../api/keys`) is mocked so the panel renders known
 * query/mutation state without a backend or React Query network call.
 * Interactions use `fireEvent`, matching the rest of the console suite.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CreatedKey, KeyEntry } from "../../api/keys";
import { AccessKeysPanel } from "./AccessKeysPanel";

const SECRET = "s3cr3t-0 nce-only-abcdef0123456789";
const CREATED: CreatedKey = {
  access_key_id: "AKIAEXAMPLEEXAMPLE12",
  secret_access_key: SECRET,
  label: null,
};

// Mutable hook state the mocked hooks return; reset per test.
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

function loadedKeys(data: readonly KeyEntry[]): typeof keysResult {
  return { data, isPending: false, isError: false, refetch: vi.fn() };
}

beforeEach(() => {
  createMutate.mockReset();
  deleteMutate.mockReset();
  keysResult = loadedKeys([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("AccessKeysPanel", () => {
  it("renders the empty state when there are no keys (Req 5.1)", () => {
    render(<AccessKeysPanel />);
    expect(screen.getByText(/no access keys yet/i)).toBeInTheDocument();
  });

  it("lists keys by id + label with no secret column (Req 5.1/5.2)", () => {
    keysResult = loadedKeys([
      { access_key_id: "AKIAONE", label: "worker", disabled: false },
      { access_key_id: "AKIATWO", label: null, disabled: true },
    ]);
    render(<AccessKeysPanel />);

    expect(screen.getByText("AKIAONE")).toBeInTheDocument();
    expect(screen.getByText("worker")).toBeInTheDocument();
    expect(screen.getByText("AKIATWO")).toBeInTheDocument();
    // No secret value is ever present for a listed key.
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("shows the one-time secret on create and clears it on dismiss (Req 5.3, 11.3)", () => {
    render(<AccessKeysPanel />);

    // Submit the create form; the mocked mutation reports success with a secret.
    createMutate.mockImplementationOnce((_body, opts) => opts.onSuccess(CREATED));
    fireEvent.click(screen.getByRole("button", { name: /create access key/i }));

    // The secret is shown exactly once, in the result dialog.
    expect(screen.getByText(SECRET)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent(/shown only once/i);

    // Dismiss the dialog; the secret leaves the DOM.
    fireEvent.click(screen.getByRole("button", { name: /^done$/i }));
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("opens an explicit confirm dialog before deleting (Req 5.4)", () => {
    keysResult = loadedKeys([{ access_key_id: "AKIAONE", label: null, disabled: false }]);
    render(<AccessKeysPanel />);

    // The row delete button only opens the confirm; it must not delete directly.
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    expect(deleteMutate).not.toHaveBeenCalled();

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(/delete access key/i);

    // Confirming inside the dialog issues the delete.
    fireEvent.click(screen.getByRole("button", { name: /delete key/i }));
    expect(deleteMutate).toHaveBeenCalledWith("AKIAONE", expect.anything());
  });
});
