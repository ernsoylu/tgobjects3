/**
 * Recovery Status screen (Requirement 19.4).
 *
 * Shows the gateway's recovery posture so an operator can confirm the index can
 * be rebuilt from Telegram at any time:
 *
 *   - the timestamp (UTC, second precision) of the last completed Checkpoint,
 *     or "None" when no Checkpoint has been written yet;
 *   - the outcome (pass / fail) of the most recent recovery drill, or "None";
 *   - the per-channel high-water marks the last Checkpoint recorded, which bound
 *     how much each channel must be re-scanned during recovery;
 *   - a description of the Recovery Kit — the offline bundle an operator needs
 *     to rebuild from scratch.
 *
 * Data comes from {@link useRecoveryStatus}, which targets the expected
 * `GET /admin/recovery` endpoint. That endpoint is not implemented in the Admin
 * API yet (see `api/recovery.ts` for the backend-dependency note); when it is
 * absent the hook resolves to `null` and this screen renders an informative
 * "endpoint not available" panel instead of crashing. The query auto-refreshes
 * on an interval via TanStack Query's `refetchInterval`.
 */
import * as Tabs from "@radix-ui/react-tabs";

import {
  RECOVERY_ENDPOINT,
  useRecoveryStatus,
  type DrillOutcome,
  type HighWaterMark,
  type RecoveryStatus,
} from "../api/recovery";

/** Placeholder shown wherever a value is absent (Requirement 19.4: "None"). */
const NONE = "None";

/**
 * Format an ISO-8601 timestamp as UTC to second precision (Requirement 19.4).
 *
 * Returns {@link NONE} for a `null`/absent value. Renders as
 * `YYYY-MM-DD HH:MM:SS UTC` so the display is unambiguous regardless of the
 * operator's local timezone. An unparseable value falls back to the raw string
 * rather than throwing.
 */
function formatUtcSeconds(iso: string | null): string {
  if (iso === null) {
    return NONE;
  }
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  const pad = (n: number): string => String(n).padStart(2, "0");
  const date = `${parsed.getUTCFullYear()}-${pad(parsed.getUTCMonth() + 1)}-${pad(
    parsed.getUTCDate(),
  )}`;
  const time = `${pad(parsed.getUTCHours())}:${pad(parsed.getUTCMinutes())}:${pad(
    parsed.getUTCSeconds(),
  )}`;
  return `${date} ${time} UTC`;
}

/** Render the drill outcome as a coloured badge, or "None" when absent. */
function DrillBadge(props: { outcome: DrillOutcome | null }): JSX.Element {
  if (props.outcome === null) {
    return <span className="recovery-value-muted">{NONE}</span>;
  }
  const modifier = props.outcome === "pass" ? "recovery-badge--pass" : "recovery-badge--fail";
  return (
    <span className={`recovery-badge ${modifier}`}>
      {props.outcome === "pass" ? "Pass" : "Fail"}
    </span>
  );
}

