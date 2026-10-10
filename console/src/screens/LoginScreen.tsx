/**
 * Dashboard login screen (admin-dashboard Requirements 1.1, 1.3, 1.4, 1.7, 1.8).
 *
 * The gateway Admin API authenticates with HTTP Basic auth against the fixed
 * username `admin` (design: "`LoginScreen` — console/src/screens/LoginScreen.tsx";
 * `ADMIN_USERNAME` in `../api/client`), so the only secret the operator supplies
 * is the password. The screen renders:
 *
 *  - A read-only, programmatically-labelled identifier field showing the fixed
 *    `admin` username, and a single password field with a programmatic label.
 *  - A submit button that is disabled while a submission is in progress
 *    (`state.status === "authenticating"`), blocking a second submit of the same
 *    form until the in-flight attempt resolves (Requirement 1.3).
 *  - An inline `role="alert"` error region. On a rejected `signIn` the fixed
 *    identifier is retained and the password field is cleared, and an
 *    authentication-failed message is shown (Requirement 1.4).
 *  - When the previous session ended via an HTTP 401 (`endedReason === "expired"`),
 *    a "session is no longer valid" message (Requirement 1.8).
 *
 * Authentication itself (Basic credential + CSRF bootstrap) lives in
 * `AuthProvider`/`useAuth`; this screen only drives `signIn(password)` and
 * reflects the observable `state`. Navigation on success is the router guard's
 * concern (task 5.2), not this component's — a successful `signIn` flips the
 * auth state to `authenticated` and the guard routes away.
 */
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";

import { ADMIN_USERNAME } from "../api/client";
import { useAuth } from "../auth/context";

/** Message shown when a bootstrap `signIn` is rejected (Requirement 1.4). */
const AUTH_FAILED_MESSAGE = "Authentication failed. Check the admin password and try again.";

/** Message shown after a session ended due to an HTTP 401 (Requirement 1.8). */
const SESSION_EXPIRED_MESSAGE = "Your session is no longer valid. Please sign in again.";

export function LoginScreen(): JSX.Element {
  const { state, signIn } = useAuth();
  const [password, setPassword] = useState("");
  // A local error distinct from `endedReason`: set when *this* screen's submit
  // is rejected, so the auth-failed message replaces the session-expired notice.
  const [submitError, setSubmitError] = useState<string | null>(null);

  const isSubmitting = state.status === "authenticating";
  const passwordRef = useRef<HTMLInputElement>(null);

  // Focus the password field on mount so the operator can type immediately
  // (reasonable focus management, Requirement 10 accessibility).
  useEffect(() => {
    passwordRef.current?.focus();
  }, []);

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (isSubmitting || password.length === 0) {
        return;
      }
      setSubmitError(null);
      void signIn(password).catch(() => {
        // Rejected login: retain the fixed identifier (it is not editable), clear
        // the entered secret, and surface the auth-failed message (Requirement 1.4).
        setPassword("");
        setSubmitError(AUTH_FAILED_MESSAGE);
        passwordRef.current?.focus();
      });
    },
    [isSubmitting, password, signIn],
  );

  // Once the session is authenticated, leave the login screen for the admin
  // area (Requirement 1.1). The index route forwards `/` to `/buckets`, and the
  // setup guard diverts to the wizard if first-run setup is still required.
  // Using the router here (rather than leaving navigation to a guard) is what
  // moves an operator off `/login` after a successful sign-in.
  if (state.status === "authenticated") {
    return <Navigate to="/" replace />;
  }

  // Prefer the explicit submit error; otherwise, if the previous session was
  // ended by a 401, show the session-expired notice (Requirement 1.8).
  const errorMessage =
    submitError ?? (state.endedReason === "expired" ? SESSION_EXPIRED_MESSAGE : null);

  return (
    <main className="login-screen">
      <section className="login-card">
        <header className="login-header">
          <p className="login-brand">tgobjects3</p>
          <h1 className="login-title">Sign in</h1>
          <p className="login-description">Enter the admin password to reach the dashboard.</p>
        </header>

        <form className="login-form" onSubmit={onSubmit} noValidate>
          <div className="login-field">
            <label className="login-label" htmlFor="login-identifier">
              Admin identifier
            </label>
            <input
              id="login-identifier"
              className="login-input"
              type="text"
              value={ADMIN_USERNAME}
              readOnly
              autoComplete="username"
            />
          </div>

          <div className="login-field">
            <label className="login-label" htmlFor="login-password">
              Password
            </label>
            <input
              id="login-password"
              ref={passwordRef}
              className="login-input"
              type="password"
              value={password}
              autoComplete="current-password"
              disabled={isSubmitting}
              aria-invalid={submitError ? true : undefined}
              aria-describedby={errorMessage ? "login-error" : undefined}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>

          {errorMessage ? (
            <p className="login-error" id="login-error" role="alert">
              {errorMessage}
            </p>
          ) : null}

          <button
            type="submit"
            className="btn btn--primary login-submit"
            disabled={isSubmitting || password.length === 0}
          >
            {isSubmitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
