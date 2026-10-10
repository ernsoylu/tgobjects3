/**
 * Telegram step of the first-run Setup Wizard (admin-dashboard Requirements
 * 2.1, 2.3, 2.4, 2.5, 2.7, 3.3; design: "Sequence: interactive Telegram login").
 *
 * This step drives the interactive Telegram login against
 * `POST /admin/setup/telegram` (via {@link useTelegramLogin}). It owns the
 * two-leg flow the backend's wizard session expects:
 *
 *  1. **Credentials (`creds`)** — the operator enters `api_id`, `api_hash`, and
 *     `login` (a phone number or bot token) and submits. The POST (no `code`)
 *     begins login; a `needs_code=true` result moves the step into the code
 *     prompt, while an `authenticated=true` result (e.g. a bot token) completes
 *     the step outright (Requirement 2.1).
 *  2. **Code prompt (`code`)** — the step prompts for the login/two-factor code
 *     and shows how many of the three attempts remain (`attempts_remaining`,
 *     Requirement 2.3). Each wrong code is counted by the backend and comes back
 *     `needs_code=true` with a decremented count, so the step keeps prompting.
 *     The third failure aborts with HTTP 401: the step surfaces the
 *     authentication error and returns to credential entry (Requirement 2.4).
 *
 * A Telegram-unavailable fault surfaces as HTTP 503 (`describeAdminError` →
 * `kind: "unavailable"`, `retryable: true`). The step shows the error, **keeps**
 * the entered `api_id`/`api_hash` in the form, and offers a retry rather than
 * discarding the credentials (Requirement 2.7). Any in-flight submit disables
 * the controls so a double submit cannot race the backend wizard session
 * (Requirement 2.3).
 *
 * TASK BOUNDARY — the shared step contract (task 7.1 shell)
 * ---------------------------------------------------------
 * This component keeps the shared {@link StepComponentProps}<{@link
 * TelegramStepState}> contract so {@link SetupWizard}'s wiring is unchanged. The
 * wizard retains the Telegram slice (`phase`, `apiId`, `apiHash`, `login`,
 * `attemptsRemaining`) and threads it back through `onPatch`, so returning to
 * this step restores what the operator typed (Requirement 3.6). `onComplete`
 * advances to the Buckets step when the login authenticates.
 *
 * Secret handling: `api_id`/`api_hash` and the `code` are runtime-entered and
 * sensitive. They are sent to the backend and held only in the retained
 * in-memory slice (never storage/cookies) and are never logged or echoed. The
 * `code` is deliberately *not* persisted via `onPatch` — it lives only in local
 * state for the duration of a single prompt and is cleared after each attempt.
 */
import { useCallback, useState, type FormEvent } from "react";

import { describeAdminError } from "../api/errors";
import { useTelegramLogin, type TelegramStepBody } from "../api/setup-telegram";
import type { StepComponentProps, TelegramStepState } from "./types";

/** Maximum two-factor attempts the backend allows (mirrors `MAX_TWO_FACTOR_ATTEMPTS`). */
const MAX_ATTEMPTS = 3;

/** Parse the entered `api_id` string into a positive integer, or `null`. */
function parseApiId(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) {
    return null;
  }
  const value = Number.parseInt(trimmed, 10);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export function TelegramStep({
  state,
  onPatch,
  onComplete,
  onBack,
}: StepComponentProps<TelegramStepState>): JSX.Element {
  const mutation = useTelegramLogin();

  // The code lives only in local state for the duration of a single prompt and
  // is never persisted to the retained slice (it is sensitive, single-use).
  const [code, setCode] = useState("");
  // An operator-readable error from the most recent failed submit, or null.
  const [error, setError] = useState<string | null>(null);

  const inCodePhase = state.phase === "code" || state.phase === "2fa";
  const busy = mutation.isPending;

  /**
   * Handle a terminal failure. A 503 (Telegram unavailable) stays in the
   * credential phase with the entered `api_id`/`api_hash` preserved and a retry
   * affordance (Requirement 2.7). A 401 (3 wrong codes) returns to credential
   * entry with the authentication error (Requirement 2.4). Anything else shows
   * the mapped message in place.
   */
  const handleError = useCallback(
    (err: unknown) => {
      const info = describeAdminError(err);
      setError(info.message);
      setCode("");
      // The 3rd wrong code aborts with 401 and the backend resets the login:
      // return to credential entry, preserving the typed credentials so the
      // operator can simply resubmit (Requirement 2.4).
      if (info.status === 401) {
        onPatch({ phase: "creds", code: "", attemptsRemaining: MAX_ATTEMPTS });
        return;
      }
      // 503 and every other fault keep the current phase; the credential fields
      // are already retained in the slice (Requirement 2.7).
    },
    [onPatch],
  );

  /**
   * Apply a successful step result. `authenticated` completes the step;
   * otherwise `needs_code` moves to the code prompt and records the remaining
   * attempts for display (Requirement 2.3).
   */
  const handleResult = useCallback(
    (result: { authenticated: boolean; needs_code: boolean; attempts_remaining: number }) => {
      setError(null);
      setCode("");
      if (result.authenticated) {
        onPatch({ phase: "done", code: "", attemptsRemaining: result.attempts_remaining });
        onComplete();
        return;
      }
      if (result.needs_code) {
        onPatch({ phase: "code", code: "", attemptsRemaining: result.attempts_remaining });
      }
    },
    [onPatch, onComplete],
  );

  const submit = useCallback(
    (body: TelegramStepBody) => {
      if (busy) {
        return;
      }
      mutation.mutate(body, { onSuccess: handleResult, onError: handleError });
    },
    [busy, mutation, handleResult, handleError],
  );

  /** Submit the credentials leg (begin login). */
  const onSubmitCredentials = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const apiId = parseApiId(state.apiId);
      if (apiId === null) {
        setError("Enter a valid API ID (a positive number).");
        return;
      }
      if (state.apiHash.trim().length === 0 || state.login.trim().length === 0) {
        setError("Enter the API hash and the phone number or bot token.");
        return;
      }
      submit({ api_id: apiId, api_hash: state.apiHash, login: state.login });
    },
    [state.apiId, state.apiHash, state.login, submit],
  );

  /** Submit the code leg (answer the two-factor prompt). */
  const onSubmitCode = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const apiId = parseApiId(state.apiId);
      if (apiId === null || code.trim().length === 0) {
        return;
      }
      submit({ api_id: apiId, api_hash: state.apiHash, login: state.login, code: code.trim() });
    },
    [state.apiId, state.apiHash, state.login, code, submit],
  );

  return (
    <div className="setup-step-body setup-telegram" aria-labelledby="telegram-step-heading">
      <h2 className="setup-step-title" id="telegram-step-heading">
        Connect Telegram
      </h2>

      {inCodePhase ? (
        <CodeForm
          code={code}
          onCodeChange={setCode}
          attemptsRemaining={state.attemptsRemaining}
          busy={busy}
          error={error}
          onSubmit={onSubmitCode}
          onBack={onBack}
        />
      ) : (
        <CredentialsForm
          state={state}
          onPatch={onPatch}
          busy={busy}
          error={error}
          onSubmit={onSubmitCredentials}
          onBack={onBack}
        />
      )}
    </div>
  );
}

