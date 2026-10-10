/**
 * System Health data hooks (admin-dashboard Requirements 7.1–7.6).
 *
 * Wraps the gateway's observability surface — the readiness probe
 * `GET /health/ready` and the Prometheus scrape target `GET /metrics` — in
 * TanStack Query hooks so the System Health panel stays presentational.
 *
 * Readiness is derived from the HTTP **status**, not from an assumed JSON body
 * (design: TypeScript API payload types — "The health panel derives `Readiness`
 * from HTTP status (200 → ready, 503 → not ready, network/timeout → unknown)
 * rather than assuming a specific JSON body"):
 *
 *   - `200` → `"ready"`     (Requirement 7.2)
 *   - `503` → `"not_ready"` (Requirement 7.3)
 *   - network error / timeout / any other status → `"unknown"` (Requirement 7.4)
 *
 * Dependency health (PostgreSQL + Telegram, Requirement 7.5) is read from the
 * `503` body when present: `gateway/app/admin/health.py` returns
 * `{"unhealthy": [...]}` naming the failed dependencies with the fixed strings
 * `"postgresql"` and `"telegram"`. On a `200` both dependencies are healthy; on
 * an `unknown` outcome neither can be asserted, so both read `false`. Deriving
 * dependency state from the `unhealthy` list (rather than assuming a positive
 * body) keeps the hook correct regardless of the exact ready-state payload.
 *
 * The readiness query polls on a ≤ 10 s interval while the panel is mounted
 * (Requirement 7.1). The metrics fetch is on demand — a mutation the operator
 * triggers from a control (Requirement 7.6) — so the raw Prometheus text is
 * only pulled when asked for, never polled.
 *
 * These endpoints are reached through {@link observabilityClient}, which targets
 * the admin app root (`/health`, `/metrics` are **not** under `/admin`) and
 * resolves every status so a `503` is observed rather than thrown.
 */
