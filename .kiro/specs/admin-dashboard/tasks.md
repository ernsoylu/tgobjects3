# Implementation Plan: Admin Dashboard

## Overview

This plan turns the tgobjects3 Console stub into a working, deployable operator
dashboard by closing the concrete gaps in `design.md`. It is ordered by
dependency across the four tracks:

1. **Backend** — the Telethon-backed `TelethonLoginAdapter` behind the
   `TelegramLoginPort`, plus the one-line `main.py::_build_setup_router` wiring
   that makes `POST /admin/setup/telegram` functional. This unblocks the whole
   Telegram setup flow and ships first.
2. **Frontend** — reworked `console/src/api/client.ts` + `AuthProvider` (Basic
   auth + in-memory `X-CSRF-Token`), which unblocks every authenticated screen,
   then the `LoginScreen`, the `SetupWizard` steps, and the expanded settings
   area (runtime settings already exist — extend, do not rewrite).
3. **Deploy** — the missing `deploy/Dockerfile.console` + `deploy/nginx.console.conf`
   and the proxy/SPA-fallback test.
4. **Design** — the evergreen-dark design-system tokens in
   `console/src/styles.css`, the Playwright e2e suite (mocking the Admin API at
   the network layer per the existing console convention), and the Figma capture
   delivery step.

Backend is Python 3.12 async (ruff + mypy strict, pytest + hypothesis); frontend
is React 18 + TypeScript strict + Vite + React Query + Radix + axios (eslint +
prettier, Playwright e2e). Property-based tests cover the design's Correctness
Properties 1–9; concrete scenarios and wiring use example/integration/e2e tests.

## Tasks

- [x] 1. Backend: interactive Telegram login adapter
  - [x] 1.1 Implement `TelethonLoginAdapter` in `gateway/app/telegram/login_adapter.py`
    - Create the single new backend **module** (not a package) implementing the
      `TelegramLoginPort` Protocol from `gateway/app/admin/setup.py`
      (`start_login`, `submit_code`, `reset`). This is the only place besides
      `telegram/adapter.py` allowed to import Telethon.
    - Hold one in-memory `TelegramClient` (`StringSession`, nothing on disk)
      across `start_login` → `submit_code`; bot-token path signs in directly and
      returns `authenticated=True`; phone path sends the code request, stores
      `phone_code_hash`, returns `needs_code=True`; `submit_code` handles the
      code and `SessionPasswordNeededError` 2FA leg, serialises
      `StringSession.save(...)` into `session_credentials` on success.
    - Map every Telethon fault onto typed errors per the design's error table:
      invalid/expired code / bad 2FA password → `TwoFactorFailedError`;
      `FloodWaitError` → `gateway.app.telegram.errors.FloodWaitError`; all other
      RPC/transport/OS faults → `TelegramUnavailableError`. No Telethon exception
      may leak past this class.
    - `reset()` disconnects and discards the in-progress client; idempotent,
      swallows disconnect errors.
    - Start the module with `from __future__ import annotations`; full type
      annotations (mypy strict), ruff clean. Because this adds a module to the
      already-listed `gateway/app/telegram` package, **do NOT** change
      `[tool.setuptools].packages` in `pyproject.toml`.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7_

  - [x] 1.2 Write property test for login-adapter error mapping
    - `tests/unit/telegram/test_login_adapter.py`, driving the adapter with a
      fake Telethon client that scripts arbitrary fault sequences (invalid code,
      expired code, flood wait, RPC error, transport/OS error).
    - **Property 1: No Telethon exception escapes the login adapter** (≥ 100
      hypothesis iterations over fault sequences — assert only
      `TwoFactorFailedError` or a `gateway.app.telegram.errors.TelegramError`
      subclass is raised).
    - **Validates: Requirements 2.7**

  - [x] 1.3 Write example tests for login-adapter happy paths
    - In `tests/unit/telegram/test_login_adapter.py`: bot-token direct auth and
      phone + code + 2FA, asserting non-empty `session_credentials` on success.
    - Supports **Property 4** (session credentials produced on success).
    - _Requirements: 2.2, 2.6_

