/**
 * Config setup-step data hook (admin-dashboard Requirements 3.2, 3.4, 3.5).
 *
 * The Config step of the first-run Setup Wizard records the gateway's storage
 * configuration through `POST /admin/setup/config`. The payload shapes mirror
 * the backend models in `gateway/app/admin/setup.py` exactly (snake_case JSON):
 * `ConfigStepBody` carries the staging path and max size, the cache path and max
 * size, and the master key, and `ConfigStepResult` returns a single `accepted`
 * flag.
 *
 * The backend validates every field against Requirement 3.4/3.5's ranges
 * (staging 1 MiB–1 TiB, cache 1 MiB–100 GiB, master key ≥ 16 characters, mirrored
 * by `STAGING_MIN_SIZE`/`STAGING_MAX_SIZE`, `CACHE_MIN_SIZE`/`CACHE_MAX_SIZE`, and
 * `MASTER_KEY_MIN_LENGTH` in `setup.py`). The step validates the same ranges
 * client-side *before* calling this mutation so an out-of-range value never
 * leaves the browser (Requirement 12.6); the backend bounds remain the
 * authoritative backstop.
 *
 * Secret handling (Requirements 11.1/11.2): `master_key` is sent exactly once in
 * this request body and is never logged, echoed back, cached in the query layer,
 * or placed in the Recovery Kit. The field lives only in the Config step's
 * in-memory form state for the lifetime of the step; this hook holds no state and
 * invalidates no query cache (the endpoint only populates the backend's in-process
 * wizard session), so nothing here retains the key.
 *
 * The shared `adminClient` attaches `Authorization` and, on this unsafe POST, the
 * cached `X-CSRF-Token`, so this hook carries no auth/CSRF logic itself.
 */
import { useMutation, type UseMutationResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * `POST /admin/setup/config` body. Mirrors the backend `ConfigStepBody`: sizes
 * are byte counts and `master_key` is the raw operator-chosen key. Validated
 * client-side against the Requirement 3.4/3.5 ranges before this is ever sent.
 */
export interface ConfigStepBody {
  readonly staging_path: string;
  readonly staging_max_size: number;
  readonly cache_path: string;
  readonly cache_max_size: number;
  readonly master_key: string;
}

/** `POST /admin/setup/config` response. Mirrors the backend `ConfigStepResult`. */
export interface ConfigStepResult {
  readonly accepted: boolean;
}

/** Path of the config step, relative to the Admin API base URL. */
export const CONFIG_STEP_ENDPOINT = "/setup/config";

async function postConfig(body: ConfigStepBody): Promise<ConfigStepResult> {
  const response = await adminClient.post<ConfigStepResult>(CONFIG_STEP_ENDPOINT, body);
  return response.data;
}

/**
 * Submit the storage configuration and master key (Requirements 3.2, 3.4, 3.5).
 *
 * The step validates every field's range client-side before invoking this
 * mutation, so a successful call means the backend accepted an already in-range
 * body; the result's `accepted` flag confirms the wizard may advance.
 */
export function useSubmitConfig(): UseMutationResult<ConfigStepResult, Error, ConfigStepBody> {
  return useMutation({
    mutationFn: postConfig,
  });
}
