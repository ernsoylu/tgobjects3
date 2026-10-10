/**
 * Centralised Admin API error → operator-readable message mapping.
 *
 * The design's "Admin API status → Dashboard behaviour" table
 * (`.kiro/specs/admin-dashboard/design.md`, Error Handling) is the authoritative
 * contract. This module owns the status→message half of that table so every
 * screen and data hook renders failures the same way instead of each inventing
 * its own axios-error parsing:
 *
 *   - **401** — handled out of band by the client's global unauthorized hook
 *     (ends the session + redirects to login, task 4.2); callers should not
 *     render a 401 as an inline message. `describeAdminError` still returns a
 *     sensible string for completeness.
 *   - **403 (CSRF)** — a transient CSRF failure the client retries once after a
 *     bootstrap refresh (see `client.ts`). A 403 that reaches a caller means the
 *     retry also failed (Requirement 11.6), so it maps to a readable message.
 *   - **409 / 422 / 503** — stay on screen with a message derived from the
 *     response `detail`. A 422 carries FastAPI's validation-error array, which
 *     is flattened to `field: message` so the offending field and its accepted
 *     range are named (Requirements 12.2, 12.6).
 *   - **other 5xx** — "unexpected gateway error"; the request must not be
 *     presented as having succeeded (Requirement 12.4).
 *   - **network error / > 30 s timeout** — "gateway could not be reached"
 *     (axios timeout is 30 000 ms, Requirement 12.3); callers pair this with a
 *     retry affordance.
 *
 * Requirements 12.1, 12.2, 12.3, 12.4, 12.6, 11.6.
 */
import axios, { type AxiosError } from "axios";

/** Message shown when the gateway cannot be reached (network down / timeout). */
export const GATEWAY_UNREACHABLE_MESSAGE = "The gateway could not be reached.";

/** Message shown for an unexpected 5xx that is not a recognised 409/422/503. */
export const UNEXPECTED_GATEWAY_ERROR_MESSAGE = "Unexpected gateway error.";

/** Message shown when a CSRF 403 survives the client's bootstrap-and-retry. */
export const CSRF_FAILED_MESSAGE =
  "Your session's security token is no longer valid. Please retry.";

/** Fallback when a thrown value carries no usable detail. */
const UNKNOWN_ERROR_MESSAGE = "Unknown error.";

/** One entry of FastAPI's 422 validation-error array (`detail: [...]`). */
interface ValidationErrorItem {
  readonly loc?: unknown;
  readonly msg?: unknown;
}

/**
 * The categories of failure a caller may branch on (e.g. a 503 during setup
 * preserves credentials and offers retry; a network error offers retry). The
 * message string is always suitable for direct display.
 */
export type AdminErrorKind =
  | "unauthorized"
  | "csrf"
  | "conflict"
  | "validation"
  | "unavailable"
  | "server"
  | "network"
  | "unknown";

/** A classified, display-ready description of an Admin API failure. */
export interface AdminErrorInfo {
  /** Which row of the status→behaviour table this failure fell on. */
  readonly kind: AdminErrorKind;
  /** HTTP status, when the request got a response; `null` for network errors. */
  readonly status: number | null;
  /** Operator-readable message, safe to render directly. */
  readonly message: string;
  /**
   * Whether the UI should offer a retry affordance. True for transient faults
   * (network/timeout, unavailable); false for terminal/validation failures the
   * operator must act on (Requirements 12.2, 12.3).
   */
  readonly retryable: boolean;
}

/** Extract the `detail` field from an Admin API JSON error body, if present. */
function responseDetail(error: AxiosError): unknown {
  const data = error.response?.data as { detail?: unknown } | undefined;
  return data?.detail;
}

/**
 * Flatten FastAPI's 422 validation-error array into a readable, field-named
 * message. Each item's `loc` tail is the field; its `msg` carries the
 * constraint (e.g. the accepted range), so `field: msg` names both
 * (Requirements 12.2, 12.6). Returns `null` when nothing usable is present.
 */
function describeValidationDetail(detail: readonly ValidationErrorItem[]): string | null {
  const messages = detail
    .map((item) => {
      const text = typeof item.msg === "string" && item.msg.length > 0 ? item.msg : "invalid value";
      const loc = Array.isArray(item.loc) ? item.loc : undefined;
      // FastAPI prefixes body fields with "body"; the last segment is the field.
      const field = loc !== undefined && loc.length > 0 ? loc[loc.length - 1] : undefined;
      return typeof field === "string" && field.length > 0 ? `${field}: ${text}` : text;
    })
    .filter((text) => text.length > 0);
  return messages.length > 0 ? messages.join("; ") : null;
}

/**
 * Classify and describe an Admin API failure per the design's status→behaviour
 * table. Accepts any thrown value; non-axios errors fall through to a readable
 * message so callers never have to re-parse.
 */
export function describeAdminError(error: unknown): AdminErrorInfo {
  if (axios.isAxiosError(error)) {
    const status = error.response?.status ?? null;

    // No response at all: network failure or the 30 s timeout tripped
    // (Requirement 12.3).
    if (status === null) {
      return {
        kind: "network",
        status: null,
        message: GATEWAY_UNREACHABLE_MESSAGE,
        retryable: true,
      };
    }

    if (status === 401) {
      // Terminal here; the global handler ends the session separately.
      return {
        kind: "unauthorized",
        status,
        message: "Your session is no longer valid.",
        retryable: false,
      };
    }

    if (status === 403) {
      // Reaching a caller means the client's bootstrap-and-retry also failed
      // (Requirement 11.6).
      return { kind: "csrf", status, message: CSRF_FAILED_MESSAGE, retryable: false };
    }

    if (status === 409 || status === 422 || status === 503) {
      const detail = responseDetail(error);
      if (status === 422 && Array.isArray(detail)) {
        const described = describeValidationDetail(detail as ValidationErrorItem[]);
        if (described !== null) {
          return { kind: "validation", status, message: described, retryable: false };
        }
      }
      if (typeof detail === "string" && detail.length > 0) {
        return {
          kind: status === 503 ? "unavailable" : status === 422 ? "validation" : "conflict",
          status,
          message: detail,
          retryable: status === 503,
        };
      }
      // A recognised status with no usable detail still stays on screen.
      return {
        kind: status === 503 ? "unavailable" : status === 422 ? "validation" : "conflict",
        status,
        message: status === 503 ? "The gateway is temporarily unavailable." : error.message,
        retryable: status === 503,
      };
    }

    // Any other 5xx: do not present as success (Requirement 12.4).
    if (status >= 500) {
      return {
        kind: "server",
        status,
        message: UNEXPECTED_GATEWAY_ERROR_MESSAGE,
        retryable: false,
      };
    }

    // Any remaining 4xx: surface the detail if present, else the axios message.
    const detail = responseDetail(error);
    if (typeof detail === "string" && detail.length > 0) {
      return { kind: "unknown", status, message: detail, retryable: false };
    }
    return { kind: "unknown", status, message: error.message, retryable: false };
  }

  if (error instanceof Error && error.message.length > 0) {
    return { kind: "unknown", status: null, message: error.message, retryable: false };
  }
  return { kind: "unknown", status: null, message: UNKNOWN_ERROR_MESSAGE, retryable: false };
}

/**
 * Convenience wrapper returning just the operator-readable message. Screens and
 * hooks that only need the string (rather than the kind/retryable fields) use
 * this as the single replacement for ad-hoc per-screen `errorMessage` helpers.
 */
export function errorMessage(error: unknown): string {
  return describeAdminError(error).message;
}
