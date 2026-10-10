/**
 * Access-key data hooks (admin-dashboard Requirements 5.1–5.6, 11.3).
 *
 * Wraps the Admin API's access-key surface (design: Admin API —
 * "GET /admin/keys + POST /admin/keys + DELETE /admin/keys/{id}") in TanStack
 * Query hooks so the Access Keys panel stays presentational. The shapes mirror
 * `gateway/app/admin/router.py` exactly (snake_case JSON):
 *
 *   - `GET /admin/keys`      → `KeyEntry[]`  — list; a secret is never included.
 *   - `POST /admin/keys`     → `CreatedKey`  — create; the plaintext
 *     `secret_access_key` is returned **exactly once** here and never again
 *     (Requirement 5.2/5.3, 11.3). The caller must show it once and drop it.
 *   - `DELETE /admin/keys/{id}` → 204        — delete; a missing key is 404.
 *
 * Three hooks live here:
 *
 *   - {@link useKeys} fetches the list.
 *   - {@link useCreateKey} creates a key and invalidates the list so the new id
 *     appears; it returns the one-time secret to the caller for a single
 *     display.
 *   - {@link useDeleteKey} deletes a key and invalidates the list so the removed
 *     id disappears; a 404 (the key is already absent) is surfaced through the
 *     mutation's `error` so the panel can report it and still refresh (5.6).
 *
 * The shared `adminClient` attaches the CSRF header to the unsafe POST/DELETE
 * methods, so these mutations carry no CSRF logic themselves.
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
 * One access key as listed by `GET /admin/keys`.
 *
 * Mirrors the backend `KeyEntry` model: the secret is **never** part of a list
 * entry — only {@link CreatedKey} ever carries a plaintext secret, once
 * (Requirement 5.2).
 */
export interface KeyEntry {
  readonly access_key_id: string;
  readonly label: string | null;
  readonly disabled: boolean;
}

/** The create-access-key request body (optional operator label). */
export interface CreateKeyBody {
  readonly label?: string | null;
}

/**
 * The create-access-key response — the only place a plaintext secret appears.
 *
 * Mirrors the backend `CreatedKey` model. Per Requirement 5.3/11.3 the
 * `secret_access_key` is surfaced exactly once in this response and is never
 * stored or returned again, so the caller must display it a single time and
 * then drop it from component state.
 */
export interface CreatedKey {
  readonly access_key_id: string;
  readonly secret_access_key: string;
  readonly label: string | null;
}

/** Query key for the access-key list; stable so refetches dedupe across mounts. */
export const KEYS_QUERY_KEY = ["admin", "keys"] as const;

async function fetchKeys(): Promise<readonly KeyEntry[]> {
  const response = await adminClient.get<KeyEntry[]>("/keys");
  return response.data;
}

/** Fetch the current access keys, id + label only (Requirement 5.1). */
export function useKeys(): UseQueryResult<readonly KeyEntry[], Error> {
  return useQuery({
    queryKey: KEYS_QUERY_KEY,
    queryFn: fetchKeys,
  });
}

async function postKey(body: CreateKeyBody): Promise<CreatedKey> {
  const response = await adminClient.post<CreatedKey>("/keys", body);
  return response.data;
}

/**
 * Create an access key, invalidating the list on success so the new id appears
 * (Requirement 5.2). The resolved {@link CreatedKey} carries the one-time
 * `secret_access_key`; the caller shows it exactly once and must not persist it
 * (Requirement 5.3, 11.3). This hook deliberately never writes the secret to
 * the query cache — only the invalidated list (which has no secrets) is cached.
 */
export function useCreateKey(): UseMutationResult<CreatedKey, Error, CreateKeyBody> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: postKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY });
    },
  });
}

async function deleteKey(accessKeyId: string): Promise<void> {
  await adminClient.delete(`/keys/${encodeURIComponent(accessKeyId)}`);
}

/**
 * Delete an access key by id, invalidating the list so the removed id
 * disappears (Requirement 5.5). A delete of an already-absent key fails with
 * HTTP 404; the mutation surfaces it through `error` so the panel can show a
 * readable message, and because the list is invalidated in `onSettled` the
 * stale entry is refreshed away regardless of success or failure
 * (Requirement 5.6).
 */
export function useDeleteKey(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteKey,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY });
    },
  });
}
