/**
 * Setup-required routing (task 15.1; **Property 9**; Requirements 3.1, 3.12).
 *
 * `RequireSetup` queries `POST /admin/setup/status` on entry to a guarded admin
 * route. This spec intercepts that probe at the network layer to exercise
 * Property 9 at the UI level:
 *
 *   - while the gateway reports `setup_required: true`, authenticated navigation
 *     into any guarded route lands on the Setup Wizard, never the admin screens
 *     — demonstrated for both the index and a deep link to a specific screen;
 *   - while it reports `setup_required: false`, authenticated navigation reaches
 *     the admin screens.
 *
 * Both cases authenticate first (the guards sit inside `RequireAuth`), so the
 * probe always carries a credential. Route handlers are registered before
 * navigation; the most-recently-added route matches first.
 */
import { expect, test, type Page } from "@playwright/test";

import { ADMIN_PASSWORD, mockAdminApi, mockSetupStatus } from "./fixtures";

/**
 * Authenticate from the given deep link. Navigating there while unauthenticated
 * bounces to `/login` *client-side* (preserving the in-memory session), the
 * password is submitted, and the helper returns once the session has flipped
 * authenticated (off `/login`). The test asserts the resulting destination.
 */
async function authenticateFrom(page: Page, deepLink: string): Promise<void> {
  await page.goto(deepLink);
  await expect(page).toHaveURL(/\/login$/);
  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login$/);
}

test("setup_required=true diverts the index into the wizard", async ({ page }) => {
  await mockAdminApi(page);
  await mockSetupStatus(page, true);

  await authenticateFrom(page, "/");

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole("heading", { level: 1, name: "Set up tgobjects3" })).toBeVisible();
});

test("setup_required=true diverts a deep link to a guarded screen into the wizard", async ({
  page,
}) => {
  await mockAdminApi(page);
  await mockSetupStatus(page, true);

  // A direct deep link to Settings still lands on the wizard, not the screen.
  await authenticateFrom(page, "/settings");

  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole("heading", { level: 1, name: "Set up tgobjects3" })).toBeVisible();
});

test("setup_required=false reaches the admin screens", async ({ page }) => {
  await mockAdminApi(page);
  await mockSetupStatus(page, false);

  await authenticateFrom(page, "/");

  // With setup complete the guards admit the admin area (the index forwards to
  // /buckets).
  await expect(page).toHaveURL(/\/buckets$/);
  await expect(page.getByRole("heading", { level: 1, name: "Buckets" })).toBeVisible();
});
