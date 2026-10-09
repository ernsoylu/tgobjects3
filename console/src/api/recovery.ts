/**
 * Recovery-status data hook (Requirement 19.4).
 *
 * The Recovery Status screen shows the gateway's recovery posture: the
 * timestamp of the last completed Checkpoint, the per-channel high-water marks
 * that bound a recovery scan, and the outcome of the most recent recovery
 * drill. The design derives all of this from the `checkpoints` table
 * (`gateway/app/db/models.py`: `Checkpoint` with `message_id`, `created_at`,
 * `high_water`) written by the Checkpoint Worker
 * (`gateway/app/jobs/checkpoint_worker.py`).
 *
 * BACKEND DEPENDENCY — endpoint not yet implemented
 * -------------------------------------------------
 * As of task 44.4 the Admin API router (`gateway/app/admin/router.py`, task
 * 41.1) exposes `/admin/buckets`, `/admin/objects`, `/admin/jobs`,
 * `/admin/settings`, and `/admin/keys`, but NOT a recovery/checkpoint endpoint.
 * The design's Admin API list (design.md — "Admin API") does not yet include
 * one either. This hook therefore targets the expected future endpoint
 * `GET /admin/recovery` and treats its absence as a first-class, non-fatal
 * state: a 404 (route not mounted) or 501 (not implemented) resolves to
 * `null` rather than throwing, so the screen can render an informative
 * "recovery status endpoint not available" panel instead of a crash. When the
 * backend adds the endpoint, no frontend change is required beyond confirming
 * the response shape matches {@link RecoveryStatus}.
 *
 * The expected response shape mirrors the `Checkpoint` row plus a drill
 * summary, in the Admin API's snake_case JSON convention (as used by
 * {@link ../api/buckets.BucketStats}).
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import axios from "axios";

import { adminClient } from "./client";

/** Outcome of a recovery drill, as reported by the backend. */
export type DrillOutcome = "pass" | "fail";

/**
 * One channel's recovery high-water mark: the highest confirmed Telegram
 * `message_id` recorded for that channel in the last Checkpoint. A recovery
 * scan only needs to replay messages strictly newer than this id, so it bounds
 * the work required to rebuild the index for the channel.
 */
export interface HighWaterMark {
  /** Telegram channel id (rendered as a string, matching the JSONB key). */
  readonly channel_id: string;
  /** Highest confirmed message id the last Checkpoint covered for the channel. */
  readonly message_id: number;
}

/**
 * The most recent recovery drill result. `null` when no drill has ever run
 * (Requirement 19.4: display "None" when no drill has completed).
 */
export interface RecoveryDrill {
  readonly outcome: DrillOutcome;
  /** ISO-8601 UTC timestamp of when the drill completed. */
  readonly completed_at: string;
}

/**
 * Recovery posture returned by `GET /admin/recovery` (expected backend shape).
 *
 * `last_checkpoint_at` is `null` when no Checkpoint has been written yet;
 * `last_drill` is `null` when no recovery drill has run yet. Both map to the
 * "None" display required by Requirement 19.4.
 */
export interface RecoveryStatus {
  /** ISO-8601 UTC timestamp of the last completed Checkpoint, or `null`. */
  readonly last_checkpoint_at: string | null;
  /** The last Checkpoint's `message_id`, or `null` when none exists. */
  readonly last_checkpoint_message_id: number | null;
  /** Per-channel high-water marks the last Checkpoint recorded. */
  readonly high_water_marks: readonly HighWaterMark[];
  /** Most recent recovery drill outcome, or `null` when none has run. */
  readonly last_drill: RecoveryDrill | null;
}

/**
 * Query key for recovery status. Stable so refetches dedupe across mounts.
 */
export const RECOVERY_QUERY_KEY = ["admin", "recovery"] as const;

/**
 * Auto-refresh cadence for the recovery view, in milliseconds. Checkpoints are
 * written on a slow cadence (minutes), so a 30 s poll keeps the posture fresh
 * without hammering the Admin API.
 */
export const RECOVERY_REFETCH_INTERVAL_MS = 30_000;

/** Path of the expected (not-yet-implemented) recovery endpoint. */
export const RECOVERY_ENDPOINT = "/recovery";

/**
 * Sentinel returned when the backend recovery endpoint is not available.
 * `fetchRecoveryStatus` resolves to this instead of throwing on a 404/501 so
 * the screen can distinguish "endpoint missing" from a genuine fetch error.
 */
export const RECOVERY_ENDPOINT_UNAVAILABLE = null;

/** HTTP statuses that mean "the endpoint isn't there yet", not a real error. */
const ENDPOINT_ABSENT_STATUSES = new Set([404, 501]);

/**
 * Fetch the recovery status, tolerating a missing backend endpoint.
 *
 * Resolves to a {@link RecoveryStatus} when the endpoint responds, or to `null`
 * ({@link RECOVERY_ENDPOINT_UNAVAILABLE}) when the endpoint is absent (404/501).
 * Any other failure (network error, 5xx, auth) rejects so TanStack Query can
 * surface it as an error state.
 */
export async function fetchRecoveryStatus(): Promise<RecoveryStatus | null> {
  try {
    const response = await adminClient.get<RecoveryStatus>(RECOVERY_ENDPOINT);
    return response.data;
  } catch (error) {
    if (
      axios.isAxiosError(error) &&
      error.response !== undefined &&
      ENDPOINT_ABSENT_STATUSES.has(error.response.status)
    ) {
      return RECOVERY_ENDPOINT_UNAVAILABLE;
    }
    throw error;
  }
}

/**
 * Fetch recovery status, auto-refreshing on an interval (Requirement 19.4).
 *
 * A resolved value of `null` means the backend endpoint is not yet available;
 * the screen renders an informative placeholder for that case rather than
 * treating it as an error.
 */
export function useRecoveryStatus(): UseQueryResult<RecoveryStatus | null, Error> {
  return useQuery({
    queryKey: RECOVERY_QUERY_KEY,
    queryFn: fetchRecoveryStatus,
    refetchInterval: RECOVERY_REFETCH_INTERVAL_MS,
  });
}
