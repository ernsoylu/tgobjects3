/**
 * Runtime-settings data hooks (Requirements 19.5–19.7).
 *
 * Wraps the Admin API's runtime-config surface (design: Admin API —
 * "GET/PUT /admin/settings — runtime config (effective within 5 s, no
 * restart)") in TanStack Query hooks so the Settings screen stays thin.
 *
 * Two hooks live here:
 *
 * - {@link useSettings} fetches `GET /admin/settings` and populates the form.
 * - {@link useUpdateSettings} PUTs `PUT /admin/settings` and invalidates the
 *   settings query on success so a successful change is reflected on the next
 *   read (well inside the "effective within 5 s" bound, Requirement 19.5). The
 *   shared `adminClient` attaches the CSRF header to unsafe methods, so the
 *   mutation itself carries no CSRF logic.
 *
 * The backend rejects a staging-size reduction below the currently reserved
 * bytes with HTTP 409 (Requirements 19.6/19.7) and out-of-range field values
 * with HTTP 422; both surface through the mutation's `error` so the screen can
 * report them.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * The five operator-editable runtime settings, as returned by and accepted on
 * `GET`/`PUT /admin/settings`.
 *
 * Field names match the Admin API's `SettingsBody` model exactly (snake_case
 * JSON): see `gateway/app/admin/router.py`. All five are integers:
 *
 * - `staging_max_size` — staging capacity in bytes (≥ 1); a reduction below the
 *   currently reserved bytes is rejected with HTTP 409.
 * - `cache_max_size` — on-disk record cache capacity in bytes (≥ 1).
 * - `cache_half_life` — cache ranking half-life in seconds (≥ 1).
 * - `read_ahead` — read-ahead record count (0–32 inclusive).
 * - `upload_concurrency` — concurrent upload worker count (≥ 1).
 */
export interface SettingsValues {
  readonly staging_max_size: number;
  readonly cache_max_size: number;
  readonly cache_half_life: number;
  readonly read_ahead: number;
  readonly upload_concurrency: number;
}

/** Query key for the settings resource; stable so refetches dedupe across mounts. */
export const SETTINGS_QUERY_KEY = ["admin", "settings"] as const;

async function fetchSettings(): Promise<SettingsValues> {
  const response = await adminClient.get<SettingsValues>("/settings");
  return response.data;
}

/** Fetch the current runtime-editable settings (Requirement 19.5). */
export function useSettings(): UseQueryResult<SettingsValues, Error> {
  return useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: fetchSettings,
  });
}

async function putSettings(body: SettingsValues): Promise<SettingsValues> {
  const response = await adminClient.put<SettingsValues>("/settings", body);
  return response.data;
}

/**
 * Apply a runtime-settings change, invalidating the settings query on success
 * so the refreshed view reflects the applied values (Requirement 19.5).
 *
 * The backend rejects a staging-size reduction below the reserved bytes with
 * HTTP 409 (Requirements 19.6/19.7) and out-of-range values with HTTP 422; the
 * mutation surfaces either through its `error` so the Settings screen can
 * present a readable message.
 */
export function useUpdateSettings(): UseMutationResult<SettingsValues, Error, SettingsValues> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: putSettings,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: SETTINGS_QUERY_KEY });
    },
  });
}