- [x] 2. Backend: wire the login factory into the setup router
  - [x] 2.1 Inject `TelegramLoginFactory` in `gateway/app/main.py::_build_setup_router`
    - Add a `login_factory(settings) -> TelegramLoginPort` closure that defers
      the `from gateway.app.telegram.login_adapter import TelethonLoginAdapter`
      import (so a deployment without Telethon still imports `main`), and pass it
      to `create_setup_router(context=..., login_factory=...)`.
    - Result: `POST /admin/setup/telegram` reaches the adapter instead of
      returning HTTP 503. No new auth surface, no package list change.
    - _Requirements: 2.1, 2.2_

  - [x] 2.2 Write property test for the 3-attempt abort invariant
    - `tests/unit/admin/test_setup_two_factor.py`, using a fake
      `TelegramLoginPort` whose `submit_code` fails a configurable number of
      times.
    - **Property 2: Wrong-code attempts abort after exactly 3 failures with a
      single `reset()`** — keep prompting while failures < 3; on the 3rd failure
      abort with 401, call `reset()` exactly once, clear login state so a bare
      `submit_code` without a fresh `start_login` is rejected.
    - **Validates: Requirements 2.4, 2.5**

  - [x] 2.3 Write integration tests for the setup router login flow and wiring
    - `tests/integration/admin/test_setup_router_login.py` — mount the real
      setup router with a fake login port scripting needs_code → success and
      needs_code → repeated failure through the step POSTs.
    - `tests/integration/test_main_wiring.py` — assert `_build_setup_router`
      injects a `login_factory` so `POST /admin/setup/telegram` no longer returns
      503 (fake adapter via the factory seam).
    - _Requirements: 2.2, 2.4, 2.5, 2.6_

- [x] 3. Checkpoint - backend Telegram login path
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Frontend: rework the Admin API client and auth context
  - [x] 4.1 Rework `console/src/api/client.ts` to Basic auth + in-memory CSRF
    - Drop `withCredentials`, the `tgo_csrf` cookie read, and cookie constants.
    - Add in-memory `basicAuth` (fixed username `admin`) and `csrfToken` with
      `setCredentials(password)` / `clearCredentials()`; a request interceptor
      that always attaches `Authorization` and attaches `X-CSRF-Token` on unsafe
      methods (not get/head/options); a response interceptor that caches the
      `X-CSRF-Token` response header; a global 401 interceptor hook and a
      30000 ms timeout. S3/SigV4 untouched — this client talks only to Admin API.
    - TypeScript strict, eslint + prettier clean.
    - _Requirements: 1.5, 11.4, 11.5, 12.1, 12.3_

  - [x] 4.2 Implement `AuthProvider` in `console/src/auth/AuthProvider.tsx`
    - Hold `AuthState` (status + `endedReason`) in React memory only (never
      storage/cookies). `signIn(password)` sets Basic creds and issues the
      bootstrap `GET /admin/buckets` (reuses an existing authenticated read, no
      new endpoint); success caches the CSRF token via the interceptor and flips
      to `authenticated`; a 401 rejects and stays `unauthenticated`. `signOut`
      clears creds + CSRF; wire the global 401 interceptor to `signOut("expired")`.
    - _Requirements: 1.2, 1.5, 1.6, 1.8, 11.4_

  - [x] 4.3 Write property/unit test for the CSRF-on-mutations invariant
    - Vitest + Testing Library over `client.ts`/`AuthProvider`: bootstrap caches
      the token; unsafe methods attach it; a mutating request before bootstrap
      (or with a mismatched token) is rejected and applies no change; 401
      triggers `signOut("expired")`.
    - **Property 3: A mutating request without the bootstrapped CSRF token is
      rejected and applies no change.**
    - **Validates: Requirements 1.5, 11.5, 11.6**

  - [x] 4.4 Write backend integration test for CSRF enforcement
    - `tests/integration/admin/test_csrf.py` — mutating request before bootstrap
      → 403 and unchanged resource; after bootstrap → success. Backend half of
      **Property 3**.
    - **Validates: Requirements 11.6**

- [x] 5. Frontend: login screen and routing guards
  - [x] 5.1 Implement `LoginScreen` in `console/src/screens/LoginScreen.tsx`
    - Single password field, submit button, inline `role="alert"` error region,
      in-progress indicator that disables the button to block double submit. On
      rejection keep the (fixed) identifier and clear the password; on a
      session-ended redirect show the "session no longer valid" message from
      `endedReason`.
    - _Requirements: 1.1, 1.3, 1.4, 1.7, 1.8_

  - [x] 5.2 Add auth/setup routing guards and logout in `console/src/App.tsx`
    - Wrap the router in `AuthProvider`; add `/login` and `/setup` routes guarded
      by `RequireAuth` and `RequireSetup`; `RequireSetup` queries
      `POST /admin/setup/status` and redirects guarded routes to `/setup` while
      `setup_required` is true. Add a logout control to the existing sidebar;
      reuse `AppLayout` and existing screens unchanged otherwise. Redirect to
      login on 401.
    - _Requirements: 1.1, 1.6, 1.7, 3.1, 3.12, 12.1, 12.5_

  - [x] 5.3 Write property/e2e test for setup-required routing
    - **Property 9: Setup-required state routes into the wizard** — while
      `setup_required = true` any navigation lands on the wizard; while `false`,
      authenticated navigation reaches the admin screens.
    - **Validates: Requirements 3.1, 3.12, 8.6, 12.5**

