/**
 * Settings panels: Access Keys and System Health (task 15.1; **Property 7**;
 * Requirements 5.3, 7.1).
 *
 * The Settings screen is a Radix Tabs host. This spec authenticates, opens the
 * relevant tab, and intercepts the panel's Admin / observability calls at the
 * network layer:
 *
 *   - **Access Keys (Property 7 / Req 5.3)** — creating a key shows the one-time
 *     `secret_access_key` exactly once in a dialog; dismissing the dialog clears
 *     it from the DOM and it is never shown again.
 *   - **System Health (Req 7.1)** — readiness is derived from the HTTP status of
 *     `GET /health/ready` (200 → ready, 503 → not ready), and the dependency
 *     rows reflect the probe; the on-demand metrics control fetches `/metrics`.
 *
 * The observability endpoints (`/health/ready`, `/metrics`) are mounted at the
 * admin app root, not under `/admin`. Route handlers are registered before
 * navigation; the most-recently-added route matches first; toast assertions are
 * scoped to `.toast-title`.
 */
import { expect, test } from "@playwright/test";

import { fulfilJson, mockAdminApi, signIn } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await mockAdminApi(page);
});

test("creating a key shows the secret once, then clears it on dismiss (Property 7)", async ({
  page,
}) => {
  // The list starts empty; the create POST returns the one-time secret.
  await page.route("**/admin/keys", (route) => {
    if (route.request().method() === "POST") {
      return fulfilJson(route, {
        access_key_id: "AKIANEWKEY00001",
        secret_access_key: "one-time-secret-value",
        label: "backup-worker",
      });
    }
    return fulfilJson(route, []);
  });

  await signIn(page, { to: "/settings" });
  await page.getByRole("tab", { name: "Access Keys" }).click();

  await expect(page.getByText("No access keys yet. Create one to get started.")).toBeVisible();

  await page.locator("#keys-label").fill("backup-worker");
  await page.getByRole("button", { name: "Create access key" }).click();

  // The secret is shown exactly once, inside the result dialog.
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("one-time-secret-value")).toBeVisible();

  // Dismissing the dialog clears the secret from the DOM — it is never shown
  // again (Property 7 / Requirement 5.3).
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByText("one-time-secret-value")).toHaveCount(0);
});

test("readiness reflects a 200 /health/ready as Ready (Req 7.1)", async ({ page }) => {
  await page.route("**/admin/keys", (route) => fulfilJson(route, []));
  await page.route("**/admin/config", (route) =>
    fulfilJson(route, { environment: [], secrets: [] }),
  );
  await page.route("**/health/ready", (route) => fulfilJson(route, { status: "ready" }, 200));

  await signIn(page, { to: "/settings" });
  await page.getByRole("tab", { name: "System Health" }).click();

  await expect(page.locator(".health-readiness")).toContainText("Ready");
  // Both dependencies read healthy on a 200.
  const depRows = page.locator("table.recovery-table tbody tr");
  await expect(depRows.filter({ hasText: "PostgreSQL" })).toContainText("Healthy");
  await expect(depRows.filter({ hasText: "Telegram session" })).toContainText("Healthy");
});

test("readiness reflects a 503 /health/ready as Not ready and names the dependency", async ({
  page,
}) => {
  await page.route("**/admin/keys", (route) => fulfilJson(route, []));
  await page.route("**/admin/config", (route) =>
    fulfilJson(route, { environment: [], secrets: [] }),
  );
  // 503 with the postgres dependency named as unhealthy.
  await page.route("**/health/ready", (route) =>
    fulfilJson(route, { unhealthy: ["postgresql"] }, 503),
  );

  await signIn(page, { to: "/settings" });
  await page.getByRole("tab", { name: "System Health" }).click();

  await expect(page.locator(".health-readiness")).toContainText("Not ready");
  const depRows = page.locator("table.recovery-table tbody tr");
  await expect(depRows.filter({ hasText: "PostgreSQL" })).toContainText("Unavailable");
  await expect(depRows.filter({ hasText: "Telegram session" })).toContainText("Healthy");
});

test("the metrics control fetches and renders the Prometheus text (Req 7.6)", async ({ page }) => {
  await page.route("**/admin/keys", (route) => fulfilJson(route, []));
  await page.route("**/admin/config", (route) =>
    fulfilJson(route, { environment: [], secrets: [] }),
  );
  await page.route("**/health/ready", (route) => fulfilJson(route, { status: "ready" }, 200));
  await page.route("**/metrics", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/plain",
      body: "# HELP tgo_requests_total Total requests\ntgo_requests_total 42\n",
    }),
  );

  await signIn(page, { to: "/settings" });
  await page.getByRole("tab", { name: "System Health" }).click();

  await page.getByRole("button", { name: "Fetch metrics" }).click();
  await expect(page.getByLabel("Prometheus metrics")).toContainText("tgo_requests_total 42");
});
