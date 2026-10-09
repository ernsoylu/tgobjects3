/**
 * Bucket-list data hook.
 *
 * Wraps `GET /admin/buckets` (design: Admin API — "list with per-bucket stats")
 * in a TanStack Query hook so screens stay thin. The query auto-refreshes on an
 * interval so the Console bucket list reflects background commits and GC without
 * a manual reload (Requirement 19.1: auto-refresh every ≤ 30 seconds).
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * Per-bucket statistics returned by `GET /admin/buckets`.
 *
 * Field names match the Admin API's `BucketStats` model exactly (snake_case
 * JSON): see `gateway/app/admin/router.py`. Requirement 19.1.
 */
export interface BucketStats {
  readonly name: string;
  readonly object_count: number;
  readonly stored_bytes: number;
  readonly pending_write_count: number;
}

/** Query key for the bucket list; stable so refetches dedupe across screens. */
export const BUCKETS_QUERY_KEY = ["admin", "buckets"] as const;

/**
 * Auto-refresh cadence for the bucket list, in milliseconds. Requirement 19.1
 * caps this at 30 seconds; we poll a little under the cap so a slow response
 * still lands within the window.
 */
export const BUCKETS_REFETCH_INTERVAL_MS = 15_000;

async function fetchBuckets(): Promise<BucketStats[]> {
  const response = await adminClient.get<BucketStats[]>("/buckets");
  return response.data;
}

/** Fetch the bucket list, auto-refreshing on an interval (Requirement 19.1). */
export function useBuckets(): UseQueryResult<BucketStats[], Error> {
  return useQuery({
    queryKey: BUCKETS_QUERY_KEY,
    queryFn: fetchBuckets,
    refetchInterval: BUCKETS_REFETCH_INTERVAL_MS,
  });
}
