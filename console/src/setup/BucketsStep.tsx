/**
 * Buckets step of the first-run Setup Wizard (admin-dashboard Requirements 3.2, 3.7).
 *
 * This step assigns each bucket to a Telegram channel and submits the full set
 * through `POST /admin/setup/buckets` (driven by {@link useSubmitBuckets}). The
 * backend probes each channel for existing bucket-manifest messages and returns
 * a per-bucket {@link BucketStepEntry}; an already-initialised channel comes back
 * `existing_manifest`/`requires_choice` with a `warning`, and the step cannot
 * advance until the operator chooses, per channel, between:
 *
 *  - **adopt** — keep the existing bucket on that channel, or
 *  - **reinitialise** — discard it. Because reinitialise makes the channel's
 *    existing bucket data inaccessible, that path is gated behind the backend
 *    `warning` surfaced in a `role="alert"` region *and* an explicit
 *    confirmation checkbox the operator must tick before the step re-submits
 *    (Requirement 3.7).
 *
 * TASK BOUNDARY — the wizard shell (task 7.1)
 * -------------------------------------------
 * To avoid a cross-task dependency race, this component defines its own props
 * and local types here and imports no shared setup-types module. The shell
 * drives it through {@link BucketsStepProps}: it seeds the bucket→channel rows
 * (`initialAssignments`) so a return to this step shows the previous values
 * (Requirement 3.6), and is handed the committed rows on completion
 * (`onComplete`) so it can record them and advance to the Config step.
 * Everything else — the manifest/adopt/reinitialise handling and its
 * accessibility — is self-contained.
 *
 * Secret handling: this step carries no secrets (channel ids are not sensitive),
 * so nothing here is written to storage or logged.
 */
import { useCallback, useMemo, useState, type FormEvent } from "react";

import {
  useSubmitBuckets,
  type BucketAssignmentBody,
  type BucketStepEntry,
  type BucketsStepResult,
  type OnExisting,
} from "../api/setup-buckets";

/** One editable bucket→channel row in the step's local form state. */
export interface BucketRow {
  /** The bucket name to assign (S3 bucket name). */
  readonly bucketName: string;
  /** The Telegram channel id to back it with. */
  readonly channelId: number;
}

/** Props the wizard shell (task 7.1) adapts this step to. */
export interface BucketsStepProps {
  /**
   * The bucket→channel rows to seed the form with when the operator returns to
   * this step (Requirement 3.6, previous values). An empty or omitted list
   * renders a single blank row.
   */
  readonly initialAssignments?: readonly BucketRow[];
  /**
   * Called when the assignments are accepted by the backend (no outstanding
   * adopt/reinitialise choice). Receives the committed rows so the shell can
   * record them and advance to the Config step.
   */
  readonly onComplete: (assignments: readonly BucketRow[]) => void;
}

/** The adopt/reinitialise decision the operator has made for one channel. */
interface Choice {
  readonly onExisting: OnExisting;
  /** True once the operator has explicitly confirmed a reinitialise. */
  readonly confirmed: boolean;
}

/** A stable per-entry key: a bucket maps to exactly one channel in this step. */
function entryKey(bucketName: string, channelId: number): string {
  return `${bucketName}\u0000${channelId}`;
}

/** Normalise the seed rows into at least one editable row. */
function seedRows(initial: readonly BucketRow[] | undefined): BucketRow[] {
  if (initial && initial.length > 0) {
    return initial.map((row) => ({ ...row }));
  }
  return [{ bucketName: "", channelId: 0 }];
}

