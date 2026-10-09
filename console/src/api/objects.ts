/**
 * Object-browser data hooks (Requirement 19.2, task 44.2).
 *
 * Thin TanStack Query wrappers over the Admin API's object-browser surface,
 * built on the shared {@link adminClient}. The response shapes mirror the
 * gateway's `GET /admin/objects` (`ObjectsPage` / `ObjectEntry`) and
 * `GET /admin/buckets` (`BucketStats`) models exactly — field names included —
 * so the screen renders the backend JSON without any remapping:
 *
 * - `GET /admin/objects?bucket=&prefix=&start_after=` returns one page of up to
 *   100 committed objects in ascending key order, filtered by `prefix` and
 *   strictly after `start_after`, plus `next_start_after` (the opaque cursor for
 *   the following page, or `null` on the last page).
 * - `GET /admin/buckets` returns the bucket list; here it only feeds the bucket
 *   selector, so we read `name` and ignore the stats the Buckets screen uses.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/** One committed object, matching the gateway `ObjectEntry` model. */
export interface ObjectEntry {
  key: string;
  size: number;
  etag: string | null;
  /** ISO-8601 timestamp string (FastAPI serialises `datetime` as a string). */
  last_modified: string;
}

/** One page of the object browser, matching the gateway `ObjectsPage` model. */
export interface ObjectsPage {
  bucket: string;
  prefix: string;
  objects: ObjectEntry[];
  /** Cursor for the next page's `start_after`, or `null` on the last page. */
  next_start_after: string | null;
}

/**
 * One bucket as returned by `GET /admin/buckets` (`BucketStats` model). Only
 * `name` is used by the object browser; the remaining fields are declared for
 * fidelity to the backend contract.
 */
export interface BucketStats {
  name: string;
  object_count: number;
  stored_bytes: number;
  pending_write_count: number;
}

/** Arguments for a single object-browser page request. */
export interface ObjectsQueryArgs {
  /** Bucket to browse. The query is disabled until this is non-empty. */
  bucket: string;
  /** Key prefix to filter by; empty string lists from the start. */
  prefix: string;
  /** Cursor from the previous page's `next_start_after`, or `null` for page 1. */
  startAfter: string | null;
}

/** Stable query-key factory so related caches invalidate together. */
export const objectsKeys = {
  all: ["objects"] as const,
  page: (args: ObjectsQueryArgs) =>
    [...objectsKeys.all, args.bucket, args.prefix, args.startAfter] as const,
};

/** Query key for the bucket-name list that feeds the selector. */
export const bucketNamesKey = ["buckets", "names"] as const;

/**
 * Fetch the bucket names for the selector.
 *
 * Backed by `GET /admin/buckets`; we project to just the names so the selector
 * does not depend on the stats fields (which the Buckets screen owns).
 */
export function useBucketNames(): UseQueryResult<string[]> {
  return useQuery({
    queryKey: bucketNamesKey,
    queryFn: async ({ signal }): Promise<string[]> => {
      const response = await adminClient.get<BucketStats[]>("/buckets", {
        signal,
      });
      return response.data.map((bucket) => bucket.name);
    },
  });
}

/**
 * Fetch one page of the object browser for `args`.
 *
 * The query is disabled until a bucket is selected so no request fires on an
 * empty selection. `start_after` is omitted entirely for the first page (so the
 * backend lists from the beginning) and sent otherwise. Previous page data is
 * kept visible while the next page loads to avoid a flash of empty table during
 * pagination.
 */
export function useObjectsPage(args: ObjectsQueryArgs): UseQueryResult<ObjectsPage> {
  return useQuery({
    queryKey: objectsKeys.page(args),
    enabled: args.bucket.length > 0,
    placeholderData: (previous) => previous,
    queryFn: async ({ signal }): Promise<ObjectsPage> => {
      const params: Record<string, string> = {
        bucket: args.bucket,
        prefix: args.prefix,
      };
      if (args.startAfter !== null) {
        params.start_after = args.startAfter;
      }
      const response = await adminClient.get<ObjectsPage>("/objects", {
        params,
        signal,
      });
      return response.data;
    },
  });
}
