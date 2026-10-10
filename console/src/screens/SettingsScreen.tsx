/**
 * Settings screen (Requirements 4–7, 6.1).
 *
 * A Radix `Tabs` host — consistent with {@link RecoveryScreen}'s tabs — with
 * four panels:
 *
 *   - **Runtime Settings** — the operator-editable runtime-settings form
 *     (Requirement 4). Fetches the five settings from `GET /admin/settings`
 *     via {@link useSettings} and PUTs the whole set through
 *     {@link useUpdateSettings}; changes are effective within 5 s with no
 *     restart (Requirements 19.5–19.7). Preserved verbatim as the first panel.
 *   - **Access Keys** — list/create/delete access keys (Requirement 5).
 *   - **Configuration Visibility** — read-only environment settings, with
 *     secrets shown only as set/not set (Requirement 6).
 *   - **System Health** — readiness + dependency panel plus a metrics fetch
 *     control (Requirement 7).
 *
 * Each of the latter three panels is a dedicated component — `AccessKeysPanel`
 * (task 10.4), `ConfigVisibilityPanel` (task 10.6), and `SystemHealthPanel`
 * (task 10.7) — dropped into its matching `Tabs.Content` slot. Tab
 * accessibility (keyboard operation, visible focus,
 * ARIA roles) comes from Radix `Tabs` and the shared `.recovery-tab*` styling,
 * matching `RecoveryScreen` (Requirement 6.1).
 *
 * The data layer lives in `../api/settings` so this screen stays presentational.
 */
import * as Tabs from "@radix-ui/react-tabs";
import * as Toast from "@radix-ui/react-toast";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { describeAdminError, errorMessage } from "../api/errors";
import { useSettings, useUpdateSettings, type SettingsValues } from "../api/settings";
import { AccessKeysPanel } from "./settings/AccessKeysPanel";
import { ConfigVisibilityPanel } from "./settings/ConfigVisibilityPanel";
import { SystemHealthPanel } from "./settings/SystemHealthPanel";

/** The editable settings fields, in display order. */
const FIELD_KEYS = [
  "staging_max_size",
  "cache_max_size",
  "cache_half_life",
  "read_ahead",
  "upload_concurrency",
] as const;

type FieldKey = (typeof FIELD_KEYS)[number];

/** Form state: each field held as the raw input string while editing. */
type FormState = Record<FieldKey, string>;

/** Per-field descriptive metadata and the backend-mirrored valid range. */
interface FieldSpec {
  readonly key: FieldKey;
  readonly label: string;
  /** Inclusive minimum accepted by the backend `SettingsBody`. */
  readonly min: number;
  /** Inclusive maximum accepted by the backend, when bounded. */
  readonly max?: number;
  readonly hint: string;
}

/**
 * Field specs mirroring the backend `SettingsBody` ranges exactly: `read_ahead`
 * is 0–32 inclusive; every other field is an integer ≥ 1.
 */
const FIELD_SPECS: readonly FieldSpec[] = [
  {
    key: "staging_max_size",
    label: "Staging size (bytes)",
    min: 1,
    hint: "Staging capacity in bytes. Cannot be reduced below the bytes currently reserved by in-flight uploads.",
  },
  {
    key: "cache_max_size",
    label: "Cache size (bytes)",
    min: 1,
    hint: "On-disk record cache capacity in bytes.",
  },
  {
    key: "cache_half_life",
    label: "Cache half-life (seconds)",
    min: 1,
    hint: "Half-life for the cache ranking score, in seconds.",
  },
  {
    key: "read_ahead",
    label: "Read-ahead count",
    min: 0,
    max: 32,
    hint: "Number of records to prefetch ahead of a read (0–32).",
  },
  {
    key: "upload_concurrency",
    label: "Upload concurrency",
    min: 1,
    hint: "Number of concurrent upload workers.",
  },
];

/** A transient notification surfaced after a save attempt. */
interface Notice {
  readonly title: string;
  readonly description: string;
  readonly variant: "success" | "error";
}

