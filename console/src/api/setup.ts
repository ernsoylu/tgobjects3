/**
 * Setup-status data hook (admin-dashboard Requirements 3.1, 3.12, 12.5).
 *
 * The gateway exposes a lightweight probe the Console uses to decide whether the
 * dashboard must divert into the first-run Setup Wizard. The design pins the
 * endpoint and shape (design: Admin API — "`POST /admin/setup/status` → —
 * `SetupStatus{setup_required}`"; TypeScript API payload types —
 * `interface SetupStatus { setup_required: boolean; }`): a POST with no body
 * returning a single boolean.
 *
 * The route guard (`RequireSetup` in `App.tsx`) consumes {@link useSetupStatus}
 * on entry to a guarded admin route; while `setup_required` is true it redirects
 * to `/setup` (design: Error Handling — "Setup-required routing"). The
 * finalize-success transition (task 8.4) invalidates {@link SETUP_STATUS_QUERY_KEY}
 * so the next read reports `false` and navigation reaches the admin screens.
 *
 * The shared `adminClient` attaches `Authorization` and, on this unsafe POST,
 * the cached `X-CSRF-Token`, so this hook carries no auth/CSRF logic itself.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { adminClient } from "./client";

/**
 * Response of `POST /admin/setup/status`. Mirrors the backend `SetupStatus`
 * model exactly (snake_case JSON): a single flag that is true until the gateway
 * has completed first-run setup.
 */
export interface SetupStatus {
  readonly setup_required: boolean;
}

/** Path of the setup-status probe, relative to the Admin API base URL. */
export const SETUP_STATUS_ENDPOINT = "/setup/status";

/** Query key for the setup-status probe; stable so refetches dedupe across mounts. */
export const SETUP_STATUS_QUERY_KEY = ["admin", "setup", "status"] as const;

async function fetchSetupStatus(): Promise<SetupStatus> {
  const response = await adminClient.post<SetupStatus>(SETUP_STATUS_ENDPOINT);
  return response.data;
}

/**
 * Fetch whether first-run setup is still required (Requirements 3.1, 3.12).
 *
 * The guard treats the in-flight state as "undecided" and renders nothing until
 * the probe resolves, so an authenticated operator is never flashed the admin
 * screens before the setup gate is known.
 */
export function useSetupStatus(): UseQueryResult<SetupStatus, Error> {
  return useQuery({
    queryKey: SETUP_STATUS_QUERY_KEY,
    queryFn: fetchSetupStatus,
  });
}
