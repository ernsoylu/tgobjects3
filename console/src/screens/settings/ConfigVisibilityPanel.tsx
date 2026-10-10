/**
 * Configuration Visibility settings panel (admin-dashboard Requirements 6.2–6.4, 11.1).
 *
 * The Configuration Visibility tab of {@link SettingsScreen}. It wraps the
 * read-only environment-configuration hook in `../../api/config` and renders
 * two read-only sections:
 *
 *   - **Environment settings (6.2/6.4)** — the non-secret Environment_Settings
 *     as a read-only name/value table. Each row is explicitly labelled as
 *     requiring a redeploy or restart to change, and there is deliberately **no
 *     editing control** anywhere in this panel — unlike the Runtime Settings
 *     tab, these values cannot be changed at runtime (Requirement 6.4).
 *   - **Secret settings (6.3/11.1)** — the secret Environment_Settings (the
 *     master key, the database URL with its embedded credentials) shown only as
 *     "Set" / "Not set". The backend never sends the secret value and this
 *     panel never renders one; the status is a label, not a hue, so it is not
 *     conveyed by colour alone (Requirement 10.5).
 *
 * The panel is purely presentational: all state comes from the `useConfig`
 * query. Loading, error (with retry), and empty states reuse the shared
 * `.recovery-*`/`.screen-*` styling so this tab matches the Access Keys and
 * Runtime Settings panels.
 */
import { errorMessage } from "../../api/errors";
import { useConfig } from "../../api/config";

/**
 * The Configuration Visibility panel body.
 *
 * Fetches the read-only environment configuration via {@link useConfig} and
 * renders the non-secret settings and the set/not-set secret flags. No value is
 * editable and no secret value is ever displayed (Requirements 6.2–6.4, 11.1).
 */
export function ConfigVisibilityPanel(): JSX.Element {
  const config = useConfig();

  if (config.isError) {
    return (
      <div className="recovery-panel">
        <div className="screen-error" role="alert">
          <p>Could not load configuration: {errorMessage(config.error)}</p>
          <button type="button" className="btn" onClick={() => void config.refetch()}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (config.isPending) {
    return (
      <div className="recovery-panel">
        <p className="screen-note">Loading configuration…</p>
      </div>
    );
  }

  const { environment, secrets } = config.data;

  return (
    <div className="recovery-panel">
      <h2 className="recovery-subtitle">Configuration Visibility</h2>
      <p className="recovery-description">
        Deployment-fixed configuration, shown read-only. These values are read from the environment
        at startup and require a redeploy or restart of the gateway to change — they cannot be
        edited here. Secret values are shown only as set or not set, never revealed.
      </p>

      <section className="config-section" aria-labelledby="config-env-heading">
        <h3 className="recovery-subtitle" id="config-env-heading">
          Environment settings
        </h3>
        <p className="recovery-description">
          Read-only. Each value requires a redeploy or restart to change.
        </p>
        {environment.length === 0 ? (
          <p className="recovery-value-muted">No environment settings to display.</p>
        ) : (
          <table className="recovery-table">
            <thead>
              <tr>
                <th>Setting</th>
                <th>Value</th>
                <th>Change requires</th>
              </tr>
            </thead>
            <tbody>
              {environment.map((entry) => (
                <tr key={entry.name}>
                  <td>
                    <code>{entry.name}</code>
                  </td>
                  <td>
                    <code>{entry.value}</code>
                  </td>
                  <td>Redeploy or restart</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="config-section" aria-labelledby="config-secrets-heading">
        <h3 className="recovery-subtitle" id="config-secrets-heading">
          Secret settings
        </h3>
        <p className="recovery-description">
          Secret values are never displayed — only whether each one is configured.
        </p>
        {secrets.length === 0 ? (
          <p className="recovery-value-muted">No secret settings to display.</p>
        ) : (
          <table className="recovery-table">
            <thead>
              <tr>
                <th>Setting</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((entry) => (
                <tr key={entry.name}>
                  <td>
                    <code>{entry.name}</code>
                  </td>
                  <td>{entry.is_set ? "Set" : "Not set"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
