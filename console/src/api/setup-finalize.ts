/**
 * Finalize setup-step data hook (admin-dashboard Requirements 3.8–3.12, 11.2, 11.3).
 *
 * The Finalize step of the first-run Setup Wizard completes setup through
 * `POST /admin/setup/finalize` (no request body). The payload shapes mirror the
 * backend models in `gateway/app/admin/setup.py` exactly (snake_case JSON):
 * `FinalizeResult` carries the first S3 access key and the downloadable
 * `RecoveryKit`.
 *
 * Secret handling (design: Data Models — finalize row; Requirements 11.2/11.3):
 *
 *   - `secret_access_key` is the first access key's plaintext secret, returned
 *     here **exactly once** and never again. The caller shows it a single time
 *     with a copy action and must drop it from component state afterwards.
 *   - `recovery_kit.master_key_verifier` is a *verifier* — a fixed known
 *     plaintext wrapped under the derived root key — not the master key itself.
 *     The master key never appears in the kit or in any response.
 *
 * On success the Finalize step invalidates {@link SETUP_STATUS_QUERY_KEY} so the
 * next `RequireSetup` read reports `setup_required = false` and navigation
 * reaches the admin screens (Requirement 3.12).
 *
 * The shared `adminClient` attaches `Authorization` and, on this unsafe POST,
 * the cached `X-CSRF-Token`, so this hook carries no auth/CSRF logic itself.
 */
import { useMutation, type UseMutationResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * One bucket→channel assignment in the Recovery Kit. Mirrors the backend
 * `RecoveryKitBucket`.
 */
export interface RecoveryKitBucket {
  readonly bucket_name: string;
  readonly channel_id: number;
}

/**
 * The downloadable Recovery Kit (Requirement 2.5, 11.2). Mirrors the backend
 * `RecoveryKit`: the master-key *verifier* (never the master key), the Telegram
 * session credentials, and the bucket channel assignments.
 */
export interface RecoveryKit {
  readonly master_key_verifier: string;
  readonly telegram_session: string;
  readonly buckets: readonly RecoveryKitBucket[];
}

/**
 * `POST /admin/setup/finalize` response. Mirrors the backend `FinalizeResult`:
 * the first access key (its `secret_access_key` returned exactly once) and the
 * Recovery Kit.
 */
export interface FinalizeResult {
  readonly access_key_id: string;
  readonly secret_access_key: string;
  readonly recovery_kit: RecoveryKit;
}

/** Path of the finalize step, relative to the Admin API base URL. */
export const FINALIZE_STEP_ENDPOINT = "/setup/finalize";

async function postFinalize(): Promise<FinalizeResult> {
  const response = await adminClient.post<FinalizeResult>(FINALIZE_STEP_ENDPOINT);
  return response.data;
}

/**
 * Complete first-run setup (Requirements 3.8–3.12).
 *
 * The mutation takes no variables — the backend finalises the in-process wizard
 * session it has accumulated across the earlier steps. While `isPending` the
 * Finalize step disables resubmission (Requirement 3.8). The resolved
 * {@link FinalizeResult} carries the one-time `secret_access_key` and the
 * Recovery Kit; the step owns their single display and clears the secret on
 * navigation away (Requirements 3.9, 3.10, 11.3). This hook deliberately never
 * writes the result to the query cache, so no secret is cached.
 */
export function useFinalizeSetup(): UseMutationResult<FinalizeResult, Error, void> {
  return useMutation({
    mutationFn: postFinalize,
  });
}
