/**
 * Upload-jobs data hooks.
 *
 * Wraps the Admin API's upload-job surface (design: Admin API — "upload job
 * list with retry action") in TanStack Query hooks so the Jobs screen stays
 * thin. Requirement 19.3: the Console lists upload jobs with their status and
 * lets the operator retry a FAILED job, which transitions it back to STAGED.
 *
 * Two hooks live here:
 *
 * - {@link useJobs} fetches `GET /admin/jobs` and auto-refreshes on an interval
 *   so new jobs and state transitions surface without a manual reload.
 * - {@link useRetryJob} POSTs `POST /admin/jobs/{id}/retry` and invalidates the
 *   jobs query on success. The shared `adminClient` attaches the CSRF header to
 *   unsafe methods automatically, so the mutation itself carries no CSRF logic.
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
 * One upload job as returned by `GET /admin/jobs`.
 *
 * Field names match the Admin API's `JobEntry` model exactly (snake_case JSON):
 * see `gateway/app/admin/router.py`. `last_error` is `null` for jobs that have
 * not failed. Requirement 19.3.
 */
export interface JobEntry {
  readonly id: string;
  readonly generation_id: string;
  readonly state: string;
  readonly attempts: number;
  readonly last_error: string | null;
}

/** The job state the retry action applies to (Requirement 19.3). */
export const JOB_STATE_FAILED = "FAILED";

/** Query key for the jobs list; stable so refetches dedupe across screens. */
export const JOBS_QUERY_KEY = ["admin", "jobs"] as const;

/**
 * Auto-refresh cadence for the jobs list, in milliseconds. Jobs move through
 * their lifecycle in the background (STAGED → UPLOADING → COMMITTED / FAILED),
 * so a short interval keeps the view close to live without hammering the API.
 */
export const JOBS_REFETCH_INTERVAL_MS = 10_000;

async function fetchJobs(): Promise<JobEntry[]> {
  const response = await adminClient.get<JobEntry[]>("/jobs");
  return response.data;
}

/** Fetch the upload-job list, auto-refreshing on an interval (Requirement 19.3). */
export function useJobs(): UseQueryResult<JobEntry[], Error> {
  return useQuery({
    queryKey: JOBS_QUERY_KEY,
    queryFn: fetchJobs,
    refetchInterval: JOBS_REFETCH_INTERVAL_MS,
  });
}

async function retryJob(jobId: string): Promise<JobEntry> {
  const response = await adminClient.post<JobEntry>(`/jobs/${encodeURIComponent(jobId)}/retry`);
  return response.data;
}

/**
 * Retry a FAILED upload job, invalidating the jobs list on success so the
 * refreshed view reflects the transition back to STAGED (Requirement 19.3).
 *
 * The backend rejects a non-FAILED job with HTTP 409; the Jobs screen only
 * offers the action for FAILED jobs, but the mutation surfaces any error (409,
 * 404, auth/CSRF) through its `error` so the caller can report it.
 */
export function useRetryJob(): UseMutationResult<JobEntry, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: retryJob,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: JOBS_QUERY_KEY });
    },
  });
}
