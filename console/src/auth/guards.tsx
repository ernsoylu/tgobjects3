/**
 * Routing guards for the dashboard (admin-dashboard Requirements 1.1, 1.6, 3.1,
 * 3.12, 12.1, 12.5).
 *
 * Two guards gate the authenticated admin area, applied outside-in by the
 * router in `App.tsx` (design: "Console SPA component diagram" —
 * `RequireAuth / RequireSetup (NEW)`; Error Handling — "Setup-required
 * routing"):
 *
 *  - {@link RequireAuth} admits a request only while the session is
 *    `authenticated`. Any other status (including the `unauthenticated` state the
 *    global 401 interceptor flips to via `signOut("expired")`, task 4.2) is sent
 *    to `/login` (Requirements 1.1, 1.6, 12.1). The `authenticating` status is
 *    transient — the login screen owns that spinner — so it too routes to login
 *    rather than flashing the admin shell.
 *
 *  - {@link RequireSetup} runs *inside* `RequireAuth` (so the probe always carries
 *    a credential) and queries `POST /admin/setup/status`. While
 *    `setup_required` is true it redirects guarded routes to `/setup`; while the
 *    probe is still in flight it renders nothing, so the admin screens are never
 *    flashed before the setup gate is known (Requirements 3.1, 3.12, 12.5). A
 *    failed probe is treated conservatively as "not yet set up" and also routes
 *    to `/setup`, keeping an operator out of a half-configured dashboard.
 *
 * The guards hold no credentials themselves; they only read `useAuth` state and
 * the setup-status query. Secrets live in the memory-only `adminClient`.
 */
import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";

import { useSetupStatus } from "../api/setup";
import { useAuth } from "./context";

export interface GuardProps {
  readonly children: ReactNode;
}

/**
 * Admit the children only while the session is authenticated; otherwise redirect
 * to `/login` (Requirements 1.1, 1.6, 12.1).
 */
export function RequireAuth({ children }: GuardProps): JSX.Element {
  const { state } = useAuth();
  if (state.status !== "authenticated") {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

/**
 * While the gateway reports `setup_required = true`, redirect guarded routes to
 * `/setup`; once it reports `false`, admit the admin screens (Requirements 3.1,
 * 3.12, 12.5).
 *
 * Rendered inside {@link RequireAuth}, so the setup-status probe always carries a
 * credential. The in-flight state renders nothing (the guarded screen is not yet
 * shown); a probe error is treated as "setup still required" so a misconfigured
 * or still-initialising gateway sends the operator to the wizard rather than a
 * broken admin view.
 */
export function RequireSetup({ children }: GuardProps): JSX.Element | null {
  const { data, isLoading } = useSetupStatus();
  if (isLoading) {
    return null;
  }
  if (data?.setup_required ?? true) {
    return <Navigate to="/setup" replace />;
  }
  return <>{children}</>;
}
