/**
 * Health status derivation (admin-dashboard task 10.8; Requirements 7.2–7.5).
 *
 * Exercises the pure {@link deriveHealth} helper over the status → snapshot
 * matrix the design pins (design: TypeScript API payload types — "The health
 * panel derives `Readiness` from HTTP status (200 → ready, 503 → not ready,
 * network/timeout → unknown) rather than assuming a specific JSON body"):
 *
 *  - HTTP 200 → ready, both PostgreSQL and Telegram healthy (Req 7.2);
 *  - HTTP 503 with `postgresql` in the `unhealthy` list → not ready, PostgreSQL
 *    unhealthy, Telegram healthy (Req 7.3);
 *  - HTTP 503 with `telegram` in the `unhealthy` list → not ready, Telegram
 *    unhealthy, PostgreSQL healthy (Req 7.4);
 *  - any other status (modelling a network error / timeout with no response) →
 *    unknown, neither dependency asserted (Req 7.5).
 *
 * Mirrors the real `{"unhealthy": [...]}` body shape from
 * `gateway/app/admin/health.py` (`DEPENDENCY_POSTGRESQL` / `DEPENDENCY_TELEGRAM`).
 * Pure derivation — no backend, no React — so the matrix is fast and exhaustive.
 *
 * Validates: Requirements 7.2, 7.3, 7.4, 7.5.
 */
import { describe, expect, it } from "vitest";

import { deriveHealth, type HealthSnapshot } from "./health";

describe("deriveHealth — HTTP status → readiness snapshot", () => {
  it("HTTP 200 → ready with both dependencies healthy (Req 7.2)", () => {
    const snapshot = deriveHealth(200, { status: "ready" });
    expect(snapshot).toEqual<HealthSnapshot>({
      readiness: "ready",
      dependencies: { postgres: true, telegram: true },
    });
  });

  it("ignores an unexpected 200 body and still reports both healthy (Req 7.2)", () => {
    // Derivation is pinned to the status, not the body shape.
    const snapshot = deriveHealth(200, undefined);
    expect(snapshot.readiness).toBe("ready");
    expect(snapshot.dependencies).toEqual({ postgres: true, telegram: true });
  });

  it("HTTP 503 with postgresql unhealthy → not ready, PostgreSQL down (Req 7.3)", () => {
    const snapshot = deriveHealth(503, { unhealthy: ["postgresql"] });
    expect(snapshot).toEqual<HealthSnapshot>({
      readiness: "not_ready",
      dependencies: { postgres: false, telegram: true },
    });
  });

  it("HTTP 503 with telegram unhealthy → not ready, Telegram down (Req 7.4)", () => {
    const snapshot = deriveHealth(503, { unhealthy: ["telegram"] });
    expect(snapshot).toEqual<HealthSnapshot>({
      readiness: "not_ready",
      dependencies: { postgres: true, telegram: false },
    });
  });

  it("HTTP 503 with both dependencies unhealthy → not ready, both down", () => {
    const snapshot = deriveHealth(503, { unhealthy: ["postgresql", "telegram"] });
    expect(snapshot).toEqual<HealthSnapshot>({
      readiness: "not_ready",
      dependencies: { postgres: false, telegram: false },
    });
  });

  it("HTTP 503 with a missing/malformed body → not ready, both read healthy", () => {
    // The design pins the status, not the body; absent an `unhealthy` list no
    // dependency is named as down, so both default to healthy on a 503.
    for (const body of [undefined, null, {}, { unhealthy: "nope" }, "503"]) {
      const snapshot = deriveHealth(503, body);
      expect(snapshot.readiness).toBe("not_ready");
      expect(snapshot.dependencies).toEqual({ postgres: true, telegram: true });
    }
  });

  it("ignores unknown entries in the unhealthy list", () => {
    const snapshot = deriveHealth(503, { unhealthy: ["redis", "postgresql"] });
    expect(snapshot.dependencies).toEqual({ postgres: false, telegram: true });
  });

  it("any non-200/503 status → unknown, neither dependency asserted (Req 7.5)", () => {
    // A network error / timeout surfaces no HTTP status; the hook maps that to
    // an unknown snapshot, and any unexpected status derives the same way here.
    for (const status of [0, 204, 301, 404, 500, 502, 504]) {
      const snapshot = deriveHealth(status, undefined);
      expect(snapshot).toEqual<HealthSnapshot>({
        readiness: "unknown",
        dependencies: { postgres: false, telegram: false },
      });
    }
  });
});
