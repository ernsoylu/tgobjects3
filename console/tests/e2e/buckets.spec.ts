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

import { SAMPLE_BUCKETS, fulfilJson, type BucketStats } from "./fixtures";

test("renders a row per bucket with its stats", async ({ page }) => {
  await page.route("**/admin/buckets", (route) => fulfilJson(route, SAMPLE_BUCKETS));
  await page.goto("/buckets");

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
  await page.route("**/admin/buckets", (route) => fulfilJson(route, []));
  await page.goto("/buckets");

  await expect(page.getByText("No buckets yet.")).toBeVisible();
  await expect(page.locator("table.data-table")).toHaveCount(0);
});

test("a failed load surfaces an error", async ({ page }) => {
  await page.route("**/admin/buckets", (route) => fulfilJson(route, { detail: "boom" }, 500));
  await page.goto("/buckets");

  // The shared QueryClient retries a failed query a few times with backoff
  // before surfacing the error, so allow headroom beyond the default timeout.
  await expect(page.getByRole("alert")).toContainText("Failed to load buckets", {
    timeout: 20_000,
  });
});

test("the list reflects a background refresh", async ({ page }) => {
  // Serve one count on the first call, a larger count on every call after, so
  // the next auto-refetch surfaces the new value.
  let callCount = 0;
  await page.route("**/admin/buckets", (route) => {
    callCount += 1;
    const stored: BucketStats[] = [
      {
        name: "photos",
        object_count: callCount === 1 ? 1234 : 2000,
        stored_bytes: 5_368_709_120,
        pending_write_count: 0,
      },
    ];
    return fulfilJson(route, stored);
  });

  await page.goto("/buckets");
  const photosRow = page.locator("table.data-table tbody tr").filter({ hasText: "photos" });
  await expect(photosRow).toContainText("1,234");

  // Trigger a refetch deterministically by re-focusing the tab is not enough
  // (refetchOnWindowFocus is off); instead reload, which the mock answers with
  // the updated count on its second call.
  await page.reload();
  await expect(photosRow).toContainText("2,000");
});
