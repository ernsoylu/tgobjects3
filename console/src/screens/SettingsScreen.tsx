/**
 * Settings screen (Requirements 19.5–19.7).
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
 *
 * The data layer lives in `../api/settings` so this screen stays presentational.
 */
import * as Toast from "@radix-ui/react-toast";
import axios from "axios";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { useSettings, useUpdateSettings, type SettingsValues } from "../api/settings";

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
 * Turn an unknown thrown value into an operator-readable message. The Admin API
 * reports failures as JSON `{ "detail": ... }`; a 409 (staging below reserved)
 * carries a plain-string detail, while a 422 (range) carries FastAPI's
 * validation-error array. Fall back to the axios/Error message otherwise.
 */
function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          const msg = (item as { msg?: unknown }).msg;
          const loc = (item as { loc?: unknown }).loc;
          const field = Array.isArray(loc) ? loc[loc.length - 1] : undefined;
          const text = typeof msg === "string" ? msg : "invalid value";
          return typeof field === "string" ? `${field}: ${text}` : text;
        })
        .filter((text) => text.length > 0);
      if (messages.length > 0) {
        return messages.join("; ");
      }
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Unknown error";
}

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

export function SettingsScreen(): JSX.Element {
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
          showNotice({
            title: "Save failed",
            description: errorMessage(error),
            variant: "error",
          });
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
    <Toast.Provider swipeDirection="right">
      <section className="screen">
        <header className="screen-header">
          <h1 className="screen-title">Settings</h1>
          <p className="screen-description">
            Runtime configuration. Changes take effect within a few seconds without restarting the
            gateway.
          </p>
        </header>

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
      </section>

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
      <Toast.Viewport className="toast-viewport" />
    </Toast.Provider>
  );
}

/**
 * The loading / error / form body of the screen, split out so the outer
 * component only wires the toast provider and submit handler.
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

  if (settings.isPending || !form) {
    return <p className="screen-note">Loading settings…</p>;
  }

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

  return (
    <form className="settings-form" onSubmit={onSubmit} noValidate>
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
        <button type="button" className="btn" disabled={isSaving || !isDirty} onClick={onReset}>
          Reset
        </button>
      </div>
    </form>
  );
}
