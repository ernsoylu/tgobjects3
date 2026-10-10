/**
 * Telegram setup-step data hook (admin-dashboard Requirements 2.1–2.5, 2.7, 3.3).
 *
 * The Telegram step of the first-run Setup Wizard drives the interactive login
 * through `POST /admin/setup/telegram` (design: "Sequence: interactive Telegram
 * login"). The payload shapes mirror the backend models in
 * `gateway/app/admin/setup.py` exactly (snake_case JSON): `TelegramStepBody`
 * carries the operator-entered `api_id`/`api_hash`/`login`, with `code` set only
 * on the follow-up POSTs that answer a two-factor prompt; `TelegramStepResult`
 * reports whether the session `authenticated`, whether Telegram still
 * `needs_code`, and how many of the three code attempts remain
 * (`attempts_remaining`).
 *
 * The backend owns the attempt counting and the 3-attempt abort: the first POST
 * begins login and a `needs_code=true` result asks the Console to prompt for a
 * code; each wrong code consumes one of the three attempts and comes back
 * `needs_code=true` with a decremented `attempts_remaining`; the third failure
 * aborts with HTTP 401 and resets the login so the operator re-enters
 * credentials (Requirement 2.4). A Telegram-unavailable fault surfaces as HTTP
 * 503, which the step renders while preserving `api_id`/`api_hash` and offering
 * retry (Requirement 2.7). This hook carries only the request/response types and
 * the mutation; the loop/abort/preserve behaviour lives in {@link TelegramStep}.
 *
 * Secret handling: `api_id`/`api_hash` and `code` are entered at runtime and are
 * sensitive — they are sent to the backend but never persisted to storage or
 * logged (design: "Admin API payloads" table; `api_hash`/`code` sent, never
 * persisted client-side; preserved in the form only across a retry). The shared
 * `adminClient` attaches `Authorization` and, on this unsafe POST, the cached
 * `X-CSRF-Token`, so this hook carries no auth/CSRF logic itself. The endpoint
 * drives the backend's in-process wizard session, so there is no query cache to
 * invalidate here.
 */
import { useMutation, type UseMutationResult } from "@tanstack/react-query";

import { adminClient, SUPPRESS_UNAUTHORIZED_HANDLER } from "./client";

/**
 * `POST /admin/setup/telegram` body. Mirrors the backend `TelegramStepBody`:
 * `api_id` is a positive integer, `api_hash` and `login` (a phone number or bot
 * token) are non-empty, and `code` is set only on the follow-up POSTs that
 * answer a two-factor prompt.
 */
export interface TelegramStepBody {
  readonly api_id: number;
  readonly api_hash: string;
  readonly login: string;
  readonly code?: string | null;
}

/**
 * `POST /admin/setup/telegram` response. Mirrors the backend
 * `TelegramStepResult`: `authenticated` signals the login step is complete;
 * `needs_code` asks the Console to prompt for a code and re-POST; and
 * `attempts_remaining` is how many of the three code attempts are left
 * (Requirements 2.3, 2.4).
 */
export interface TelegramStepResult {
  readonly authenticated: boolean;
  readonly needs_code: boolean;
  readonly attempts_remaining: number;
}

/** Path of the Telegram step, relative to the Admin API base URL. */
export const TELEGRAM_STEP_ENDPOINT = "/setup/telegram";

async function postTelegram(body: TelegramStepBody): Promise<TelegramStepResult> {
  // The 3rd wrong code aborts with HTTP 401 as a domain signal (the step returns
  // to credential entry, Requirement 2.4), not an expired session — so this
  // request opts out of the client's global 401 session-end hook.
  const response = await adminClient.post<TelegramStepResult>(TELEGRAM_STEP_ENDPOINT, body, {
    [SUPPRESS_UNAUTHORIZED_HANDLER]: true,
  });
  return response.data;
}

/**
 * Drive one Telegram login step (Requirements 2.1–2.4, 2.7).
 *
 * The same mutation serves both legs of the flow: the credentials POST (no
 * `code`) that begins login, and the follow-up POSTs that submit a two-factor
 * `code`. The step inspects the result's `authenticated`/`needs_code` to decide
 * whether to advance, keep prompting, or (on a 401/503 error) return to
 * credential entry while preserving what the operator typed.
 */
export function useTelegramLogin(): UseMutationResult<TelegramStepResult, Error, TelegramStepBody> {
  return useMutation({
    mutationFn: postTelegram,
  });
}
