/**
 * Access Keys settings panel (admin-dashboard Requirements 5.1–5.6, 11.3).
 *
 * The Access Keys tab of {@link SettingsScreen}. It wraps the access-key data
 * hooks in `../../api/keys` and renders:
 *
 *   - **List (5.1)** — existing keys by `access_key_id` + label, with an empty
 *     state when none exist. A list entry never shows a secret (5.2): the
 *     backend `GET /admin/keys` returns none, and this panel renders none.
 *   - **Create (5.2/5.3, 11.3)** — a form with an optional label. On success the
 *     new `secret_access_key` is shown **exactly once** in a Radix dialog with a
 *     copy-to-clipboard action; dismissing the dialog (or unmounting the panel,
 *     e.g. navigating away) clears the secret from component state and the DOM.
 *     The secret is never written to storage or the query cache.
 *   - **Delete (5.4/5.5/5.6)** — behind an explicit Radix confirm dialog. On
 *     success the key is removed from the list (the list query is invalidated).
 *     A delete of an absent key (HTTP 404) surfaces a readable error and still
 *     refreshes the list, since the delete hook invalidates on settle (5.6).
 *
 * Accessibility: Radix `Dialog` traps focus and moves it into the dialog on
 * open and back to the trigger on close; status/error regions use `role="alert"`
 * so a change is announced in place (Requirement 10).
 */
import * as Dialog from "@radix-ui/react-dialog";
import axios from "axios";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  useCreateKey,
  useDeleteKey,
  useKeys,
  type CreatedKey,
  type KeyEntry,
} from "../../api/keys";

/**
 * Turn an unknown thrown value into an operator-readable message. The Admin API
 * reports failures as JSON `{ "detail": ... }` (a 404 for an absent key carries
 * a plain-string detail); fall back to the axios/Error message otherwise.
 */
function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    if (error.response?.status === 404) {
      return "That access key no longer exists. The list has been refreshed.";
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Unknown error";
}