interface CredentialsFormProps {
  readonly state: TelegramStepState;
  readonly onPatch: (patch: Partial<TelegramStepState>) => void;
  readonly busy: boolean;
  readonly error: string | null;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  readonly onBack: (() => void) | null;
}

/** The credential-entry leg: api_id / api_hash / login (Requirement 2.1). */
function CredentialsForm({
  state,
  onPatch,
  busy,
  error,
  onSubmit,
  onBack,
}: CredentialsFormProps): JSX.Element {
  return (
    <form className="setup-form" onSubmit={onSubmit} noValidate>
      <p>
        Enter the Telegram API credentials and the phone number or bot token to sign in. These are
        sent to the gateway to establish its single session and are never stored by the console.
      </p>

      <div className="field">
        <label className="field-label" htmlFor="telegram-api-id">
          API ID
        </label>
        <input
          id="telegram-api-id"
          className="field-control"
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={state.apiId}
          disabled={busy}
          onChange={(event) => onPatch({ apiId: event.target.value })}
        />
      </div>

      <div className="field">
        <label className="field-label" htmlFor="telegram-api-hash">
          API hash
        </label>
        <input
          id="telegram-api-hash"
          className="field-control"
          type="password"
          autoComplete="off"
          value={state.apiHash}
          disabled={busy}
          onChange={(event) => onPatch({ apiHash: event.target.value })}
        />
      </div>

      <div className="field">
        <label className="field-label" htmlFor="telegram-login">
          Phone number or bot token
        </label>
        <input
          id="telegram-login"
          className="field-control"
          type="text"
          autoComplete="off"
          value={state.login}
          disabled={busy}
          onChange={(event) => onPatch({ login: event.target.value })}
        />
      </div>

      {error !== null ? (
        <p className="setup-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="setup-step-actions">
        {onBack !== null ? (
          <button type="button" className="btn" onClick={onBack} disabled={busy}>
            Back
          </button>
        ) : null}
        <button type="submit" className="btn btn--primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </div>
    </form>
  );
}

interface CodeFormProps {
  readonly code: string;
  readonly onCodeChange: (value: string) => void;
  readonly attemptsRemaining: number;
  readonly busy: boolean;
  readonly error: string | null;
  readonly onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  readonly onBack: (() => void) | null;
}

/** The code-prompt leg: login/two-factor code + attempts remaining (Req 2.3). */
function CodeForm({
  code,
  onCodeChange,
  attemptsRemaining,
  busy,
  error,
  onSubmit,
  onBack,
}: CodeFormProps): JSX.Element {
  return (
    <form className="setup-form" onSubmit={onSubmit} noValidate>
      <p>
        Telegram sent a login code. Enter it below to finish signing in. You have{" "}
        <strong>{attemptsRemaining}</strong> {attemptsRemaining === 1 ? "attempt" : "attempts"}{" "}
        remaining.
      </p>

      <div className="field">
        <label className="field-label" htmlFor="telegram-code">
          Login code
        </label>
        <input
          id="telegram-code"
          className="field-control"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={code}
          disabled={busy}
          onChange={(event) => onCodeChange(event.target.value)}
        />
      </div>

      {error !== null ? (
        <p className="setup-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="setup-step-actions">
        {onBack !== null ? (
          <button type="button" className="btn" onClick={onBack} disabled={busy}>
            Back
          </button>
        ) : null}
        <button
          type="submit"
          className="btn btn--primary"
          disabled={busy || code.trim().length === 0}
        >
          {busy ? "Verifying…" : "Submit code"}
        </button>
      </div>
    </form>
  );
}
