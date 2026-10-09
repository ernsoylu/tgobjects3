/**
 * Object browser critical path (task 45.1; Requirement 19.2).
 *
 * Verifies the Object Browser loads the bucket selector from `/admin/buckets`,
 * lists a page of committed objects with human-readable sizes, filters by key
 * prefix, and pages forward/back using the backend's opaque `next_start_after`
 * cursor. The mock answers `/admin/objects` from its query parameters so prefix
 * filtering and pagination are exercised end-to-end rather than faked.
 */
import { expect, test } from "@playwright/test";

import { SAMPLE_BUCKETS, fulfilJson, type ObjectsPage } from "./fixtures";

/** Build an objects page response for a given cursor/prefix. */
function pageFor(prefix: string, startAfter: string | null): ObjectsPage {
  if (prefix === "logs/") {
    return {
      bucket: "photos",
      prefix,
      objects: [
        {
          key: "logs/app.log",
          size: 10,
          etag: '"log"',
          last_modified: "2024-01-01T00:00:00Z",
        },
      ],
      next_start_after: null,
    };
  }
  // Two-page listing for the empty prefix: page 1 points to page 2.
  if (startAfter === null) {
    return {
      bucket: "photos",
      prefix,
      objects: [
        {
          key: "a/first.bin",
          size: 1024,
          etag: null,
          last_modified: "2024-05-01T12:00:00Z",
        },
      ],
      next_start_after: "a/first.bin",
    };
  }
  return {
    bucket: "photos",
    prefix,
    objects: [
      {
        key: "b/second.bin",
        size: 2_097_152,
        etag: '"two"',
        last_modified: "2024-05-02T12:00:00Z",
      },
    ],
    next_start_after: null,
  };
}

test.beforeEach(async ({ page }) => {
  await page.route("**/admin/buckets", (route) => fulfilJson(route, SAMPLE_BUCKETS));
  await page.route("**/admin/objects*", (route) => {
    const url = new URL(route.request().url());
    const prefix = url.searchParams.get("prefix") ?? "";
    const startAfter = url.searchParams.get("start_after");
    return fulfilJson(route, pageFor(prefix, startAfter));
  });
});

test("lists objects for the default bucket with readable sizes", async ({ page }) => {
  await page.goto("/objects");

  // The selector defaults to the first bucket (photos).
  await expect(page.locator("#object-bucket")).toHaveValue("photos");

  const rows = page.locator("table.data-table tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("a/first.bin");
  await expect(rows.first()).toContainText("1 KB");
  // A null ETag renders as an em dash placeholder.
  await expect(rows.first()).toContainText("—");
});

test("filtering by prefix reloads the listing", async ({ page }) => {
  await page.goto("/objects");
  await expect(page.locator("table.data-table tbody tr")).toContainText("a/first.bin");

  await page.locator("#object-prefix").fill("logs/");

  const rows = page.locator("table.data-table tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("logs/app.log");
});

test("pages forward and back through the cursor", async ({ page }) => {
  await page.goto("/objects");

  await expect(page.getByText("Page 1")).toBeVisible();
  await expect(page.locator("table.data-table tbody tr")).toContainText("a/first.bin");

  const next = page.getByRole("button", { name: "Next" });
  const previous = page.getByRole("button", { name: "Previous" });
  await expect(previous).toBeDisabled();

  await next.click();
  await expect(page.getByText("Page 2")).toBeVisible();
  await expect(page.locator("table.data-table tbody tr")).toContainText("b/second.bin");
  // On the last page there is no further cursor.
  await expect(next).toBeDisabled();

  await previous.click();
  await expect(page.getByText("Page 1")).toBeVisible();
  await expect(page.locator("table.data-table tbody tr")).toContainText("a/first.bin");
});