import {
  useMutation,
  useQuery,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import axios from "axios";

import { observabilityClient } from "./client";

/**
 * Derived readiness of the gateway (design: TypeScript API payload types).
 *
 *   - `"ready"`     — `/health/ready` returned `200`.
 *   - `"not_ready"` — `/health/ready` returned `503`.
 *   - `"unknown"`   — the probe could not be completed (network error, timeout,
 *     or an unexpected status), so readiness cannot be asserted either way.
 */
export type Readiness = "ready" | "not_ready" | "unknown";

/**
 * Health of the two dependencies the gateway's request path needs, mirroring
 * the readiness probe's checks (`gateway/app/admin/health.py`): PostgreSQL (the
 * index/cache) and the single Telegram MTProto session (durable truth).
 */
export interface DependencyHealth {
  readonly postgres: boolean;
  readonly telegram: boolean;
}

/** The derived readiness snapshot the System Health panel renders. */
export interface HealthSnapshot {
  readonly readiness: Readiness;
  readonly dependencies: DependencyHealth;
}

/**
 * Fixed dependency names the readiness probe reports in its `503`
 * `{"unhealthy": [...]}` body (`gateway/app/admin/health.py`:
 * `DEPENDENCY_POSTGRESQL` / `DEPENDENCY_TELEGRAM`). Matched to derive which
 * dependency is unhealthy on a not-ready response (Requirement 7.5).
 */
const DEPENDENCY_POSTGRESQL = "postgresql";
const DEPENDENCY_TELEGRAM = "telegram";

/** Path of the readiness probe, relative to the observability base URL. */
export const READY_ENDPOINT = "/health/ready";

/** Path of the Prometheus scrape target, relative to the observability base URL. */
export const METRICS_ENDPOINT = "/metrics";

/**
 * Readiness poll interval in milliseconds. The panel must poll "at least every
 * 10 s while displayed" (Requirement 7.1); 10 000 ms satisfies the ceiling.
 */
export const READINESS_POLL_INTERVAL_MS = 10_000;

/** Query key for the readiness probe; stable so refetches dedupe across mounts. */
export const READINESS_QUERY_KEY = ["gateway", "health", "ready"] as const;

/**
 * Read the `unhealthy` dependency list from a readiness response body, matching
 * the backend's fixed names. Returns which of the two dependencies the probe
 * reported as down; unknown entries are ignored. Defensive against a missing or
 * malformed body (the design pins the status, not the body shape).
 */
function unhealthyFromBody(data: unknown): { postgres: boolean; telegram: boolean } {
  const unhealthy =
    typeof data === "object" &&
    data !== null &&
    Array.isArray((data as { unhealthy?: unknown }).unhealthy)
      ? ((data as { unhealthy: unknown[] }).unhealthy as unknown[])
      : [];
  return {
    postgres: unhealthy.includes(DEPENDENCY_POSTGRESQL),
    telegram: unhealthy.includes(DEPENDENCY_TELEGRAM),
  };
}

/**
 * Derive the health snapshot from an HTTP status and (for a 503) its body.
 *
 *   - `200` → ready, both dependencies healthy (Requirement 7.2).
 *   - `503` → not ready; a dependency is unhealthy iff it is named in the body's
 *     `unhealthy` list (Requirement 7.3, 7.5).
 *   - anything else → unknown; neither dependency can be asserted
 *     (Requirement 7.4).
 */
export function deriveHealth(status: number, data: unknown): HealthSnapshot {
  if (status === 200) {
    return { readiness: "ready", dependencies: { postgres: true, telegram: true } };
  }
  if (status === 503) {
    const down = unhealthyFromBody(data);
    return {
      readiness: "not_ready",
      dependencies: { postgres: !down.postgres, telegram: !down.telegram },
    };
  }
  return { readiness: "unknown", dependencies: { postgres: false, telegram: false } };
}

/** Health snapshot used when the probe could not be completed at all. */
const UNKNOWN_SNAPSHOT: HealthSnapshot = {
  readiness: "unknown",
  dependencies: { postgres: false, telegram: false },
};

/**
 * Fetch and derive the readiness snapshot once.
 *
 * {@link observabilityClient} resolves every HTTP status (`validateStatus`
 * returns true), so a `503` lands here as a normal response and is derived to
 * `not_ready` rather than thrown. A genuine failure — network down or the 30 s
 * timeout — surfaces as a thrown axios error, which maps to `unknown` so the
 * panel degrades gracefully instead of erroring out (Requirement 7.4).
 */
async function fetchReadiness(): Promise<HealthSnapshot> {
  try {
    const response = await observabilityClient.get(READY_ENDPOINT);
    return deriveHealth(response.status, response.data);
  } catch (error) {
    if (axios.isAxiosError(error)) {
      // A response with an unexpected status (shouldn't happen with
      // validateStatus) still derives; a transport failure has no response.
      if (error.response !== undefined) {
        return deriveHealth(error.response.status, error.response.data);
      }
      return UNKNOWN_SNAPSHOT;
    }
    throw error;
  }
}

/**
 * Poll the gateway readiness while the System Health panel is mounted
 * (Requirement 7.1), returning the derived {@link HealthSnapshot}.
 *
 * The query refetches on a 10 s interval and also while the window is in the
 * background so the displayed state never goes stale beyond the ceiling. The
 * query itself never rejects — `fetchReadiness` maps every outcome, including a
 * transport failure, onto a snapshot — so the panel always has a value to show.
 */
export function useReadiness(): UseQueryResult<HealthSnapshot, Error> {
  return useQuery({
    queryKey: READINESS_QUERY_KEY,
    queryFn: fetchReadiness,
    refetchInterval: READINESS_POLL_INTERVAL_MS,
    refetchIntervalInBackground: true,
  });
}

/**
 * Fetch the raw Prometheus exposition text from `GET /metrics`.
 *
 * The endpoint returns `text/plain`, so the response is requested and returned
 * as a string for verbatim display. A non-200 is surfaced as an error through
 * the mutation so the control can report it.
 */
async function fetchMetrics(): Promise<string> {
  const response = await observabilityClient.get<string>(METRICS_ENDPOINT, {
    responseType: "text",
    headers: { Accept: "text/plain" },
  });
  if (response.status !== 200) {
    throw new Error(`Metrics endpoint returned HTTP ${response.status}.`);
  }
  return typeof response.data === "string" ? response.data : String(response.data);
}

/**
 * On-demand fetch of the Prometheus `/metrics` text (Requirement 7.6).
 *
 * Exposed as a mutation rather than a polling query because the metrics dump is
 * large and only wanted when the operator explicitly asks for it: the panel
 * wires a control to {@link UseMutationResult.mutate} and renders the resolved
 * text (or the error) from the mutation state.
 */
export function useMetrics(): UseMutationResult<string, Error, void> {
  return useMutation({
    mutationFn: fetchMetrics,
  });
}