/**
 * Build the operator-facing message for a failed save (Requirement 4.7).
 *
 * A staging-size reduction below the currently reserved bytes is rejected by
 * the backend with HTTP 409 and a `detail` of the form
 * `"staging_max_size cannot be reduced below the currently reserved N bytes;
 * minimum permitted value is N"` (see `gateway/app/admin/router.py`). The
 * shared {@link describeAdminError} already surfaces that `detail` verbatim, so
 * the minimum permitted value is present in the message. For a 409 we parse the
 * minimum out and lead with it explicitly so the operator sees the smallest
 * value they may set, then append the backend's full explanation.
 *
 * Any non-409 failure (422 range rejection, 503, network, …) is reported with
 * the shared message as-is. The caller must NOT present the change as applied
 * (no success notice, the form stays dirty) regardless of which branch runs.
 */
function saveErrorNotice(error: unknown): Notice {
  const info = describeAdminError(error);
  if (info.kind === "conflict") {
    const match = /minimum permitted value is (\d+)/i.exec(info.message);
    const description =
      match !== null
        ? `The smallest value you can set is ${match[1]}. ${info.message}`
        : info.message;
    return { title: "Save rejected", description, variant: "error" };
  }
  return { title: "Save failed", description: info.message, variant: "error" };
}

/**
 * The Admin API's status→message mapping — turning a 409 (plain-string
 * `detail`, e.g. staging below reserved) or a 422 (FastAPI validation-error
 * array, naming the field and its range) into an operator-readable string — is
 * centralised in {@link errorMessage} (`../api/errors`) so every screen reports
 * failures identically (Requirements 12.2, 12.4, 12.6). This panel re-exports
 * nothing; it simply calls the shared helper.
 */

/** Build the string-valued form state from a fetched settings snapshot. */
function toFormState(values: SettingsValues): FormState {
  return {
    staging_max_size: String(values.staging_max_size),
    cache_max_size: String(values.cache_max_size),
    cache_half_life: String(values.cache_half_life),
    read_ahead: String(values.read_ahead),
    upload_concurrency: String(values.upload_concurrency),
  };
}

/**
 * Validate one field's raw string against its spec, returning an error message
 * or `null` when valid. Values must be non-empty, whole numbers within range.
 */
function fieldError(spec: FieldSpec, raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === "") {
    return "Required.";
  }
  if (!/^-?\d+$/.test(trimmed)) {
    return "Must be a whole number.";
  }
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value)) {
    return "Number is too large.";
  }
  if (value < spec.min) {
    return `Must be at least ${spec.min}.`;
  }
  if (spec.max !== undefined && value > spec.max) {
    return `Must be at most ${spec.max}.`;
  }
  return null;
}

/**
 * Settings screen tab host (Requirement 6.1).
 *
 * Hosts four tab panels — Runtime Settings (the live form), Access Keys,
 * Configuration Visibility, and System Health — each a dedicated panel
 * component. The `Toast.Provider` wraps the whole screen so the Runtime
 * Settings save flow can surface success/error toasts regardless of which tab
 * is active.
 */
export function SettingsScreen(): JSX.Element {
  return (
    <Toast.Provider swipeDirection="right">
      <section className="screen">
        <header className="screen-header">
          <h1 className="screen-title">Settings</h1>
          <p className="screen-description">
            Runtime configuration, access keys, environment visibility, and system health.
          </p>
        </header>

        <Tabs.Root className="recovery-tabs" defaultValue="runtime">
          <Tabs.List className="recovery-tablist" aria-label="Settings views">
            <Tabs.Trigger className="recovery-tab" value="runtime">
              Runtime Settings
            </Tabs.Trigger>
            <Tabs.Trigger className="recovery-tab" value="keys">
              Access Keys
            </Tabs.Trigger>
            <Tabs.Trigger className="recovery-tab" value="config">
              Configuration Visibility
            </Tabs.Trigger>
            <Tabs.Trigger className="recovery-tab" value="health">
              System Health
            </Tabs.Trigger>
          </Tabs.List>

          <Tabs.Content value="runtime">
            <RuntimeSettingsPanel />
          </Tabs.Content>

          <Tabs.Content value="keys">
            <AccessKeysPanel />
          </Tabs.Content>

          <Tabs.Content value="config">
            <ConfigVisibilityPanel />
          </Tabs.Content>

          <Tabs.Content value="health">
            <SystemHealthPanel />
          </Tabs.Content>
        </Tabs.Root>
      </section>

      <Toast.Viewport className="toast-viewport" />
    </Toast.Provider>
  );
}