export function BucketsStep({ initialAssignments, onComplete }: BucketsStepProps): JSX.Element {
  const [rows, setRows] = useState<BucketRow[]>(() => seedRows(initialAssignments));
  // Entries the backend flagged as needing an adopt/reinitialise choice, keyed
  // by bucket+channel. Empty until the first submit reports `requires_choice`.
  const [pending, setPending] = useState<readonly BucketStepEntry[]>([]);
  // The operator's per-entry choice while resolving `pending`.
  const [choices, setChoices] = useState<Record<string, Choice>>({});

  const mutation = useSubmitBuckets();

  const updateRow = useCallback((index: number, patch: Partial<BucketRow>) => {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }, []);

  const addRow = useCallback(() => {
    setRows((current) => [...current, { bucketName: "", channelId: 0 }]);
  }, []);

  const removeRow = useCallback((index: number) => {
    setRows((current) => (current.length > 1 ? current.filter((_, i) => i !== index) : current));
  }, []);

  // The rows reduced to the request body. Blank bucket names are dropped so an
  // empty trailing row never blocks submission.
  const assignments = useMemo<BucketAssignmentBody[]>(
    () =>
      rows
        .filter((row) => row.bucketName.trim().length > 0)
        .map((row) => {
          const choice = choices[entryKey(row.bucketName, row.channelId)];
          return choice
            ? {
                bucket_name: row.bucketName,
                channel_id: row.channelId,
                on_existing: choice.onExisting,
              }
            : { bucket_name: row.bucketName, channel_id: row.channelId };
        }),
    [rows, choices],
  );

  // Whether every flagged entry now has a usable choice: adopt is always ready;
  // reinitialise is only ready once explicitly confirmed (Requirement 3.7).
  const allChoicesResolved = useMemo(
    () =>
      pending.every((entry) => {
        const choice = choices[entryKey(entry.bucket_name, entry.channel_id)];
        if (!choice) {
          return false;
        }
        return choice.onExisting === "adopt" || choice.confirmed;
      }),
    [pending, choices],
  );

  const setChoice = useCallback((entry: BucketStepEntry, onExisting: OnExisting) => {
    setChoices((current) => ({
      ...current,
      // Switching choice resets the reinitialise confirmation so the operator
      // must re-confirm after flipping back to reinitialise.
      [entryKey(entry.bucket_name, entry.channel_id)]: { onExisting, confirmed: false },
    }));
  }, []);

  const setConfirmed = useCallback((entry: BucketStepEntry, confirmed: boolean) => {
    setChoices((current) => {
      const key = entryKey(entry.bucket_name, entry.channel_id);
      const existing = current[key];
      if (!existing) {
        return current;
      }
      return { ...current, [key]: { ...existing, confirmed } };
    });
  }, []);

  const handleResult = useCallback(
    (result: BucketsStepResult, committed: readonly BucketRow[]) => {
      if (result.requires_choice) {
        // Keep only the entries that still need a decision on screen.
        setPending(result.buckets.filter((entry) => entry.requires_choice));
        return;
      }
      setPending([]);
      onComplete(committed);
    },
    [onComplete],
  );

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (mutation.isPending || assignments.length === 0) {
        return;
      }
      // Block a submit that would send a reinitialise still awaiting confirmation.
      if (pending.length > 0 && !allChoicesResolved) {
        return;
      }
      const committed = rows.filter((row) => row.bucketName.trim().length > 0);
      mutation.mutate({ assignments }, { onSuccess: (result) => handleResult(result, committed) });
    },
    [mutation, assignments, pending, allChoicesResolved, rows, handleResult],
  );

  const submitLabel = mutation.isPending
    ? "Submitting…"
    : pending.length > 0
      ? "Confirm choices"
      : "Assign buckets";
  const submitDisabled =
    mutation.isPending || assignments.length === 0 || (pending.length > 0 && !allChoicesResolved);

  return (
    <div className="setup-step-body setup-buckets" aria-labelledby="buckets-step-heading">
      <h2 className="setup-step-title" id="buckets-step-heading">
        Assign buckets to channels
      </h2>
      <p>
        Map each S3 bucket to the Telegram channel that stores its objects. A channel that already
        holds bucket data must be adopted or reinitialised before you can continue.
      </p>

      <form className="setup-form" onSubmit={onSubmit} noValidate>
        <ul className="buckets-rows">
          {rows.map((row, index) => {
            const nameId = `bucket-name-${index}`;
            const channelId = `bucket-channel-${index}`;
            return (
              <li className="buckets-row" key={index}>
                <div className="field">
                  <label className="field-label" htmlFor={nameId}>
                    Bucket name
                  </label>
                  <input
                    id={nameId}
                    className="field-control"
                    type="text"
                    value={row.bucketName}
                    autoComplete="off"
                    disabled={mutation.isPending}
                    onChange={(event) => updateRow(index, { bucketName: event.target.value })}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor={channelId}>
                    Channel id
                  </label>
                  <input
                    id={channelId}
                    className="field-control"
                    type="number"
                    value={Number.isNaN(row.channelId) ? "" : row.channelId}
                    disabled={mutation.isPending}
                    onChange={(event) =>
                      updateRow(index, { channelId: event.target.valueAsNumber })
                    }
                  />
                </div>
                <button
                  type="button"
                  className="btn buckets-remove"
                  onClick={() => removeRow(index)}
                  disabled={mutation.isPending || rows.length <= 1}
                  aria-label={`Remove bucket row ${index + 1}`}
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>

        <button
          type="button"
          className="btn buckets-add"
          onClick={addRow}
          disabled={mutation.isPending}
        >
          Add another bucket
        </button>

        {pending.length > 0 ? (
          <fieldset className="buckets-choices">
            <legend>Existing channels need a decision</legend>
            {pending.map((entry) => {
              const key = entryKey(entry.bucket_name, entry.channel_id);
              const choice = choices[key];
              const groupName = `on-existing-${key}`;
              const warningId = `warning-${key}`;
              const isReinit = choice?.onExisting === "reinitialise";
              return (
                <div className="buckets-choice" key={key}>
                  <p className="buckets-choice-title">
                    <strong>{entry.bucket_name}</strong> → channel {entry.channel_id}
                  </p>
                  <div
                    className="buckets-choice-options"
                    role="radiogroup"
                    aria-label={`Existing data on channel ${entry.channel_id} for bucket ${entry.bucket_name}`}
                  >
                    <label className="buckets-choice-option">
                      <input
                        type="radio"
                        name={groupName}
                        value="adopt"
                        checked={choice?.onExisting === "adopt"}
                        disabled={mutation.isPending}
                        onChange={() => setChoice(entry, "adopt")}
                      />
                      <span>Adopt the existing bucket (keep its data)</span>
                    </label>
                    <label className="buckets-choice-option">
                      <input
                        type="radio"
                        name={groupName}
                        value="reinitialise"
                        checked={isReinit}
                        disabled={mutation.isPending}
                        onChange={() => setChoice(entry, "reinitialise")}
                      />
                      <span>Reinitialise the channel (discard its data)</span>
                    </label>
                  </div>

                  {isReinit ? (
                    <div className="buckets-reinit-gate">
                      <p className="buckets-warning" id={warningId} role="alert">
                        {entry.warning ??
                          "Reinitialising this channel will make its existing bucket data inaccessible."}
                      </p>
                      <label className="buckets-confirm">
                        <input
                          type="checkbox"
                          checked={choice?.confirmed ?? false}
                          disabled={mutation.isPending}
                          aria-describedby={warningId}
                          onChange={(event) => setConfirmed(entry, event.target.checked)}
                        />
                        <span>
                          I understand the existing data on channel {entry.channel_id} becomes
                          inaccessible.
                        </span>
                      </label>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </fieldset>
        ) : null}

        {mutation.isError ? (
          <p className="buckets-error" role="alert">
            Could not assign buckets. {mutation.error.message}
          </p>
        ) : null}

        <div className="setup-step-actions">
          <button type="submit" className="btn btn--primary" disabled={submitDisabled}>
            {submitLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
