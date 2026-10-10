/**
 * Auth context, state types, and the `useAuth` consumer hook.
 *
 * Kept separate from `AuthProvider.tsx` so that module exports only its React
 * component (satisfying `react-refresh/only-export-components`); the context
 * object, value/state types, and hook live here. See `AuthProvider.tsx` for
 * the login + CSRF-bootstrap behaviour (design: "Auth provider/context —
 * console/src/auth/AuthProvider.tsx"; Requirements 1.2, 1.5, 1.6, 1.8, 11.4).
 */
import { createContext, useContext } from "react";

/** Reason the previous session ended, surfaced on the login screen. */
export type SessionEndedReason = "logout" | "expired";

/** Observable session state held in React memory only (never storage/cookies). */
export interface AuthState {
  readonly status: "unauthenticated" | "authenticating" | "authenticated";
  /** Reason the last session ended, surfaced on the login screen. */
  readonly endedReason: SessionEndedReason | null;
}

/** Context value exposed to consumers through {@link useAuth}. */
export interface AuthContextValue {
  readonly state: AuthState;
  /** Validate the password via a bootstrap GET; caches creds + CSRF on success. */
  signIn(password: string): Promise<void>;
  /** Discard credentials + CSRF token and return to the login screen. */
  signOut(reason?: SessionEndedReason): void;
}

/** Initial, signed-out session state. */
export const INITIAL_AUTH_STATE: AuthState = { status: "unauthenticated", endedReason: null };

export const AuthContext = createContext<AuthContextValue | null>(null);

/** Access the auth context; throws if used outside an `AuthProvider`. */
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return value;
}
