/**
 * Dashboard authentication provider.
 *
 * Holds the admin session state in React memory only — never `localStorage`,
 * `sessionStorage`, or cookies (design: "Auth provider/context —
 * console/src/auth/AuthProvider.tsx"; Requirements 1.5, 11.4). The actual
 * Basic credential and CSRF token live inside the shared `adminClient` module
 * (also memory-only); this provider only tracks the observable session
 * `status` and the `endedReason` surfaced to the login screen.
 *
 * Login model (design: "Sequence: Dashboard login + CSRF bootstrap"): the
 * backend uses HTTP Basic auth (fixed username `admin`) and returns the
 * per-session CSRF token in the `X-CSRF-Token` *response* header on any
 * authenticated request — no cookie, no dedicated login endpoint. `signIn`
 * therefore sets the in-memory Basic credential and issues a bootstrap
 * `GET /admin/buckets` (an existing authenticated read, Requirement 19.1): a
 * 200 lets the client's response interceptor cache the CSRF token and flips
 * the session to `authenticated` (Requirements 1.2, 1.5, 11.4); a 401 rejects
 * and leaves the session `unauthenticated` (Requirement 1.6).
 *
 * A global 401 handler is registered on the client so that a 401 from any
 * *non-bootstrap* authenticated request ends the session via
 * `signOut("expired")` (Requirements 1.6, 1.8, 12.1). The bootstrap GET must
 * not take that path — a rejected login is not an expired session — so the
 * global handler is suspended for the duration of the bootstrap request and
 * restored afterwards.
 *
 * The context, state types, and `useAuth` hook live in `./context` so this
 * module exports only its React component.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  adminClient,
  clearCredentials,
  setCredentials,
  setUnauthorizedHandler,
} from "../api/client";
import {
  AuthContext,
  INITIAL_AUTH_STATE,
  type AuthContextValue,
  type AuthState,
  type SessionEndedReason,
} from "./context";

/**
 * The bootstrap read used to validate credentials and obtain the CSRF token.
 * Mirrors `BOOTSTRAP_PATH` in `../api/client` (the client's response-error
 * interceptor re-issues the same read to refresh a stale token). Kept as a
 * local constant rather than imported so the client module can be mocked in
 * `AuthProvider.test.tsx` without having to re-export it.
 */
const BOOTSTRAP_PATH = "/buckets";

export interface AuthProviderProps {
  readonly children: ReactNode;
}

/**
 * Provides the auth context to the tree and wires the global 401 interceptor
 * to end the session.
 */
export function AuthProvider({ children }: AuthProviderProps): JSX.Element {
  const [state, setState] = useState<AuthState>(INITIAL_AUTH_STATE);

  /**
   * True only while the bootstrap GET of `signIn` is in flight. When set, the
   * global 401 handler stands down so a rejected login does not masquerade as
   * an expired session.
   */
  const bootstrapping = useRef(false);

  const signOut = useCallback((reason: SessionEndedReason = "logout"): void => {
    clearCredentials();
    setState({ status: "unauthenticated", endedReason: reason });
  }, []);

  const signIn = useCallback(async (password: string): Promise<void> => {
    setState({ status: "authenticating", endedReason: null });
    setCredentials(password);
    bootstrapping.current = true;
    try {
      // Bootstrap read: a 200 lets the response interceptor cache the CSRF
      // token; a 401 is a rejected login (handled below), not an expired
      // session, so the global handler stays suspended for this request.
      await adminClient.get(BOOTSTRAP_PATH);
    } catch (error) {
      clearCredentials();
      setState({ status: "unauthenticated", endedReason: null });
      throw error;
    } finally {
      bootstrapping.current = false;
    }
    setState({ status: "authenticated", endedReason: null });
  }, []);

  // Register the global 401 handler: a 401 from any non-bootstrap request ends
  // the session as "expired" (Requirements 1.6, 1.8, 12.1). The disposer keeps
  // registration scoped to this provider's lifetime.
  useEffect(() => {
    return setUnauthorizedHandler(() => {
      if (bootstrapping.current) {
        return;
      }
      signOut("expired");
    });
  }, [signOut]);

  const value = useMemo<AuthContextValue>(
    () => ({ state, signIn, signOut }),
    [state, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