/**
 * Runtime Settings panel (Requirement 4; Requirements 19.5–19.7).
 *
 * Fetches the five operator-editable runtime settings from
 * `GET /admin/settings` via {@link useSettings} and populates an editable form:
 * staging size, cache size, cache half-life, read-ahead count, and upload
 * concurrency. On save it PUTs the whole set through {@link useUpdateSettings}
 * (which invalidates the settings query so the applied values re-render — the
 * change is effective within 5 s with no restart, Requirement 19.5).
 *
 * Client-side validation mirrors the backend `SettingsBody` ranges
 * (`gateway/app/admin/router.py`): `read_ahead` is 0–32 inclusive, every other
 * field is ≥ 1. Inline field errors block submission before a request is made.
 * The backend's cross-field rule — a staging-size reduction below the currently
 * reserved bytes is rejected with HTTP 409 (Requirements 19.6/19.7) — cannot be
 * checked client-side, so it (and any 422 range rejection) is surfaced as a
 * readable Radix toast, matching how the Jobs screen reports errors.
 */
function RuntimeSettingsPanel(): JSX.Element {
  const settings = useSettings();
  const update = useUpdateSettings();

  const [form, setForm] = useState<FormState | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // Remount the toast on an incrementing key so a repeated outcome re-opens it.
  const [noticeKey, setNoticeKey] = useState(0);

  // Seed/refresh the form whenever the fetched settings change (initial load
  // and after a successful save re-fetches the applied values).
  useEffect(() => {
    if (settings.data) {
      setForm(toFormState(settings.data));
    }
  }, [settings.data]);

  const showNotice = useCallback((next: Notice) => {
    setNotice(next);
    setNoticeKey((key) => key + 1);
  }, []);

  const errors = useMemo<Partial<Record<FieldKey, string>>>(() => {
    if (!form) {
      return {};
    }
    const result: Partial<Record<FieldKey, string>> = {};
    for (const spec of FIELD_SPECS) {
      const error = fieldError(spec, form[spec.key]);
      if (error) {
        result[spec.key] = error;
      }
    }
    return result;
  }, [form]);

  const hasErrors = Object.keys(errors).length > 0;

  // Dirty check compares the current (trimmed) form values against the fetched
  // snapshot so an unchanged form keeps Save disabled.
  const isDirty = useMemo(() => {
    if (!form || !settings.data) {
      return false;
    }
    return FIELD_KEYS.some((key) => form[key].trim() !== String(settings.data[key]));
  }, [form, settings.data]);

  const onFieldChange = useCallback((key: FieldKey, value: string) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  }, []);

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!form || hasErrors || !isDirty) {
        return;
      }
      const body: SettingsValues = {
        staging_max_size: Number(form.staging_max_size.trim()),
        cache_max_size: Number(form.cache_max_size.trim()),
        cache_half_life: Number(form.cache_half_life.trim()),
        read_ahead: Number(form.read_ahead.trim()),
        upload_concurrency: Number(form.upload_concurrency.trim()),
      };
      update.mutate(body, {
        onSuccess: () => {
          showNotice({
            title: "Settings saved",
            description: "The new runtime settings are now in effect.",
            variant: "success",
          });
        },
        onError: (error) => {
          showNotice(saveErrorNotice(error));
        },
      });
    },
    [form, hasErrors, isDirty, update, showNotice],
  );

  const onReset = useCallback(() => {
    if (settings.data) {
      setForm(toFormState(settings.data));
    }
  }, [settings.data]);

  return (
    <>
      <SettingsContent
        settings={settings}
        form={form}
        errors={errors}
        hasErrors={hasErrors}
        isDirty={isDirty}
        isSaving={update.isPending}
        onFieldChange={onFieldChange}
        onSubmit={onSubmit}
        onReset={onReset}
      />

      {notice ? (
        <Toast.Root
          key={noticeKey}
          className={`toast toast--${notice.variant}`}
          duration={6_000}
          onOpenChange={(open) => {
            if (!open) {
              setNotice(null);
            }
          }}
        >
          <Toast.Title className="toast-title">{notice.title}</Toast.Title>
          <Toast.Description className="toast-description">{notice.description}</Toast.Description>
          <Toast.Close className="toast-close" aria-label="Dismiss">
            ×
          </Toast.Close>
        </Toast.Root>
      ) : null}
    </>
  );
}

