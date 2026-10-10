/**
 * Configuration Visibility data hook (admin-dashboard Requirements 6.2–6.4, 11.1).
 *
 * Wraps the Admin API's read-only environment-configuration surface
 * (`GET /admin/config`) in a TanStack Query hook so the Configuration
 * Visibility panel stays presentational. The shapes mirror
 * `gateway/app/admin/router.py` exactly (snake_case JSON):
 *
 *   - `environment` — the non-secret Environment_Settings as `{ name, value }`
 *     pairs. These are fixed at deployment and require a redeploy or restart to
 *     change; the panel renders them read-only with no editing control
 *     (Requirements 6.2, 6.4).
 *   - `secrets` — the secret Environment_Settings (the master key, the database
 *     URL with its embedded credentials) as `{ name, is_set }`. The backend
 *     never carries a secret value across the boundary; the panel shows only
 *     "set" / "not set" (Requirements 6.3, 11.1).
 *
 * The response is read-only, so this module exposes a single query hook and no
 * mutation. The shared `adminClient` attaches Basic auth; no CSRF logic is
 * needed on a GET.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * One non-secret Environment_Setting from `GET /admin/config`.
 *
 * Mirrors the backend `EnvSettingEntry` model: a deployment-fixed value shown
 * read-only, labelled by the panel as requiring a redeploy or restart to change
 * (Requirement 6.2). The `value` is already stringified by the backend for
 * display (booleans as `"true"`/`"false"`, integers verbatim).
 */
export interface EnvSettingEntry {
  readonly name: string;
  readonly value: string;
}

/**
 * One secret Environment_Setting from `GET /admin/config`.
 *
 * Mirrors the backend `SecretEnvSettingEntry` model: the secret value is
 * **never** included (Requirement 6.3, 11.1) — only whether it is configured.
 */
export interface SecretEnvSettingEntry {
  readonly name: string;
  readonly is_set: boolean;
}

/**
 * The full Configuration Visibility payload from `GET /admin/config`.
 *
 * Mirrors the backend `ConfigVisibility` model. The five operator-editable
 * Runtime_Settings are intentionally absent — they are read and edited through
 * `GET`/`PUT /admin/settings` (Requirement 4), keeping the editable and
 * read-only surfaces disjoint (Requirement 6.1).
 */
export interface ConfigVisibility {
  readonly environment: readonly EnvSettingEntry[];
  readonly secrets: readonly SecretEnvSettingEntry[];
}

/** Query key for the config resource; stable so refetches dedupe across mounts. */
export const CONFIG_QUERY_KEY = ["admin", "config"] as const;

async function fetchConfig(): Promise<ConfigVisibility> {
  const response = await adminClient.get<ConfigVisibility>("/config");
  return response.data;
}

/**
 * Fetch the read-only environment configuration (Requirement 6).
 *
 * Non-secret settings arrive as `{ name, value }`; secret settings arrive as
 * `{ name, is_set }` with no value. The Configuration Visibility panel renders
 * both read-only — there is no mutation counterpart because this configuration
 * cannot change at runtime (Requirement 6.4).
 */
export function useConfig(): UseQueryResult<ConfigVisibility, Error> {
  return useQuery({
    queryKey: CONFIG_QUERY_KEY,
    queryFn: fetchConfig,
  });
}
