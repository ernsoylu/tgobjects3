/**
 * Config step of the first-run Setup Wizard (admin-dashboard Requirements 3.2,
 * 3.4, 3.5, 11.1, 12.6).
 *
 * This step collects the gateway's storage configuration — the staging path and
 * its maximum size, the record-cache path and its maximum size, and the master
 * key — and submits it through `POST /admin/setup/config` (driven by
 * {@link useSubmitConfig}). It replaces the task-7.1 placeholder at this path
 * while keeping the shared {@link StepComponentProps}`<`{@link ConfigStepState}`>`
 * contract, so {@link SetupWizard} wiring is unchanged.
 *
 * Client-side range validation (Requirements 3.4/3.5, 12.6)
 * ---------------------------------------------------------
 * Every field is validated *before* the mutation is ever invoked, mirroring the
 * backend bounds in `gateway/app/admin/setup.py`:
 *
 *  - staging max size: {@link STAGING_MIN_SIZE} (1 MiB) – {@link STAGING_MAX_SIZE}
 *    (1 TiB), inclusive;
 *  - cache max size: {@link CACHE_MIN_SIZE} (1 MiB) – {@link CACHE_MAX_SIZE}
 *    (100 GiB), inclusive;
 *  - master key: at least {@link MASTER_KEY_MIN_LENGTH} (16) characters;
 *  - both paths: non-empty.
 *
 * When any field is out of range (or missing/non-numeric) the step stays put: it
 * names the offending field and its accepted range in an inline `role="alert"`
 * region tied to the control via `aria-describedby`/`aria-invalid`, and it does
 * **not** POST or advance (Requirements 3.5, 12.6). Only when every field is in
 * range does it POST and, on success, call `onComplete` to advance. The backend
 * ranges remain the authoritative backstop; a value that somehow slips past is
 * still rejected server-side and surfaced through the shared error mapping.
 *
 * Secret handling (Requirements 11.1/11.2)
 * ----------------------------------------
 * The master key lives only in the retained {@link ConfigStepState} and this
 * component's derived form state for the lifetime of the wizard. It is sent once
 * in the request body and is never logged, echoed back, written to any
 * persistent store, or placed in the Recovery Kit. The input uses
 * `type="password"` and `autoComplete="off"` so it is masked and not offered for
 * storage by the browser. Entered values are threaded back through `onPatch` so
 * returning to this step restores them (Requirement 3.6) — the key included,
 * since the wizard state is itself in-memory only.
 */
import { useCallback, useMemo, useState, type FormEvent } from "react";

import { useSubmitConfig, type ConfigStepBody } from "../api/setup-config";
import { errorMessage } from "../api/errors";
import { formatBytes } from "../lib/format";
import type { ConfigStepState, StepComponentProps } from "./types";

/** Lower bound for the staging volume max size, in bytes (1 MiB). */
export const STAGING_MIN_SIZE = 1 * 1024 ** 2;
/** Upper bound for the staging volume max size, in bytes (1 TiB). */
export const STAGING_MAX_SIZE = 1 * 1024 ** 4;
/** Lower bound for the record-cache max size, in bytes (1 MiB). */
export const CACHE_MIN_SIZE = 1 * 1024 ** 2;
/** Upper bound for the record-cache max size, in bytes (100 GiB). */
export const CACHE_MAX_SIZE = 100 * 1024 ** 3;
/** Minimum master-key length in characters. */
export const MASTER_KEY_MIN_LENGTH = 16;

/** The fields the step validates, used to key inline error regions. */
type FieldKey = "stagingPath" | "stagingMaxSize" | "cachePath" | "cacheMaxSize" | "masterKey";

/** Validation errors by field; a present entry blocks submission. */
type FieldErrors = Partial<Record<FieldKey, string>>;

/**
 * Parse a raw size string to a byte count, or `null` when it is not a
 * non-negative whole number. Values are entered as plain byte counts (the label
 * states the accepted range in human-readable units for guidance).
 */
function parseSize(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "" || !/^\d+$/.test(trimmed)) {
    return null;
  }
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

/**
 * Validate one size field against its inclusive [min, max] byte range. Returns a
 * message naming the accepted range (in both human-readable and byte terms) when
 * out of range or unparseable, else `null`.
 */