- [x] 6. Checkpoint - auth and routing
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Frontend: setup wizard shell and Telegram step
  - [x] 7.1 Build the wizard shell in `console/src/setup/`
    - `SetupWizard` with the `WizardState` step/state model
      (`telegram | buckets | config | finalize`, plus `furthestReached`), a
      `StepIndicator` showing current/completed steps, and the four ordered steps
      in sequence. Allow returning to a completed earlier step with its previous
      values and forward again up to `furthestReached`.
    - _Requirements: 3.2, 3.3, 3.6_

  - [x] 7.2 Implement the Telegram step with the needs_code / 2FA loop
    - `console/src/setup/TelegramStep.tsx` driven by a React Query mutation
      against `POST /admin/setup/telegram`. Collect api_id/api_hash/login at
      runtime (never stored in repo); own the code/2FA prompt loop and
      `attempts_remaining` display (up to 3); on the 3rd failure show the auth
      error and return to credential entry; on Telegram-unavailable (503) show
      the error, preserve api_id/api_hash, and offer retry; indicate in-progress
      and block re-submit.
    - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.7, 3.3_

  - [x] 7.3 Write component tests for the Telegram step
    - needs_code → code → success, 3-wrong-code abort/return, and
      Telegram-unavailable credential preservation + retry.
    - _Requirements: 2.4, 2.7_

- [x] 8. Frontend: buckets, config, and finalize steps
  - [x] 8.1 Implement the Buckets step in `console/src/setup/BucketsStep.tsx`
    - Bucket→channel assignment against `POST /admin/setup/buckets`; when a
      channel already has manifest messages, offer adopt vs reinitialise, and
      gate reinitialise behind a warning + explicit confirmation that existing
      data becomes inaccessible.
    - _Requirements: 3.2, 3.7_

  - [x] 8.2 Implement the Config step in `console/src/setup/ConfigStep.tsx`
    - Collect staging path + max size (1 MiB–1 TiB), cache path + max size
      (1 MiB–100 GiB), and master key (≥ 16 chars); validate each range
      client-side before `POST /admin/setup/config`; on an out-of-range value
      stay on the step, name the offending field and its accepted range, and do
      not advance. Master key lives only in component state, never logged/echoed
      or placed in the Recovery Kit.
    - _Requirements: 3.2, 3.4, 3.5, 11.1, 12.6_

  - [x] 8.3 Write property test for config range validation
    - **Property 5: Config range validation blocks out-of-range submits** —
      out-of-range field reported with its range and no advance; all-in-range
      advances.
    - **Validates: Requirements 3.4, 3.5, 12.6**

  - [x] 8.4 Implement the Finalize step in `console/src/setup/FinalizeStep.tsx`
    - Mutation against `POST /admin/setup/finalize`; disable resubmission while
      in flight; render the Recovery Kit as a downloadable bundle (verifier, not
      the master key) and the first access-key secret exactly once, clearing the
      secret on navigation away; on failure stay on the step with a readable
      error and allow retry; on success leave the wizard for the admin screens.
    - _Requirements: 3.8, 3.9, 3.10, 3.11, 3.12, 11.2, 11.3_

  - [x] 8.5 Write property test for session-credentials → Recovery Kit
    - **Property 4: Successful code submission yields session credentials carried
      into the Recovery Kit** — `recovery_kit.telegram_session` equals the login
      `session_credentials`; `recovery_kit.master_key_verifier` never equals the
      submitted master key.
    - **Validates: Requirements 2.6, 11.2**

