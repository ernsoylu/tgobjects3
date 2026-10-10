/**
 * Admin API client.
 *
 * Thin wrapper over axios for talking to the gateway Admin API (served on
 * `ADMIN_PORT`, default 8080). In development, Vite proxies `/admin/*` to the
 * gateway (see `vite.config.ts`); in production the console is served from the
 * same origin as the Admin API behind nginx, so a relative base URL works in
 * both cases.
 *
 * Auth model (design: "Sequence: Dashboard login + CSRF bootstrap"): the
 * backend uses HTTP Basic auth with the fixed username `admin` and a password
 * supplied at login, and it returns the per-session CSRF token in the
 * `X-CSRF-Token` *response* header on any authenticated request — it sets no
 * cookie. The Console therefore holds the Basic credential and the CSRF token
 * in process memory only (never storage/cookies): a request interceptor
 * attaches `Authorization` on every request and `X-CSRF-Token` on unsafe
 * methods, and a response interceptor caches the token whenever the server
 * echoes it. A global 401 hook lets the auth layer end the session.
 *
 * On a 403 from a mutating request the client treats the CSRF token as stale:
 * it re-runs the bootstrap `GET` to refresh the `X-CSRF-Token` and retries the
 * original request exactly once; a second 403 is surfaced to the caller as a
 * readable error (design: "Admin API status → Dashboard behaviour", 403 row;
 * Requirement 11.6). A per-request guard flag prevents retry loops.
 *
 * This client talks only to the Admin API. S3/SigV4 request signing is a
 * separate concern and is unaffected by anything here. Requirements 1.5,
 * 11.4, 11.5, 11.6, 12.1, 12.3.
 */
