/**
 * Upload jobs + retry critical path (task 45.1; Requirement 19.3).
 *
 * Verifies the Jobs screen lists upload jobs with their state, shows a Retry
 * button only for FAILED jobs, and that clicking Retry POSTs
 * `/admin/jobs/{id}/retry`, surfaces a success toast, and reflects the job's
 * transition back to STAGED after the list refetches. Also covers the error
 * boundary where the backend rejects the retry (e.g. the job is no longer
 * FAILED) with HTTP 409.
 */
import { expect, test } from "@playwright/test";

import { fulfilJson, type JobEntry } from "./fixtures";

const COMMITTED_JOB: JobEntry = {
  id: "job-committed-1",
  generation_id: "gen-aaaa",
  state: "COMMITTED",
  attempts: 1,
  last_error: null,
};

const FAILED_JOB: JobEntry = {
  id: "job-failed-1",
  generation_id: "gen-bbbb",
  state: "FAILED",
  attempts: 3,
  last_error: "flood wait: 42s",
};

test("lists jobs and offers Retry only for FAILED jobs", async ({ page }) => {
  await page.route("**/admin/jobs", (route) => fulfilJson(route, [COMMITTED_JOB, FAILED_JOB]));
  await page.goto("/jobs");

  const rows = page.locator("table.data-table tbody tr");
  await expect(rows).toHaveCount(2);

  const committedRow = rows.filter({ hasText: "job-committed-1" });
  const failedRow = rows.filter({ hasText: "job-failed-1" });

  await expect(committedRow.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await expect(failedRow.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(failedRow).toContainText("flood wait: 42s");
});

test("retrying a FAILED job transitions it back to STAGED", async ({ page }) => {
  // The list returns FAILED first; after a successful retry the next refetch
  // returns the job as STAGED. A one-shot toggle models that transition.
  let retried = false;
  await page.route("**/admin/jobs", (route) => {
    const job: JobEntry = retried
      ? { ...FAILED_JOB, state: "STAGED", last_error: null }
      : FAILED_JOB;
    return fulfilJson(route, [job]);
  });
  await page.route("**/admin/jobs/*/retry", (route) => {
    retried = true;
    return fulfilJson(route, { ...FAILED_JOB, state: "STAGED" });
  });

  await page.goto("/jobs");
  const row = page.locator("table.data-table tbody tr");
  await expect(row).toContainText("FAILED");

  await row.getByRole("button", { name: "Retry" }).click();

  // Success toast confirms the submission (scope to the visible toast title;
  // Radix also mirrors the text into an ARIA live region for screen readers).
  await expect(page.locator(".toast-title")).toContainText("Retry submitted");
  // After the jobs query is invalidated, the row reflects the new state.
  await expect(row).toContainText("STAGED");
  await expect(row.getByRole("button", { name: "Retry" })).toHaveCount(0);
});

test("a rejected retry surfaces the backend error", async ({ page }) => {
  await page.route("**/admin/jobs", (route) => fulfilJson(route, [FAILED_JOB]));
  await page.route("**/admin/jobs/*/retry", (route) =>
    fulfilJson(route, { detail: "job is not in FAILED state" }, 409),
  );

  await page.goto("/jobs");
  await page.locator("table.data-table tbody tr").getByRole("button", { name: "Retry" }).click();

  await expect(page.locator(".toast-title")).toContainText("Retry failed");
  await expect(page.locator(".toast-description")).toContainText("job is not in FAILED state");
});
