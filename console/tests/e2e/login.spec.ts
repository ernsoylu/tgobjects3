/**
 * Dashboard login critical path (task 15.1; Requirements 1.1, 1.3, 1.4, 1.6).
 *
 * The admin shell authenticates with HTTP Basic auth against the fixed username
 * `admin`; the only secret the operator supplies is the password. Sign-in is a
 * bootstrap `GET /admin/buckets` whose 200 (carrying the `X-CSRF-Token` response
 * header) flips the in-memory session to authenticated, and whose 401 is a
 * rejected login. This spec intercepts that bootstrap at the network layer to
 * cover, at the UI level, the auth leg of **Property 9** and Requirement 1:
 *
 *   - accepted sign-in lands on the admin screens (1.1);
 *   - a rejected sign-in stays on `/login`, announces the failure, clears the
 *     password, and keeps the fixed identifier (1.4);
 *   - the submit control is disabled while a sign-in is in flight so a second
 *     submit cannot race the first (1.3);
 *   - a 401 on a *later* authenticated request ends the session and returns to
 *     `/login` with the "session is no longer valid" notice (1.6).
 *
 * Route handlers are registered before navigation, and the most-recently-added
 * route matches first (per the console convention). Toast assertions, where
 * used, are scoped to `.toast-title`.
 */
import { expect, test } from "@playwright/test";

import {
  ADMIN_PASSWORD,
  CSRF_HEADER,
  SAMPLE_BUCKETS,
  SAMPLE_CSRF_TOKEN,
  fulfilJson,
  mockAdminApi,
  mockSetupStatus,
} from "./fixtures";

test("an accepted password lands on the admin screens", async ({ page }) => {
  await mockAdminApi(page);
  await mockSetupStatus(page, false);

  await page.goto("/buckets");
  await expect(page).toHaveURL(/\/login$/);

  // The fixed identifier is shown read-only; only the password is entered.
  await expect(page.locator("#login-identifier")).toHaveValue("admin");
  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // The session flips to authenticated: LoginScreen redirects off /login and
  // the guards admit the Buckets screen (Requirement 1.1).
  await expect(page).toHaveURL(/\/buckets$/);
  await expect(page.getByRole("heading", { level: 1, name: "Buckets" })).toBeVisible();
});

test("a rejected password stays on login, clears the password, keeps the identifier", async ({
  page,
}) => {
  // The bootstrap read rejects with 401 — a rejected login, not an expired
  // session (Requirement 1.4).
  await page.route("**/admin/buckets", (route) =>
    fulfilJson(route, { detail: "invalid credentials" }, 401),
  );

  await page.goto("/buckets");
  await expect(page).toHaveURL(/\/login$/);

  await page.locator("#login-password").fill("wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  // Stays on login with an assertive error, the password cleared, and the fixed
  // identifier retained.
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("alert")).toContainText(/authentication failed/i);
  await expect(page.locator("#login-password")).toHaveValue("");
  await expect(page.locator("#login-identifier")).toHaveValue("admin");
});

test("the submit control is disabled while a sign-in is in flight (Req 1.3)", async ({ page }) => {
  // Hold the bootstrap response open so the in-flight state is observable, then
  // release it so the sign-in completes.
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockSetupStatus(page, false);
  await page.route("**/admin/buckets", async (route) => {
    await gate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { [CSRF_HEADER]: SAMPLE_CSRF_TOKEN },
      body: JSON.stringify(SAMPLE_BUCKETS),
    });
  });

  await page.goto("/buckets");
  await expect(page).toHaveURL(/\/login$/);

  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  const submit = page.getByRole("button", { name: /Sign/ });
  await submit.click();

  // While the bootstrap is pending the button is disabled and shows progress,
  // so a second submit cannot race the first (Requirement 1.3).
  await expect(submit).toBeDisabled();
  await expect(submit).toHaveText(/Signing in/);

  // Release the response: the sign-in completes and lands on the admin screens.
  release();
  await expect(page).toHaveURL(/\/buckets$/);
});

test("a 401 on a later request ends the session and re-prompts login (Req 1.6)", async ({
  page,
}) => {
  // Sign in successfully, land on /buckets, then navigate to Settings via the
  // sidebar (client-side) so the authenticated GET /admin/settings runs. That
  // request returns 401, which trips the client's global unauthorized hook →
  // signOut("expired") → redirect to login (Requirement 1.6/1.8).
  await mockAdminApi(page);
  await mockSetupStatus(page, false);
  // Register the 401 settings handler last so it wins over mockAdminApi's.
  await page.route("**/admin/settings", (route) =>
    fulfilJson(route, { detail: "token expired" }, 401),
  );

  await page.goto("/buckets");
  await expect(page).toHaveURL(/\/login$/);
  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/buckets$/);

  // The Settings screen's first read 401s and ends the session.
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("alert")).toContainText(/no longer valid/i);
});