/** The per-channel high-water marks table, with an empty-state row. */
function HighWaterTable(props: { marks: readonly HighWaterMark[] }): JSX.Element {
  if (props.marks.length === 0) {
    return <p className="recovery-value-muted">No high-water marks recorded yet.</p>;
  }
  return (
    <table className="recovery-table">
      <thead>
        <tr>
          <th>Channel</th>
          <th>Highest confirmed message id</th>
        </tr>
      </thead>
      <tbody>
        {props.marks.map((mark) => (
          <tr key={mark.channel_id}>
            <td>{mark.channel_id}</td>
            <td>{mark.message_id.toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The "Status" tab body: checkpoint time, drill outcome, high-water marks. */
function StatusPanel(props: { status: RecoveryStatus }): JSX.Element {
  const { status } = props;
  const channelsCovered = status.high_water_marks.length;
  return (
    <div className="recovery-panel">
      <dl className="recovery-facts">
        <div className="recovery-fact">
          <dt>Last completed Checkpoint</dt>
          <dd>{formatUtcSeconds(status.last_checkpoint_at)}</dd>
        </div>
        <div className="recovery-fact">
          <dt>Last Checkpoint message id</dt>
          <dd>
            {status.last_checkpoint_message_id === null ? (
              <span className="recovery-value-muted">{NONE}</span>
            ) : (
              status.last_checkpoint_message_id.toLocaleString()
            )}
          </dd>
        </div>
        <div className="recovery-fact">
          <dt>Last recovery drill</dt>
          <dd>
            <DrillBadge outcome={status.last_drill ? status.last_drill.outcome : null} />
            {status.last_drill ? (
              <span className="recovery-fact-sub">
                {formatUtcSeconds(status.last_drill.completed_at)}
              </span>
            ) : null}
          </dd>
        </div>
        <div className="recovery-fact">
          <dt>Channels covered</dt>
          <dd>{channelsCovered}</dd>
        </div>
      </dl>

      <h2 className="recovery-subtitle">Per-channel high-water marks</h2>
      <p className="recovery-description">
        A recovery scan replays only the messages newer than these marks, so they bound the work
        needed to rebuild the index from Telegram.
      </p>
      <HighWaterTable marks={status.high_water_marks} />
    </div>
  );
}

/** The "Recovery Kit" tab body: static operator guidance. */
function RecoveryKitPanel(): JSX.Element {
  return (
    <div className="recovery-panel">
      <p className="recovery-description">
        The Recovery Kit is the offline bundle an operator needs to rebuild this gateway from
        Telegram alone, without the PostgreSQL cache. The database is a rebuildable index; durable
        truth lives in the Telegram channels.
      </p>
      <ul className="recovery-kit-list">
        <li>System channel id — where Checkpoints and the bucket manifest live.</li>
        <li>Per-bucket channel ids — where each bucket's object history lives.</li>
        <li>Master key — unwraps the per-bucket keys and decrypts records.</li>
        <li>
          Latest Checkpoint metadata — lets the recovery CLI restore the index dump and scan only
          messages past the high-water marks.
        </li>
      </ul>
      <p className="recovery-description">
        The Kit is generated by the guided setup wizard and should be stored offline. To rebuild,
        run the recovery CLI (<code>cli/rebuild.py</code>) with the Kit's values.
      </p>
    </div>
  );
}

/** Recovery Status screen (Requirement 19.4). */
export function RecoveryScreen(): JSX.Element {
  const query = useRecoveryStatus();

  return (
    <section className="screen">
      <h1 className="screen-title">Recovery</h1>
      <p className="screen-description">
        Checkpoint freshness, per-channel high-water marks, and recovery readiness. Auto-refreshes
        periodically.
      </p>

      <Tabs.Root className="recovery-tabs" defaultValue="status">
        <Tabs.List className="recovery-tablist" aria-label="Recovery views">
          <Tabs.Trigger className="recovery-tab" value="status">
            Status
          </Tabs.Trigger>
          <Tabs.Trigger className="recovery-tab" value="kit">
            Recovery Kit
          </Tabs.Trigger>
        </Tabs.List>

        <Tabs.Content value="status">
          {query.isPending ? (
            <p className="recovery-state">Loading recovery status…</p>
          ) : query.isError ? (
            <p className="recovery-state recovery-state--error">
              Failed to load recovery status: {query.error.message}
            </p>
          ) : query.data === null ? (
            <div className="recovery-panel">
              <p className="recovery-state recovery-state--warn">
                Recovery status endpoint not available.
              </p>
              <p className="recovery-description">
                The Admin API does not yet expose <code>{RECOVERY_ENDPOINT}</code>. Once the backend
                adds it, this screen will show the last Checkpoint time, high-water marks, and the
                most recent recovery drill outcome automatically.
              </p>
            </div>
          ) : (
            <StatusPanel status={query.data} />
          )}
        </Tabs.Content>

        <Tabs.Content value="kit">
          <RecoveryKitPanel />
        </Tabs.Content>
      </Tabs.Root>
    </section>
  );
}