- [x] 9. Checkpoint - setup wizard end to end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Frontend: expanded settings area
  - [x] 10.1 Convert `console/src/screens/SettingsScreen.tsx` to a Radix Tabs host
    - Four panels (Runtime Settings, Access Keys, Configuration Visibility,
      System Health), consistent with `RecoveryScreen`'s tabs. Keep the existing
      runtime-settings form as the first panel — extend, do not rewrite.
    - _Requirements: 6.1_

  - [x] 10.2 Extend runtime-settings validation (unsaved-changes + ranges)
    - Using the existing `api/settings.ts` form: display all five
      Runtime_Settings; validate each (`read_ahead` 0–32 inclusive, others ≥ 1)
      before submit; show unsaved-changes with submit/discard; indicate in-flight
      and block double submit; re-display applied values and clear the indicator
      on success; on 409 (staging below reserved) show the minimum permitted
      value and do not present as applied.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 10.3 Write property test for runtime-settings range validation
    - **Property 6: Runtime-settings validation mirrors the backend ranges** —
      submit iff every field in range; out-of-range field reported with its
      range and not submitted.
    - **Validates: Requirements 4.2, 4.3**

  - [x] 10.4 Implement the Access Keys panel with `console/src/api/keys.ts`
    - List existing keys by id + label with an empty state and no secrets;
      create with optional label showing the new secret exactly once with a copy
      action, cleared on dismiss/navigate-away; delete behind an explicit confirm
      dialog, removing the key from the list; on a delete of an absent key show a
      readable error and refresh the list.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 11.3_

  - [x] 10.5 Write property test for one-time access-key secret
    - **Property 7: An access-key secret is shown exactly once and is absent after
      dismissal** — secret present in the DOM only while the creation result is
      shown; absent (and absent from any persistent store) after dismiss.
    - **Validates: Requirements 5.3, 11.3**

  - [x] 10.6 Implement the Configuration Visibility panel with `console/src/api/config.ts`
    - Show non-secret Environment_Settings as read-only, each labelled as
      requiring redeploy/restart, with no editing control; show secret env values
      (e.g. Master_Key) as "set / not set" only, never revealing the value.
    - _Requirements: 6.2, 6.3, 6.4, 11.1_

  - [x] 10.7 Implement the System Health panel with `console/src/api/health.ts`
    - Poll `GET /health/ready` at least every 10 s while displayed; derive
      readiness from HTTP status (200 ready, 503 not ready, network/timeout
      unknown); show PostgreSQL + Telegram dependency health; show
      readiness pending while in Setup_Required_State; add a control that fetches
      and presents `/metrics`.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [x] 10.8 Write component tests for health status derivation
    - 200 → ready/healthy, 503 (postgres vs telegram) → not ready + which
      dependency, network/timeout → unknown.
    - _Requirements: 7.2, 7.3, 7.4, 7.5_

- [x] 11. Checkpoint - settings area
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Frontend: wire error handling and recovery routing
  - [x] 12.1 Add the status → behaviour mapping across the API/query layer
    - Centralise the Admin API status handling: 401 → end session + redirect to
      login; 403 (CSRF) → re-bootstrap the token and retry once, then surface a
      readable error; 409/422/503 → readable message from the response, stay on
      screen; other 5xx → "unexpected gateway error", not presented as success;
      network error / > 30 s → "gateway could not be reached" with retry;
      validation failures name the field and its accepted range.
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.6, 11.6_

- [x] 13. Deploy: console image and reverse proxy
  - [x] 13.1 Add `deploy/Dockerfile.console` (multi-stage build → nginx)
    - Stage 1 `node:20-bookworm-slim`: `npm ci` then `npm run build`
      (`tsc --noEmit && vite build`) so a build failure aborts the image and no
      partial bundle is served. Stage 2 `nginx:1.27-bookworm`: copy
      `deploy/nginx.console.conf` and the built `dist`, add a `HEALTHCHECK`
      hitting `/healthz`, expose 80. Build context is the repo root (matches the
      existing compose `context: ..`); the compose `console` service already
      references this Dockerfile, so no compose change is required.
    - _Requirements: 8.1, 8.2, 8.4_

  - [x] 13.2 Add `deploy/nginx.console.conf`
    - Serve the static bundle; `location = /healthz` returns 200; proxy
      `/admin`, `/health`, `/metrics` to `http://gateway:8080`; SPA fallback
      `try_files $uri $uri/ /index.html` for all other paths.
    - _Requirements: 8.3, 8.5, 8.6_

  - [x] 13.3 Write proxy / SPA-fallback test for the nginx config
    - Run nginx with `nginx.console.conf` against a stub upstream (or the built
      image) and assert routing.
    - **Property 8: nginx routing partitions API paths from the SPA fallback** —
      `/admin`, `/health`, `/metrics` proxy to the gateway; `/`, `/buckets`,
      `/setup` serve `index.html`; `/healthz` returns 200.
    - **Validates: Requirements 8.3, 8.5, 8.6**

