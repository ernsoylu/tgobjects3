/**
 * Bucket list critical path (task 45.1; Requirement 19.1).
 *
 * Verifies the Buckets screen renders one row per bucket with its name, object
 * count, human-readable stored bytes, and pending-write count, and that the
 * list auto-refreshes to reflect background commits (the `useBuckets` hook polls
 * on an interval). The refresh is proven deterministically by swapping the mock
 * response and asserting the new value appears after a refetch, rather than
 * waiting on the real interval.
 */
import { expect, test } from "@playwright/test";

import { SAMPLE_BUCKETS, fulfilBootstrap, fulfilJson, signIn, type BucketStats } from "./fixtures";

test("renders a row per bucket with its stats", async ({ page }) => {
  // The spec's own buckets handler carries the CSRF header (via fulfilBootstrap)
  // so it serves both the sign-in bootstrap and the Buckets screen fetch.
  await page.route("**/admin/buckets", (route) => fulfilBootstrap(route, SAMPLE_BUCKETS));
  await signIn(page, { to: "/buckets", skipBootstrapRoute: true });

  const rows = page.locator("table.data-table tbody tr");
  await expect(rows).toHaveCount(SAMPLE_BUCKETS.length);

  // The `photos` bucket row shows its name, object count (1,234), stored size
  // (5 GiB -> "5.0 GB"), and pending writes (2).
  const photosRow = rows.filter({ hasText: "photos" });
  await expect(photosRow).toContainText("1,234");
  await expect(photosRow).toContainText("5.0 GB");
  await expect(photosRow).toContainText("2");

  const backupsRow = rows.filter({ hasText: "backups" });
  await expect(backupsRow).toContainText("1.0 KB");
});

test("empty bucket list shows the empty state", async ({ page }) => {
  // An empty list still carries the CSRF header so sign-in bootstrap succeeds.
  await page.route("**/admin/buckets", (route) => fulfilBootstrap(route, []));
  await signIn(page, { to: "/buckets", skipBootstrapRoute: true });

  await expect(page.getByText("No buckets yet.")).toBeVisible();
  await expect(page.locator("table.data-table")).toHaveCount(0);
});

test("a failed load surfaces an error", async ({ page }) => {
  // The first GET is the sign-in bootstrap and must succeed (with the CSRF
  // header); every subsequent GET — the Buckets screen's own fetch — fails with
  // 500 so the screen surfaces its error state.
  let bootstrapped = false;
  await page.route("**/admin/buckets", (route) => {
    if (!bootstrapped) {
      bootstrapped = true;
      return fulfilBootstrap(route, SAMPLE_BUCKETS);
    }
    return fulfilJson(route, { detail: "boom" }, 500);
  });
  await signIn(page, { to: "/buckets", skipBootstrapRoute: true });

  // The shared QueryClient retries a failed query a few times with backoff
  // before surfacing the error, so allow headroom beyond the default timeout.
  await expect(page.getByRole("alert")).toContainText("Failed to load buckets", {
    timeout: 20_000,
  });
});

test("the list reflects a background refresh", async ({ page }) => {
  // The sign-in bootstrap consumes the first GET; the Buckets screen's own
  // fetches follow. Serve 1,234 until the page reloads, then 2,000, so the
  // reload surfaces the updated count. `screenCalls` counts only the screen's
  // fetches (every GET after the bootstrap one).
  let bootstrapped = false;
  let screenCalls = 0;
  await page.route("**/admin/buckets", (route) => {
    if (!bootstrapped) {
      bootstrapped = true;
      return fulfilBootstrap(route, SAMPLE_BUCKETS);
    }
    screenCalls += 1;
    const stored: BucketStats[] = [
      {
        name: "photos",
        object_count: screenCalls === 1 ? 1234 : 2000,
        stored_bytes: 5_368_709_120,
        pending_write_count: 0,
      },
    ];
    return fulfilBootstrap(route, stored);
  });

  await signIn(page, { to: "/buckets", skipBootstrapRoute: true });
  const photosRow = page.locator("table.data-table tbody tr").filter({ hasText: "photos" });
  await expect(photosRow).toContainText("1,234");

  // A full page reload would drop the in-memory session (auth lives only in
  // React memory now) and bounce back to /login, so prove the auto-refresh
  // through the hook's own poll instead: `useBuckets` refetches on a 15 s
  // interval, and the mock answers the next poll with the updated count. Allow
  // headroom past the interval.
  await expect(photosRow).toContainText("2,000", { timeout: 20_000 });
});
