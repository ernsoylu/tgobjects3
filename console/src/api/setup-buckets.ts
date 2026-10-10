/**
 * Buckets setup-step data hook (admin-dashboard Requirements 3.2, 3.7).
 *
 * The Buckets step of the first-run Setup Wizard assigns each bucket to a
 * Telegram channel through `POST /admin/setup/buckets`. The payload shapes
 * mirror the backend models in `gateway/app/admin/setup.py` exactly (snake_case
 * JSON): `BucketsStepBody` carries the full set of `BucketAssignmentBody`
 * assignments, and `BucketsStepResult` returns a per-bucket `BucketStepEntry`
 * list plus an aggregate `requires_choice` flag.
 *
 * A channel that already holds bucket-manifest messages cannot be assigned
 * blind: the backend flags its entry `existing_manifest`/`requires_choice` and
 * returns a `warning`, and the operator must re-POST that assignment with
 * `on_existing` set to `"adopt"` (keep the existing bucket) or `"reinitialise"`
 * (discard it — its existing data becomes inaccessible). The reinitialise path
 * is gated in the UI behind that warning and an explicit confirmation
 * (Requirement 3.7); this hook only carries the request/response types and the
 * mutation.
 *
 * The shared `adminClient` attaches `Authorization` and, on this unsafe POST,
 * the cached `X-CSRF-Token`, so this hook carries no auth/CSRF logic itself. The
 * endpoint is purely state-collecting on the backend's in-process wizard
 * session, so there is no query cache to invalidate here.
 */
import { useMutation, type UseMutationResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/** The adopt/reinitialise choices for a channel that already holds a manifest. */
export type OnExisting = "adopt" | "reinitialise";

/**
 * One bucket→channel assignment. Mirrors the backend `BucketAssignmentBody`:
 * `on_existing` is omitted (or `null`) for an empty channel and only set once
 * the operator has chosen how to handle a channel with an existing manifest.
 */
export interface BucketAssignmentBody {
  readonly bucket_name: string;
  readonly channel_id: number;
  readonly on_existing?: OnExisting | null;
}

/** `POST /admin/setup/buckets` body: the full set of assignments. */
export interface BucketsStepBody {
  readonly assignments: readonly BucketAssignmentBody[];
}

/**
 * Per-bucket outcome of the buckets step. Mirrors the backend `BucketStepEntry`:
 * when `existing_manifest` is true and `requires_choice` is set the operator
 * must re-POST that assignment with `on_existing`, surfacing `warning` first.
 */
export interface BucketStepEntry {
  readonly bucket_name: string;
  readonly channel_id: number;
  readonly existing_manifest: boolean;
  readonly requires_choice: boolean;
  readonly warning: string | null;
}

/** `POST /admin/setup/buckets` response. Mirrors the backend `BucketsStepResult`. */
export interface BucketsStepResult {
  readonly buckets: readonly BucketStepEntry[];
  /** True while at least one bucket still needs an adopt/reinitialise choice. */
  readonly requires_choice: boolean;
}

/** Path of the buckets step, relative to the Admin API base URL. */
export const BUCKETS_STEP_ENDPOINT = "/setup/buckets";

async function postBuckets(body: BucketsStepBody): Promise<BucketsStepResult> {
  const response = await adminClient.post<BucketsStepResult>(BUCKETS_STEP_ENDPOINT, body);
  return response.data;
}

/**
 * Submit the bucket→channel assignments (Requirements 3.2, 3.7).
 *
 * The result's `requires_choice` tells the Buckets step whether any channel
 * still needs an adopt/reinitialise decision; the step re-POSTs the full set
 * once the operator supplies the missing `on_existing` choices.
 */
export function useSubmitBuckets(): UseMutationResult<BucketsStepResult, Error, BucketsStepBody> {
  return useMutation({
    mutationFn: postBuckets,
  });
}