- [x] 14. Design system: evergreen-dark theme tokens
  - [x] 14.1 Apply the evergreen-dark design-system tokens in `console/src/styles.css`
    - Override the existing CSS custom properties on `:root` (keep token *names*
      `--bg`, `--panel`, `--accent`, … so existing rules inherit): colour tokens
      per the design table, typography scale, 4-based spacing scale, and the five
      control states (default/hover/focus/active/disabled) for button, primary
      button, destructive button, input, nav link, and tab. Focus is always a
      visible ring/border (never bare `outline:none`); status never conveyed by
      hue alone (icon/label alongside colour); text pairs meet WCAG 2.1 AA
      contrast.
    - _Requirements: 9.2, 9.3, 10.2, 10.4, 10.5_

  - [x] 14.2 Add accessibility assertions for the new screens
    - Component/e2e checks: every control has a programmatic label; focus moves
      to the first control on modal/dialog/step open and returns to the trigger
      on close; status/error regions use `role="alert"` / `aria-live`.
    - _Requirements: 10.1, 10.3, 10.5, 10.6_

- [x] 15. End-to-end: Playwright suite (mock Admin API at the network layer)
  - [x] 15.1 Add the Playwright e2e suite following the existing console convention
    - Intercept Admin API responses at the network layer (no live backend).
      Cover: login accepted/rejected + in-progress disable + 401 re-login
      (Req 1); full setup wizard — Telegram needs_code → code → success and
      3-wrong-code abort, buckets adopt/reinitialise confirm, config range
      errors, finalize one-time secret + Recovery Kit, then landing on admin
      screens; settings/keys/health panels; setup-required routing.
    - Exercises **Properties 2, 5, 7, 9** at the UI level.
    - _Requirements: 1.1, 1.3, 1.4, 1.6, 2.4, 3.1, 3.7, 3.9, 3.10, 3.12, 5.3, 7.1_

- [x] 16. Checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

- [x] 17. Figma design-record capture (delivery step — not an automated test)
  - [x] 17.1 Capture the running dashboard into a Figma design file
    - **Delivery / design-record step, not CI**: against the running dashboard,
      use the Figma integration (`create_new_file` to create the file in the
      operator's Figma team, then `generate_figma_design` to capture each primary
      screen as a frame): Login, each of the four Setup Wizard steps (Telegram,
      Buckets, Config, Finalize), Runtime Settings, Access Keys, Configuration
      Visibility, and System Health. This records the implemented design; it is
      not asserted by automated tests.
    - _Requirements: 9.1, 9.4_

## Notes

- Tasks marked with `*` are optional (tests and polish) and may be skipped for a
  faster MVP; core implementation tasks are never marked optional.
- Task 17 (Figma capture) is a one-time delivery/design-record step performed
  against the running dashboard, not an automated or CI task.
- A new backend **module** (`login_adapter.py`) is added inside the existing
  `gateway/app/telegram` package, so `[tool.setuptools].packages` in
  `pyproject.toml` does **not** change (task 1.1).
- Telethon is imported **only** in `gateway/app/telegram/login_adapter.py`,
  behind the `TelegramLoginPort`.
- Runtime settings already exist — task 10 extends the existing form and
  `api/settings.ts`, it does not rewrite them.
- Property tests (hypothesis / Vitest) cover Correctness Properties 1–9; concrete
  flows and wiring use example, integration, and Playwright e2e tests.
- Each task references the requirement clauses it satisfies for traceability.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.1"] },
    { "id": 3, "tasks": ["4.2", "13.1", "13.2", "14.1"] },
    { "id": 4, "tasks": ["4.3", "4.4", "5.1", "5.2", "10.1", "13.3", "14.2"] },
    { "id": 5, "tasks": ["5.3", "7.1", "8.1", "10.2", "10.4", "10.6", "10.7", "12.1"] },
    { "id": 6, "tasks": ["7.2", "8.2", "8.4", "10.3", "10.5", "10.8"] },
    { "id": 7, "tasks": ["7.3", "8.3", "8.5"] },
    { "id": 8, "tasks": ["15.1"] },
    { "id": 9, "tasks": ["17.1"] }
  ]
}
```
