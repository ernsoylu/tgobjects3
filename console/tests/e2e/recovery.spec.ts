/**
 * Recovery status critical path (task 45.1; Requirement 19.4).
 *
 * Verifies the Recovery screen renders the posture returned by the expected
 * `GET /admin/recovery` endpoint — last Checkpoint time (UTC, second
 * precision), the recovery-drill outcome, and the per-channel high-water
 * marks — and that it degrades gracefully to an informative panel when the
 * backend endpoint is absent (404/501), as documented in `src/api/recovery.ts`.
 * Also checks the Recovery Kit tab renders its static operator guidance.
 */
import { expect, test } from "@playwright/test";

import { fulfilJson } from "./fixtures";

const STATUS_WITH_DATA = {
  last_checkpoint_at: "2024-06-01T08:15:30Z",
  last_checkpoint_message_id: 4096,
  high_water_marks: [
    { channel_id: "-100123", message_id: 5000 },
    { channel_id: "-100456", message_id: 1200 },
  ],
  last_drill: { outcome: "pass", completed_at: "2024-06-01T09:00:00Z" },
};

test("renders checkpoint time, drill outcome, and high-water marks", async ({ page }) => {
  await page.route("**/admin/recovery", (route) => fulfilJson(route, STATUS_WITH_DATA));
  await page.goto("/recovery");

  await expect(page.getByText("2024-06-01 08:15:30 UTC")).toBeVisible();
  await expect(page.getByText("Pass")).toBeVisible();

  const markRows = page.locator("table.recovery-table tbody tr");
  await expect(markRows).toHaveCount(2);
  await expect(markRows.filter({ hasText: "-100123" })).toContainText("5,000");
});

test("shows an informative panel when the endpoint is absent", async ({ page }) => {
  // The hook treats 404/501 as "endpoint not available" and resolves to null.
  await page.route("**/admin/recovery", (route) => fulfilJson(route, { detail: "Not Found" }, 404));
  await page.goto("/recovery");

  await expect(page.getByText("Recovery status endpoint not available.")).toBeVisible();
});

test("the Recovery Kit tab shows operator guidance", async ({ page }) => {
  await page.route("**/admin/recovery", (route) => fulfilJson(route, STATUS_WITH_DATA));
  await page.goto("/recovery");

  await page.getByRole("tab", { name: "Recovery Kit" }).click();
  await expect(page.getByText(/Master key/)).toBeVisible();
  // The guidance references the recovery CLI by its path in a <code> element.
  await expect(page.getByText("cli/rebuild.py")).toBeVisible();
});