import axios, {
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";

declare module "axios" {
  /**
   * Per-request opt-out of the global 401 session-end hook. See
   * {@link SUPPRESS_UNAUTHORIZED_HANDLER} for why the interactive Telegram login
   * step sets this. Augmenting `AxiosRequestConfig` lets callers pass the flag
   * with full type-safety instead of casting.
   */
  interface AxiosRequestConfig {
    "x-suppress-unauthorized-handler"?: boolean;
  }
}

/** Header the Admin API expects the CSRF token on for unsafe requests, and
 * echoes the current token on in authenticated responses. */
export const CSRF_HEADER = "X-CSRF-Token";

/** Fixed Basic-auth username the Admin API authenticates against. */
export const ADMIN_USERNAME = "admin";

/** HTTP methods that do not mutate state and therefore need no CSRF token. */
const SAFE_METHODS = new Set(["get", "head", "options"]);

/** Base path for all Admin API routes. Relative so it works under any origin. */
export const ADMIN_BASE_URL = "/admin";

/**
 * Bootstrap read used to (re)obtain the per-session CSRF token: an existing
 * authenticated GET whose response echoes `X-CSRF-Token`. `signIn` issues this
 * once at login; the response-error interceptor re-issues it to refresh a stale
 * token before retrying a 403'd mutation (Requirement 11.6).
 */
export const BOOTSTRAP_PATH = "/buckets";

/** Default per-request timeout in milliseconds (Requirement 12.3). */
export const ADMIN_REQUEST_TIMEOUT_MS = 30_000;

/**
 * In-memory session credential as the ready-to-send `Authorization` header
 * value (`"Basic base64(admin:password)"`), or `null` when signed out. Kept in
 * process memory only — never persisted to storage or cookies.
 */
let basicAuth: string | null = null;

/**
 * In-memory CSRF token, cached from the `X-CSRF-Token` response header of the
 * most recent authenticated response, or `null` before bootstrap / after
 * sign-out.
 */
let csrfToken: string | null = null;

/** Registered global 401 handler, invoked when an authenticated request is
 * rejected with HTTP 401 so the auth layer can end the session. */
let onUnauthorized: (() => void) | null = null;

/**
 * Set the in-memory Basic credential from a password. The username is always
 * the fixed `ADMIN_USERNAME`. Clears any previously cached CSRF token so a
 * fresh bootstrap re-establishes it for the new credential.
 */
export function setCredentials(password: string): void {
  basicAuth = `Basic ${btoa(`${ADMIN_USERNAME}:${password}`)}`;
  csrfToken = null;
}

/** Discard the in-memory Basic credential and cached CSRF token. */
export function clearCredentials(): void {
  basicAuth = null;
  csrfToken = null;
}

/**
 * Register (or clear, with `null`) the global handler invoked on any HTTP 401
 * from the Admin API. The auth layer wires this to end the session and redirect
 * to login. Returns a disposer that unregisters this handler.
 */
export function setUnauthorizedHandler(handler: (() => void) | null): () => void {
  onUnauthorized = handler;
  return () => {
    if (onUnauthorized === handler) {
      onUnauthorized = null;
    }
  };
}

function attachAuth(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  if (basicAuth !== null) {
    config.headers.set("Authorization", basicAuth);
  }
  const method = (config.method ?? "get").toLowerCase();
  if (!SAFE_METHODS.has(method) && csrfToken !== null) {
    config.headers.set(CSRF_HEADER, csrfToken);
  }
  return config;
}

function cacheCsrf(response: AxiosResponse): AxiosResponse {
  const token = response.headers[CSRF_HEADER.toLowerCase()];
  if (typeof token === "string" && token.length > 0) {
    csrfToken = token;
  }
  return response;
}

/**
 * Per-request config augmented with the client's retry guard. The flag is set
 * on the config object of a mutation the moment it is retried after a CSRF
 * refresh, so a second 403 on the same logical request falls straight through
 * to the caller instead of looping (Requirement 11.6).
 */
interface RetryableRequestConfig extends InternalAxiosRequestConfig {
  _csrfRetried?: boolean;
}

/**
 * Opt a single request out of the global 401 session-end hook. The interactive
 * Telegram login step (`POST /admin/setup/telegram`) returns HTTP 401 as a
 * *domain* signal — the 3rd wrong code aborts the login and the step returns the
 * operator to credential entry (Requirement 2.4) — rather than as an expired
 * admin session. Treating that 401 as a session end would tear the operator out
 * of the wizard to the login screen, so the setup-telegram call sets this flag
 * and the response interceptor skips `onUnauthorized` for it. Every other
 * request leaves the flag unset and keeps the default behaviour.
 */
export const SUPPRESS_UNAUTHORIZED_HANDLER = "x-suppress-unauthorized-handler";

/** True for a request config whose method mutates state (needs a CSRF token). */
function isUnsafeMethod(config: InternalAxiosRequestConfig): boolean {
  return !SAFE_METHODS.has((config.method ?? "get").toLowerCase());
}

/**
 * Build the response-error interceptor for a given instance. It handles two
 * cross-cutting Admin API faults from the design's status→behaviour table:
 *
 *   - **401** — invoke the global unauthorized hook so the auth layer can end
 *     the session, then reject (unchanged behaviour).
 *   - **403 on a mutation** — treat the CSRF token as stale: re-run the
 *     bootstrap `GET` to refresh `X-CSRF-Token`, then retry the original
 *     request exactly once. A `_csrfRetried` guard on the request config stops
 *     a second 403 (or a 403 on a safe method) from looping; it is surfaced to
 *     the caller instead (Requirement 11.6). A 403 whose bootstrap refresh
 *     itself fails rejects with that failure.
 *
 * All other errors reject untouched; `describeAdminError` (api/errors.ts) maps
 * them to operator-readable messages at the call site.
 */
function makeResponseErrorHandler(instance: AxiosInstance): (error: unknown) => Promise<unknown> {
  return async (error: unknown): Promise<unknown> => {
    if (!axios.isAxiosError(error)) {
      return Promise.reject(error);
    }
    const status = error.response?.status;

    if (status === 401) {
      // A request may opt out of the global session-end hook when its 401 is a
      // domain signal rather than an expired session (see the flag's docs).
      if (error.config?.[SUPPRESS_UNAUTHORIZED_HANDLER] !== true) {
        onUnauthorized?.();
      }
      return Promise.reject(error);
    }

    const config = error.config as RetryableRequestConfig | undefined;
    if (
      status === 403 &&
      config !== undefined &&
      isUnsafeMethod(config) &&
      config._csrfRetried !== true
    ) {
      config._csrfRetried = true;
      try {
        // Refresh the CSRF token via the bootstrap read; the success
        // interceptor caches the echoed `X-CSRF-Token`.
        await instance.get(BOOTSTRAP_PATH);
      } catch {
        // The refresh itself failed — surface the original 403 to the caller.
        return Promise.reject(error);
      }
      // Retry the original request once; `attachAuth` now stamps the fresh
      // token. A second 403 carries `_csrfRetried` and falls through.
      return instance.request(config);
    }

    return Promise.reject(error);
  };
}

/** Create a configured axios instance for the Admin API. */
export function createAdminClient(baseURL: string = ADMIN_BASE_URL): AxiosInstance {
  const instance = axios.create({
    baseURL,
    timeout: ADMIN_REQUEST_TIMEOUT_MS,
    headers: { Accept: "application/json" },
  });
  instance.interceptors.request.use(attachAuth);
  instance.interceptors.response.use(cacheCsrf, makeResponseErrorHandler(instance));
  return instance;
}

/** Shared singleton client used by the data hooks. */
export const adminClient = createAdminClient();

/**
 * Base path for the gateway's observability endpoints. Unlike the Admin API,
 * the health probes (`/health/live`, `/health/ready`) and the Prometheus
 * scrape target (`/metrics`) are mounted on the admin *app* root — **not** under
 * `/admin` (see `gateway/app/main.py::_wire_health_endpoints` /
 * `_wire_metrics_endpoint`). Both the Vite dev proxy and the deploy nginx
 * (`deploy/nginx.console.conf`) forward `/health` and `/metrics` to the gateway,
 * so a root-relative base URL works in dev and production alike.
 */
export const OBSERVABILITY_BASE_URL = "/";

/**
 * Create an axios instance for the gateway's observability endpoints
 * (`/health/*`, `/metrics`).
 *
 * These endpoints live at the admin app root rather than under `/admin`, so
 * they need their own base URL and must *not* carry the Admin API's CSRF /
 * 401-session interceptors: the System Health panel derives readiness purely
 * from the HTTP status (200 → ready, 503 → not ready, network/timeout →
 * unknown, Requirements 7.2–7.5) and must observe the raw status itself rather
 * than have a 401 hijack the request into a session-end. The shared Basic
 * credential is still attached when present (via {@link attachAuth}); these are
 * safe `GET`s, so no CSRF token is needed. `validateStatus` returns `true` for
 * every status so a 503 resolves rather than rejecting — the caller reads
 * `response.status` directly.
 */
export function createObservabilityClient(baseURL: string = OBSERVABILITY_BASE_URL): AxiosInstance {
  const instance = axios.create({
    baseURL,
    timeout: ADMIN_REQUEST_TIMEOUT_MS,
    validateStatus: () => true,
  });
  instance.interceptors.request.use(attachAuth);
  return instance;
}

/** Shared singleton client used by the System Health data hooks. */
export const observabilityClient = createObservabilityClient();
