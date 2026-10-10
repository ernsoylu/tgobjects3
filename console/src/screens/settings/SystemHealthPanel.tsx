/**
 * System Health settings panel (admin-dashboard Requirements 7.1–7.7).
 *
 * The System Health tab of {@link SettingsScreen}. It wraps the observability
 * hooks in `../../api/health` and renders:
 *
 *   - **Readiness (7.1–7.4)** — the derived readiness state from
 *     {@link useReadiness}, which polls `GET /health/ready` on a ≤ 10 s interval
 *     while this panel is mounted. Readiness is derived from the HTTP status:
 *     `200` → ready, `503` → not ready, network/timeout/other → unknown.
 *   - **Dependency health (7.5)** — PostgreSQL and the Telegram session shown
 *     individually. On a not-ready response the specific unhealthy dependency
 *     is named from the probe body.
 *   - **Setup-required pending (7.7)** — while the gateway reports
 *     `setup_required = true` ({@link useSetupStatus}) the gateway has no
 *     meaningful readiness yet, so the panel shows "readiness pending" instead
 *     of a possibly-misleading not-ready/unknown state.
 *   - **Metrics (7.6)** — a control that fetches the Prometheus `/metrics` text
 *     on demand ({@link useMetrics}) and presents it verbatim.
 *
 * Status is never conveyed by hue alone (Requirement 10.4/10.5): every state
 * carries a text label and a glyph (✓ / ✕ / • / …) alongside its colour, and
 * the readiness region is an `aria-live` status so a poll transition is
 * announced without moving focus.
 */
import { errorMessage } from "../../api/errors";
import { useMetrics, useReadiness, type HealthSnapshot, type Readiness } from "../../api/health";
import { useSetupStatus } from "../../api/setup";

/** Presentation metadata for a readiness state: glyph, label, badge modifier. */
interface ReadinessView {
  readonly glyph: string;
  readonly label: string;
  readonly badgeModifier: "pass" | "fail" | "warn";
  readonly description: string;
}

/**
 * Map a derived readiness onto its presentation. The glyph + label carry the
 * status so it is legible without colour (Requirement 10.4).
 */
function readinessView(readiness: Readiness): ReadinessView {
  switch (readiness) {
    case "ready":
      return {
        glyph: "✓",
        label: "Ready",
        badgeModifier: "pass",
        description: "The gateway reports it can serve traffic.",
      };
    case "not_ready":
      return {
        glyph: "✕",
        label: "Not ready",
        badgeModifier: "fail",
        description: "The gateway reported one or more dependencies as unavailable.",
      };
    case "unknown":
      return {
        glyph: "•",
        label: "Unknown",
        badgeModifier: "warn",
        description: "The readiness probe could not be reached. Retrying automatically.",
      };
  }
}

/** A single dependency health row, labelled and glyphed (not hue-only). */
function DependencyRow(props: { name: string; healthy: boolean; known: boolean }): JSX.Element {
  const { name, healthy, known } = props;
  // When readiness is unknown we cannot assert a dependency either way, so show
  // a neutral "unknown" rather than a false "unhealthy".
  const modifier = !known ? "warn" : healthy ? "pass" : "fail";
  const glyph = !known ? "•" : healthy ? "✓" : "✕";
  const text = !known ? "Unknown" : healthy ? "Healthy" : "Unavailable";
  return (
    <tr>
      <td>{name}</td>
      <td>
        <span className={`recovery-badge recovery-badge--${modifier}`}>
          <span aria-hidden="true">{glyph} </span>
          {text}
        </span>
      </td>
    </tr>
  );
}

/** The System Health panel body. */
export function SystemHealthPanel(): JSX.Element {
  const setup = useSetupStatus();
  const readiness = useReadiness();
  const metrics = useMetrics();

  // Setup-required gate (Requirement 7.7): the gateway is still awaiting
  // first-run setup, so readiness is not yet meaningful — show it as pending.
  const setupRequired = setup.data?.setup_required === true;

  const snapshot: HealthSnapshot | undefined = readiness.data;
  const view = snapshot ? readinessView(snapshot.readiness) : null;
  // Dependency health is only "known" on a definite ready/not-ready outcome.
  const depsKnown = snapshot !== undefined && snapshot.readiness !== "unknown";

  return (
    <div className="recovery-panel">
      <h2 className="recovery-subtitle">System Health</h2>
      <p className="recovery-description">
        Live readiness of the gateway and its dependencies, polled continuously while this tab is
        open. Fetch the raw Prometheus metrics on demand below.
      </p>

      <section className="health-section" aria-labelledby="health-readiness-heading">
        <h3 className="recovery-subtitle" id="health-readiness-heading">
          Readiness
        </h3>

        <div className="health-readiness" role="status" aria-live="polite">
          {setupRequired ? (
            <span className="recovery-badge recovery-badge--warn">
              <span aria-hidden="true">… </span>
              Readiness pending — first-run setup not complete
            </span>
          ) : readiness.isPending || !view ? (
            <span className="recovery-badge recovery-badge--warn">
              <span aria-hidden="true">… </span>
              Checking…
            </span>
          ) : (
            <>
              <span className={`recovery-badge recovery-badge--${view.badgeModifier}`}>
                <span aria-hidden="true">{view.glyph} </span>
                {view.label}
              </span>
              <p className="recovery-description">{view.description}</p>
            </>
          )}
        </div>
      </section>

      {!setupRequired ? (
        <section className="health-section" aria-labelledby="health-deps-heading">
          <h3 className="recovery-subtitle" id="health-deps-heading">
            Dependencies
          </h3>
          <table className="recovery-table">
            <thead>
              <tr>
                <th>Dependency</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              <DependencyRow
                name="PostgreSQL"
                healthy={snapshot?.dependencies.postgres ?? false}
                known={depsKnown}
              />
              <DependencyRow
                name="Telegram session"
                healthy={snapshot?.dependencies.telegram ?? false}
                known={depsKnown}
              />
            </tbody>
          </table>
        </section>
      ) : null}

      <section className="health-section" aria-labelledby="health-metrics-heading">
        <h3 className="recovery-subtitle" id="health-metrics-heading">
          Metrics
        </h3>
        <p className="recovery-description">
          Fetch the current Prometheus exposition text from <code>/metrics</code>.
        </p>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => metrics.mutate()}
          disabled={metrics.isPending}
        >
          {metrics.isPending ? "Fetching…" : "Fetch metrics"}
        </button>

        {metrics.isError ? (
          <p className="settings-error" role="alert">
            Could not fetch metrics: {errorMessage(metrics.error)}
          </p>
        ) : null}

        {metrics.data !== undefined ? (
          <pre className="health-metrics" aria-label="Prometheus metrics">
            {metrics.data}
          </pre>
        ) : null}
      </section>
    </div>
  );
}