/**
 * The loading / error / form body of the Runtime Settings panel, split out so
 * the panel component only wires the form handlers and toast.
 */
function SettingsContent(props: {
  settings: ReturnType<typeof useSettings>;
  form: FormState | null;
  errors: Partial<Record<FieldKey, string>>;
  hasErrors: boolean;
  isDirty: boolean;
  isSaving: boolean;
  onFieldChange: (key: FieldKey, value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onReset: () => void;
}): JSX.Element {
  const { settings, form, errors, hasErrors, isDirty, isSaving, onFieldChange, onSubmit, onReset } =
    props;

  // Surface a load failure before the loading guard: on an error the query is
  // no longer pending but `form` is still null (nothing was fetched to seed
  // it), so checking `isError` first ensures the failure is announced via the
  // `role="alert"` region instead of showing the loading note indefinitely
  // (Requirement 10.6).
  if (settings.isError) {
    return (
      <div className="screen-error" role="alert">
        <p>Could not load settings: {errorMessage(settings.error)}</p>
        <button type="button" className="btn" onClick={() => void settings.refetch()}>
          Try again
        </button>
      </div>
    );
  }

  if (settings.isPending || !form) {
    return <p className="screen-note">Loading settings…</p>;
  }

  return (
    <form className="settings-form" onSubmit={onSubmit} noValidate>
      {/*
        Unsaved-changes affordance (Requirement 4.4): while the form differs
        from the last-displayed (applied) values, announce that unsaved changes
        are present via an `aria-live` status region, and point the operator at
        the two choices — submit (Save changes) or discard (Discard changes,
        which restores the applied values). The region is kept in the DOM so
        assistive tech picks up the transition; `hidden` toggles visibility.
      */}
      <p
        className="settings-dirty"
        role="status"
        aria-live="polite"
        hidden={!isDirty}
        data-testid="unsaved-indicator"
      >
        You have unsaved changes. Save them to apply, or discard to restore the last-saved values.
      </p>

      {FIELD_SPECS.map((spec) => {
        const error = errors[spec.key];
        const controlId = `settings-${spec.key}`;
        const hintId = `${controlId}-hint`;
        const errorId = `${controlId}-error`;
        return (
          <div className="settings-field" key={spec.key}>
            <label className="settings-label" htmlFor={controlId}>
              {spec.label}
            </label>
            <input
              id={controlId}
              className="settings-input"
              type="number"
              inputMode="numeric"
              step={1}
              min={spec.min}
              max={spec.max}
              value={form[spec.key]}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : hintId}
              disabled={isSaving}
              onChange={(event) => onFieldChange(spec.key, event.target.value)}
            />
            {error ? (
              <p className="settings-error" id={errorId} role="alert">
                {error}
              </p>
            ) : (
              <p className="settings-hint" id={hintId}>
                {spec.hint}
              </p>
            )}
          </div>
        );
      })}

      <div className="settings-actions">
        <button
          type="submit"
          className="btn btn--primary"
          disabled={isSaving || hasErrors || !isDirty}
        >
          {isSaving ? "Saving…" : "Save changes"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={isSaving || !isDirty}
          onClick={onReset}
          title="Discard unsaved changes and restore the last-saved values"
        >
          Reset
        </button>
      </div>
    </form>
  );
}
