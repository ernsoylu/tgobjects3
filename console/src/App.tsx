import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider } from "./auth/AuthProvider";
import { RequireAuth, RequireSetup } from "./auth/guards";
import { AppLayout } from "./components/AppLayout";
import { BucketsScreen } from "./screens/BucketsScreen";
import { JobsScreen } from "./screens/JobsScreen";
import { LoginScreen } from "./screens/LoginScreen";
import { ObjectsScreen } from "./screens/ObjectsScreen";
import { RecoveryScreen } from "./screens/RecoveryScreen";
import { SettingsScreen } from "./screens/SettingsScreen";
import { SetupWizard } from "./setup/SetupWizard";

/**
 * Single shared QueryClient. Console screens poll the Admin API through
 * TanStack Query (e.g. the bucket list auto-refreshes via `refetchInterval`),
 * so a sensible default stale time keeps background refetches cheap without
 * hiding fresh data.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
    },
  },
});

/**
 * Application shell (admin-dashboard Requirements 1.1, 1.6, 1.7, 3.1, 3.12,
 * 12.1, 12.5).
 *
 * The router tree is wrapped in `AuthProvider` so every route (and the shared
 * `adminClient`'s global 401 hook) sees the in-memory session state (design:
 * "Console SPA component diagram"). Routing is layered:
 *
 *  - `/login` renders the `LoginScreen` and is reachable without a session; a
 *    401 from any authenticated request flips the session to `unauthenticated`
 *    (task 4.2), and `RequireAuth` then diverts here (Requirements 1.1, 1.6, 12.1).
 *  - `/setup` renders the `SetupWizard` behind `RequireAuth` only — it must stay
 *    reachable while `setup_required` is true, so it is deliberately *not* behind
 *    `RequireSetup` (which would redirect it to itself).
 *  - The admin screens sit behind `RequireAuth` then `RequireSetup`: an operator
 *    must be authenticated and the gateway must report setup complete before
 *    `AppLayout` and the existing screens render (Requirements 3.1, 3.12, 12.5).
 *
 * `AppLayout` and the existing screens are reused unchanged apart from the
 * sidebar logout control added in `AppLayout` (Requirement 1.7).
 */
export function App(): JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginScreen />} />
            <Route
              path="/setup"
              element={
                <RequireAuth>
                  <SetupWizard />
                </RequireAuth>
              }
            />
            <Route
              element={
                <RequireAuth>
                  <RequireSetup>
                    <AppLayout />
                  </RequireSetup>
                </RequireAuth>
              }
            >
              <Route index element={<Navigate to="/buckets" replace />} />
              <Route path="buckets" element={<BucketsScreen />} />
              <Route path="objects" element={<ObjectsScreen />} />
              <Route path="jobs" element={<JobsScreen />} />
              <Route path="recovery" element={<RecoveryScreen />} />
              <Route path="settings" element={<SettingsScreen />} />
              <Route path="*" element={<Navigate to="/buckets" replace />} />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
