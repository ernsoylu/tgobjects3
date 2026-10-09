/**
 * Navigation critical path (task 45.1; Requirement 19.1 — the Console shell).
 *
 * Verifies the app shell renders, the index route redirects to `/buckets`, and
 * the sidebar links move between all five screens, with the active screen's
 * heading reflecting the destination. The Admin API is mocked so each screen
 * renders its loaded state rather than a spinner.
 */
import { expect, test } from "@playwright/test";

import { mockAdminApi } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await mockAdminApi(page);
});

test("index route redirects to the Buckets screen", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/buckets$/);
  await expect(page.getByRole("heading", { level: 1, name: "Buckets" })).toBeVisible();
});

test("sidebar navigates between all five screens", async ({ page }) => {
  await page.goto("/");

  const steps: Array<{ link: string; url: RegExp; heading: string }> = [
    { link: "Objects", url: /\/objects$/, heading: "Objects" },
    { link: "Jobs", url: /\/jobs$/, heading: "Upload Jobs" },
    { link: "Recovery", url: /\/recovery$/, heading: "Recovery" },
    { link: "Settings", url: /\/settings$/, heading: "Settings" },
    { link: "Buckets", url: /\/buckets$/, heading: "Buckets" },
  ];

  for (const step of steps) {
    await page.getByRole("link", { name: step.link }).click();
    await expect(page).toHaveURL(step.url);
    await expect(page.getByRole("heading", { level: 1, name: step.heading })).toBeVisible();
  }
});

test("an unknown route falls back to the Buckets screen", async ({ page }) => {
  await page.goto("/does-not-exist");
  await expect(page).toHaveURL(/\/buckets$/);
  await expect(page.getByRole("heading", { level: 1, name: "Buckets" })).toBeVisible();
});
