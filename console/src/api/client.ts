/**
 * Admin API client.
 *
 * Thin wrapper over axios for talking to the gateway Admin API (served on
 * `ADMIN_PORT`, default 8080). In development, Vite proxies `/admin/*` to the
 * gateway (see `vite.config.ts`); in production the console is served from the
 * same origin as the Admin API behind nginx, so a relative base URL works in
 * both cases.
 *
 * CSRF: every write endpoint on the Admin API validates a CSRF token in a
 * request header against the session-bound token (design: Admin API — "All
 * write endpoints validate a CSRF token in the request header against the
 * session-bound token"). The gateway issues the token on a bootstrap GET and
 * sets it in a readable cookie; we mirror it back on every unsafe method via
 * the `X-CSRF-Token` header. Requirement 19.1.
 */
import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

/** Header the Admin API expects the CSRF token on for unsafe requests. */
export const CSRF_HEADER = "X-CSRF-Token";

/** Cookie the Admin API sets the readable CSRF token in. */
export const CSRF_COOKIE = "tgo_csrf";

/** HTTP methods that do not mutate state and therefore need no CSRF token. */
const SAFE_METHODS = new Set(["get", "head", "options"]);

/** Base path for all Admin API routes. Relative so it works under any origin. */
export const ADMIN_BASE_URL = "/admin";

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

function attachCsrf(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const method = (config.method ?? "get").toLowerCase();
  if (!SAFE_METHODS.has(method)) {
    const token = readCookie(CSRF_COOKIE);
    if (token) {
      config.headers.set(CSRF_HEADER, token);
    }
  }
  return config;
}

/** Create a configured axios instance for the Admin API. */
export function createAdminClient(baseURL: string = ADMIN_BASE_URL): AxiosInstance {
  const instance = axios.create({
    baseURL,
    withCredentials: true,
    headers: { Accept: "application/json" },
  });
  instance.interceptors.request.use(attachCsrf);
  return instance;
}

/** Shared singleton client used by the data hooks. */
export const adminClient = createAdminClient();