function sizeError(raw: string, min: number, max: number, noun: string): string | null {
  const value = parseSize(raw);
  if (value === null) {
    return `Enter the ${noun} as a whole number of bytes.`;
  }
  if (value < min || value > max) {
    return (
      `The ${noun} must be between ${formatBytes(min)} and ${formatBytes(max)} ` +
      `(${min}–${max} bytes).`
    );
  }
  return null;
}

/**
 * Compute all field errors for the current form values (Requirements 3.4/3.5).
 * An entry is present only for an invalid field; an empty object means the whole
 * form is valid and may be submitted.
 */
function validate(state: ConfigStepState): FieldErrors {
  const errors: FieldErrors = {};

  if (state.stagingPath.trim() === "") {
    errors.stagingPath = "Enter the staging directory path.";
  }
  const stagingSizeError = sizeError(
    state.stagingMaxSize,
    STAGING_MIN_SIZE,
    STAGING_MAX_SIZE,
    "staging maximum size",
  );
  if (stagingSizeError !== null) {
    errors.stagingMaxSize = stagingSizeError;
  }

  if (state.cachePath.trim() === "") {
    errors.cachePath = "Enter the cache directory path.";
  }
  const cacheSizeError = sizeError(
    state.cacheMaxSize,
    CACHE_MIN_SIZE,
    CACHE_MAX_SIZE,
    "cache maximum size",
  );
  if (cacheSizeError !== null) {
    errors.cacheMaxSize = cacheSizeError;
  }

  if (state.masterKey.length < MASTER_KEY_MIN_LENGTH) {
    errors.masterKey = `The master key must be at least ${MASTER_KEY_MIN_LENGTH} characters.`;
  }

  return errors;
}

export function ConfigStep(props: StepComponentProps<ConfigStepState>): JSX.Element {
  const { state, onPatch, onComplete, onBack } = props;
  const mutation = useSubmitConfig();

  // Field errors are shown only after a submit attempt, so the operator is not
  // scolded while still typing their first values. Each submit recomputes them.
  const [showErrors, setShowErrors] = useState(false);
  const errors = useMemo(() => validate(state), [state]);

  const patchField = useCallback(
    (patch: Partial<ConfigStepState>) => {
      onPatch(patch);
    },
    [onPatch],
  );

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (mutation.isPending) {
        return;
      }
      setShowErrors(true);
      // Any out-of-range/invalid field: stay on the step, surface the named
      // errors, and do not POST or advance (Requirements 3.5, 12.6).
      if (Object.keys(errors).length > 0) {
        return;
      }

      // Every field is in range: the parses below cannot fail here.
      const stagingMaxSize = parseSize(state.stagingMaxSize);
      const cacheMaxSize = parseSize(state.cacheMaxSize);
      if (stagingMaxSize === null || cacheMaxSize === null) {
        return;
      }

      const body: ConfigStepBody = {
        staging_path: state.stagingPath.trim(),
        staging_max_size: stagingMaxSize,
        cache_path: state.cachePath.trim(),
        cache_max_size: cacheMaxSize,
        master_key: state.masterKey,
      };
      mutation.mutate(body, { onSuccess: () => onComplete() });
    },
    [mutation, errors, state, onComplete],
  );

  const field = (key: FieldKey): string | undefined => (showErrors ? errors[key] : undefined);

  return (
    <div className="setup-step-body setup-config" aria-labelledby="config-step-heading">
      <h2 className="setup-step-title" id="config-step-heading">
        Storage configuration
      </h2>
      <p>
        Choose where the gateway stages uploads and caches records, and set the master key that
        wraps every per-bucket key. The master key is never stored by this console or placed in the
        Recovery Kit — keep it somewhere safe.
      </p>

      <form className="setup-form" onSubmit={onSubmit} noValidate>
        <PathAndSizeField
          idPrefix="staging"
          pathLabel="Staging directory path"
          sizeLabel="Staging maximum size (bytes)"
          sizeHint={`Between ${formatBytes(STAGING_MIN_SIZE)} and ${formatBytes(STAGING_MAX_SIZE)}.`}
          pathValue={state.stagingPath}
          sizeValue={state.stagingMaxSize}
          pathError={field("stagingPath")}
          sizeError={field("stagingMaxSize")}
          disabled={mutation.isPending}
          onPathChange={(value) => patchField({ stagingPath: value })}
          onSizeChange={(value) => patchField({ stagingMaxSize: value })}
        />

        <PathAndSizeField
          idPrefix="cache"
          pathLabel="Cache directory path"
          sizeLabel="Cache maximum size (bytes)"
          sizeHint={`Between ${formatBytes(CACHE_MIN_SIZE)} and ${formatBytes(CACHE_MAX_SIZE)}.`}
          pathValue={state.cachePath}
          sizeValue={state.cacheMaxSize}
          pathError={field("cachePath")}
          sizeError={field("cacheMaxSize")}
          disabled={mutation.isPending}
          onPathChange={(value) => patchField({ cachePath: value })}
          onSizeChange={(value) => patchField({ cacheMaxSize: value })}
        />

        <div className="field">
          <label className="field-label" htmlFor="config-master-key">
            Master key
          </label>
          <input
            id="config-master-key"
            className="field-control"
            type="password"
            autoComplete="off"
            value={state.masterKey}
            disabled={mutation.isPending}
            aria-invalid={field("masterKey") ? true : undefined}
            aria-describedby={
              field("masterKey") ? "config-master-key-error" : "config-master-key-hint"
            }
            onChange={(event) => patchField({ masterKey: event.target.value })}
          />
          {field("masterKey") ? (
            <p className="field-error" id="config-master-key-error" role="alert">
              {field("masterKey")}
            </p>
          ) : (
            <p className="field-hint" id="config-master-key-hint">
              At least {MASTER_KEY_MIN_LENGTH} characters.
            </p>
          )}
        </div>

        {mutation.isError ? (
          <p className="setup-error" role="alert">
            Could not save the configuration. {errorMessage(mutation.error)}
          </p>
        ) : null}

        <div className="setup-step-actions">
          {onBack ? (
            <button type="button" className="btn" onClick={onBack} disabled={mutation.isPending}>
              Back
            </button>
          ) : null}
          <button type="submit" className="btn btn--primary" disabled={mutation.isPending}>
            {mutation.isPending ? "Saving…" : "Save configuration"}
          </button>
        </div>
      </form>
    </div>
  );
}

