import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./components/AppLayout";
import { BucketsScreen } from "./screens/BucketsScreen";
import { JobsScreen } from "./screens/JobsScreen";
import { ObjectsScreen } from "./screens/ObjectsScreen";
import { RecoveryScreen } from "./screens/RecoveryScreen";
import { SettingsScreen } from "./screens/SettingsScreen";

/**
 * Single shared QueryClient. Console screens poll the Admin API through
 * TanStack Query (e.g. the bucket list auto-refreshes via `refetchInterval`,
 * task 44.1), so a sensible default stale time keeps background refetches cheap
 * without hiding fresh data.
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
 * Application shell: wires the TanStack Query provider and the router with
 * placeholder routes for the screens implemented in task 44. Requirement 19.1.
 */
export function App(): JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<Navigate to="/buckets" replace />} />
            <Route path="buckets" element={<BucketsScreen />} />
            <Route path="objects" element={<ObjectsScreen />} />
            <Route path="jobs" element={<JobsScreen />} />
            <Route path="recovery" element={<RecoveryScreen />} />
            <Route path="settings" element={<SettingsScreen />} />
            <Route path="*" element={<Navigate to="/buckets" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
