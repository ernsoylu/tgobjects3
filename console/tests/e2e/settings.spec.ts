/**
 * Settings read/update critical path (task 45.1; Requirements 19.5–19.7).
 *
 * Verifies the Settings screen loads the five runtime settings into an editable
 * form, applies a valid change via `PUT /admin/settings` with a success toast,
 * blocks submission on a client-side range violation (e.g. read-ahead > 32),
 * and surfaces the backend's cross-field rejection — a staging-size reduction
 * below the reserved bytes returns HTTP 409 (Requirements 19.6/19.7) — as a
 * readable error toast.
 */
import { expect, test } from "@playwright/test";

import { SAMPLE_SETTINGS, fulfilJson } from "./fixtures";

test("loads the current settings into the form", async ({ page }) => {
  await page.route("**/admin/settings", (route) => fulfilJson(route, SAMPLE_SETTINGS));
  await page.goto("/settings");

  await expect(page.locator("#settings-staging_max_size")).toHaveValue(
    String(SAMPLE_SETTINGS.staging_max_size),
  );
  await expect(page.locator("#settings-read_ahead")).toHaveValue("8");
  await expect(page.locator("#settings-upload_concurrency")).toHaveValue("4");
});

test("saving a valid change shows a success toast", async ({ page }) => {
  await page.route("**/admin/settings", (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as Record<string, number>;
      return fulfilJson(route, { ...SAMPLE_SETTINGS, ...body });
    }
    return fulfilJson(route, SAMPLE_SETTINGS);
  });
  await page.goto("/settings");

  await page.locator("#settings-upload_concurrency").fill("6");
  await page.getByRole("button", { name: "Save changes" }).click();

  // Scope to the visible toast title; Radix also mirrors it into an ARIA live
  // region, so an unscoped text match would be ambiguous under strict mode.
  await expect(page.locator(".toast-title")).toContainText("Settings saved");
});

test("an out-of-range field blocks submission", async ({ page }) => {
  await page.route("**/admin/settings", (route) => fulfilJson(route, SAMPLE_SETTINGS));
  await page.goto("/settings");

  // read_ahead max is 32; 99 is rejected client-side.
  await page.locator("#settings-read_ahead").fill("99");

  await expect(page.getByText("Must be at most 32.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save changes" })).toBeDisabled();
});

test("a 409 staging reduction below reserved surfaces as an error toast", async ({ page }) => {
  await page.route("**/admin/settings", (route) => {
    if (route.request().method() === "PUT") {
      return fulfilJson(route, { detail: "staging_max_size below reserved bytes" }, 409);
    }
    return fulfilJson(route, SAMPLE_SETTINGS);
  });
  await page.goto("/settings");

  // Any valid change enables Save; the backend then rejects it with 409.
  await page.locator("#settings-staging_max_size").fill("1024");
  await page.getByRole("button", { name: "Save changes" }).click();

  await expect(page.locator(".toast-title")).toContainText("Save failed");
  await expect(page.locator(".toast-description")).toContainText(
    "staging_max_size below reserved bytes",
  );
});