/** Props for one path + max-size field pair. */
interface PathAndSizeFieldProps {
  readonly idPrefix: string;
  readonly pathLabel: string;
  readonly sizeLabel: string;
  readonly sizeHint: string;
  readonly pathValue: string;
  readonly sizeValue: string;
  readonly pathError: string | undefined;
  readonly sizeError: string | undefined;
  readonly disabled: boolean;
  readonly onPathChange: (value: string) => void;
  readonly onSizeChange: (value: string) => void;
}

/**
 * A labelled path input paired with its max-size input, each with an inline
 * `role="alert"` error region tied to the control via `aria-describedby` so the
 * offending field and its accepted range are announced (Requirements 10, 12.6).
 */
function PathAndSizeField(props: PathAndSizeFieldProps): JSX.Element {
  const pathId = `config-${props.idPrefix}-path`;
  const sizeId = `config-${props.idPrefix}-size`;
  const pathErrorId = `${pathId}-error`;
  const sizeErrorId = `${sizeId}-error`;
  const sizeHintId = `${sizeId}-hint`;
  return (
    <div className="setup-config-group">
      <div className="field">
        <label className="field-label" htmlFor={pathId}>
          {props.pathLabel}
        </label>
        <input
          id={pathId}
          className="field-control"
          type="text"
          autoComplete="off"
          value={props.pathValue}
          disabled={props.disabled}
          aria-invalid={props.pathError ? true : undefined}
          aria-describedby={props.pathError ? pathErrorId : undefined}
          onChange={(event) => props.onPathChange(event.target.value)}
        />
        {props.pathError ? (
          <p className="field-error" id={pathErrorId} role="alert">
            {props.pathError}
          </p>
        ) : null}
      </div>
      <div className="field">
        <label className="field-label" htmlFor={sizeId}>
          {props.sizeLabel}
        </label>
        <input
          id={sizeId}
          className="field-control"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={props.sizeValue}
          disabled={props.disabled}
          aria-invalid={props.sizeError ? true : undefined}
          aria-describedby={props.sizeError ? sizeErrorId : sizeHintId}
          onChange={(event) => props.onSizeChange(event.target.value)}
        />
        {props.sizeError ? (
          <p className="field-error" id={sizeErrorId} role="alert">
            {props.sizeError}
          </p>
        ) : (
          <p className="field-hint" id={sizeHintId}>
            {props.sizeHint}
          </p>
        )}
      </div>
    </div>
  );
}