/** The Access Keys panel body. */
export function AccessKeysPanel(): JSX.Element {
  const keys = useKeys();
  const create = useCreateKey();
  const deleteKey = useDeleteKey();

  // The one-time create result. Held only while the result dialog is open; set
  // on a successful create and cleared on dismiss or unmount (Req 5.3, 11.3).
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [label, setLabel] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  // The key currently pending a delete confirmation, or null when the confirm
  // dialog is closed.
  const [pendingDelete, setPendingDelete] = useState<KeyEntry | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Defence in depth for Property 7: if the panel unmounts (operator navigates
  // away) while the secret dialog is open, drop the secret from memory so it
  // cannot linger. React also removes it from the DOM on unmount.
  useEffect(() => {
    return () => {
      setCreated(null);
    };
  }, []);

  const onCreate = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (create.isPending) {
        return;
      }
      setCreateError(null);
      const trimmed = label.trim();
      create.mutate(
        { label: trimmed === "" ? null : trimmed },
        {
          onSuccess: (result) => {
            setCreated(result);
            setLabel("");
          },
          onError: (error) => {
            setCreateError(errorMessage(error));
          },
        },
      );
    },
    [create, label],
  );

  const dismissSecret = useCallback(() => {
    // Clearing component state removes the only in-memory copy of the secret
    // and unmounts the dialog subtree holding it from the DOM (Req 5.3, 11.3).
    setCreated(null);
  }, []);

  const confirmDelete = useCallback(() => {
    if (!pendingDelete || deleteKey.isPending) {
      return;
    }
    setDeleteError(null);
    const target = pendingDelete;
    deleteKey.mutate(target.access_key_id, {
      onSuccess: () => {
        setPendingDelete(null);
      },
      onError: (error) => {
        // On a 404 the list is still refreshed (the hook invalidates on
        // settle); surface a readable error and close the confirm (Req 5.6).
        setDeleteError(errorMessage(error));
        setPendingDelete(null);
      },
    });
  }, [deleteKey, pendingDelete]);

  return (
    <div className="recovery-panel">
      <div className="keys-header">
        <div>
          <h2 className="recovery-subtitle">Access Keys</h2>
          <p className="recovery-description">
            S3 access keys for this gateway. The secret is shown once at creation and never again —
            copy it before dismissing.
          </p>
        </div>
      </div>

      <form className="keys-create" onSubmit={onCreate}>
        <div className="keys-create-field">
          <label className="settings-label" htmlFor="keys-label">
            Label (optional)
          </label>
          <input
            id="keys-label"
            className="settings-input"
            type="text"
            value={label}
            placeholder="e.g. backup-worker"
            disabled={create.isPending}
            onChange={(event) => setLabel(event.target.value)}
          />
        </div>
        <button type="submit" className="btn btn--primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create access key"}
        </button>
      </form>

      {createError ? (
        <p className="settings-error" role="alert">
          {createError}
        </p>
      ) : null}

      {deleteError ? (
        <p className="settings-error" role="alert">
          {deleteError}
        </p>
      ) : null}

      <KeyList
        keys={keys}
        onRequestDelete={(key) => {
          setDeleteError(null);
          setPendingDelete(key);
        }}
      />

      <CreatedKeyDialog created={created} onDismiss={dismissSecret} />

      <ConfirmDeleteDialog
        target={pendingDelete}
        isDeleting={deleteKey.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

/** The access-key list, with loading / error / empty states. */
function KeyList(props: {
  keys: ReturnType<typeof useKeys>;
  onRequestDelete: (key: KeyEntry) => void;
}): JSX.Element {
  const { keys, onRequestDelete } = props;

  if (keys.isError) {
    return (
      <div className="screen-error" role="alert">
        <p>Could not load access keys: {errorMessage(keys.error)}</p>
        <button type="button" className="btn" onClick={() => void keys.refetch()}>
          Try again
        </button>
      </div>
    );
  }

  if (keys.isPending) {
    return <p className="screen-note">Loading access keys…</p>;
  }

  if (keys.data.length === 0) {
    return <p className="recovery-value-muted">No access keys yet. Create one to get started.</p>;
  }

  return (
    <table className="recovery-table">
      <thead>
        <tr>
          <th>Access key id</th>
          <th>Label</th>
          <th>Status</th>
          <th aria-label="Actions" />
        </tr>
      </thead>
      <tbody>
        {keys.data.map((key) => (
          <tr key={key.access_key_id}>
            <td>
              <code>{key.access_key_id}</code>
            </td>
            <td>{key.label ?? <span className="recovery-value-muted">(no label)</span>}</td>
            <td>{key.disabled ? "Disabled" : "Active"}</td>
            <td>
              <button type="button" className="btn btn--retry" onClick={() => onRequestDelete(key)}>
                Delete
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The one-time-secret dialog (Requirement 5.3, 11.3).
 *
 * Open iff `created` is non-null. Shows the new `secret_access_key` once with a
 * copy-to-clipboard action; closing the dialog calls `onDismiss`, which clears
 * the secret from component state so it leaves the DOM.
 */
function CreatedKeyDialog(props: {
  created: CreatedKey | null;
  onDismiss: () => void;
}): JSX.Element {
  const { created, onDismiss } = props;
  const [copied, setCopied] = useState(false);

  // Reset the "copied" acknowledgement whenever a new secret is shown.
  useEffect(() => {
    setCopied(false);
  }, [created]);

  const onCopy = useCallback(() => {
    if (!created) {
      return;
    }
    const secret = created.secret_access_key;
    void navigator.clipboard
      ?.writeText(secret)
      .then(() => setCopied(true))
      .catch(() => setCopied(false));
  }, [created]);

  return (
    <Dialog.Root
      open={created !== null}
      onOpenChange={(open) => {
        if (!open) {
          onDismiss();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-describedby="keys-secret-desc">
          <Dialog.Title className="dialog-title">Access key created</Dialog.Title>
          <Dialog.Description className="dialog-description" id="keys-secret-desc">
            Copy the secret now. For security it is shown only once and cannot be retrieved again.
          </Dialog.Description>

          {created ? (
            <dl className="keys-secret">
              <div className="keys-secret-row">
                <dt>Access key id</dt>
                <dd>
                  <code>{created.access_key_id}</code>
                </dd>
              </div>
              <div className="keys-secret-row">
                <dt>Secret access key</dt>
                <dd>
                  <code className="keys-secret-value">{created.secret_access_key}</code>
                </dd>
              </div>
            </dl>
          ) : null}

          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onCopy}>
              {copied ? "Copied" : "Copy secret"}
            </button>
            <Dialog.Close asChild>
              <button type="button" className="btn btn--primary">
                Done
              </button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/**
 * The explicit delete-confirmation dialog (Requirement 5.4/5.5).
 *
 * Open iff `target` is non-null. Deleting is irreversible and immediately
 * revokes the key, so the operator must confirm before the DELETE is issued.
 */
function ConfirmDeleteDialog(props: {
  target: KeyEntry | null;
  isDeleting: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}): JSX.Element {
  const { target, isDeleting, onConfirm, onCancel } = props;

  return (
    <Dialog.Root
      open={target !== null}
      onOpenChange={(open) => {
        if (!open && !isDeleting) {
          onCancel();
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-describedby="keys-delete-desc">
          <Dialog.Title className="dialog-title">Delete access key?</Dialog.Title>
          <Dialog.Description className="dialog-description" id="keys-delete-desc">
            {target ? (
              <>
                This permanently deletes <code>{target.access_key_id}</code>
                {target.label ? ` (${target.label})` : ""} and immediately revokes it. This cannot
                be undone.
              </>
            ) : null}
          </Dialog.Description>

          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onCancel} disabled={isDeleting}>
              Cancel
            </button>
            <button
              type="button"
              className="btn btn--retry"
              onClick={onConfirm}
              disabled={isDeleting}
            >
              {isDeleting ? "Deleting…" : "Delete key"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
