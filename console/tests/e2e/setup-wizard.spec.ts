/**
 * Setup Wizard critical path (task 15.1; Requirements 2.4, 3.7, 3.9, 3.10, 3.12;
 * **Properties 2 and 5** at the UI level).
 *
 * The first-run Setup Wizard walks Telegram → Buckets → Config → Finalize. This
 * spec reaches the wizard by authenticating while the gateway reports
 * `setup_required: true` (so the guards divert to `/setup`), then intercepts
 * each step's Admin API call at the network layer with deterministic fixtures
 * whose shapes mirror the backend models exactly (snake_case).
 *
 * Coverage:
 *   - **Telegram (Property 2 at the UI)** — `needs_code` → code → success, and
 *     three wrong codes aborting back to credential entry (Requirement 2.4);
 *   - **Buckets (Requirement 3.7)** — an already-initialised channel offers
 *     adopt vs reinitialise, and reinitialise is gated behind an explicit
 *     confirmation before the step re-submits;
 *   - **Config (Property 5 at the UI)** — an out-of-range field keeps the step
 *     put and names the field and its range; all-in-range advances;
 *   - **Finalize (Requirements 3.9/3.10/3.12)** — the one-time access-key secret
 *     and the Recovery Kit are shown once, then the wizard hands off to the
 *     admin screens.
 *
 * Route handlers are registered before navigation; the most-recently-added
 * route matches first.
 */
import { expect, test, type Page, type Route } from "@playwright/test";

import { ADMIN_PASSWORD, fulfilJson, mockAdminApi, mockSetupStatus } from "./fixtures";

/** In-range config values (staging 1 MiB–1 TiB, cache 1 MiB–100 GiB). */
const VALID_STAGING_SIZE = String(16 * 1024 ** 3); // 16 GiB
const VALID_CACHE_SIZE = String(8 * 1024 ** 3); // 8 GiB
const VALID_MASTER_KEY = "a-sufficiently-long-master-key"; // ≥ 16 chars

/**
 * Authenticate and land on the wizard. `setup_required: true` diverts the
 * authenticated operator to `/setup` (the deep link is irrelevant — LoginScreen
 * routes to `/`, which the setup guard diverts).
 */
async function openWizard(page: Page): Promise<void> {
  await mockSetupStatus(page, true);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/setup$/);
}

/** Fill and submit the Telegram credentials leg. */
async function submitTelegramCredentials(page: Page): Promise<void> {
  await page.locator("#telegram-api-id").fill("12345");
  await page.locator("#telegram-api-hash").fill("deadbeefdeadbeefdeadbeefdeadbeef");
  await page.locator("#telegram-login").fill("+15551234567");
  await page.getByRole("button", { name: "Sign in" }).click();
}

test.beforeEach(async ({ page }) => {
  await mockAdminApi(page);
});

test("Telegram needs_code → code → success advances to Buckets (Property 2)", async ({ page }) => {
  // First POST (credentials, no code) → needs_code with 3 attempts; the second
  // POST (with the code) authenticates.
  await page.route("**/admin/setup/telegram", (route) => {
    const body = route.request().postDataJSON() as { code?: string };
    if (body.code === undefined) {
      return fulfilJson(route, {
        authenticated: false,
        needs_code: true,
        attempts_remaining: 3,
      });
    }
    return fulfilJson(route, { authenticated: true, needs_code: false, attempts_remaining: 3 });
  });

  await openWizard(page);

  await submitTelegramCredentials(page);

  // The code prompt appears with the attempts count.
  await expect(page.locator("#telegram-code")).toBeVisible();
  await expect(page.getByText(/3\s+attempts remaining/)).toBeVisible();

  await page.locator("#telegram-code").fill("55555");
  await page.getByRole("button", { name: "Submit code" }).click();

  // Authenticated: the wizard advances to the Buckets step.
  await expect(page.getByRole("heading", { name: "Assign buckets to channels" })).toBeVisible();
});

test("three wrong codes abort back to credential entry (Property 2 / Req 2.4)", async ({
  page,
}) => {
  // The credentials POST starts the code prompt; each wrong code decrements the
  // count; the third wrong code aborts with HTTP 401 and the backend resets the
  // login, so the step returns to credential entry.
  let codeAttempts = 0;
  await page.route("**/admin/setup/telegram", (route) => {
    const body = route.request().postDataJSON() as { code?: string };
    if (body.code === undefined) {
      return fulfilJson(route, {
        authenticated: false,
        needs_code: true,
        attempts_remaining: 3,
      });
    }
    codeAttempts += 1;
    if (codeAttempts >= 3) {
      return fulfilJson(route, { detail: "too many invalid codes" }, 401);
    }
    return fulfilJson(route, {
      authenticated: false,
      needs_code: true,
      attempts_remaining: 3 - codeAttempts,
    });
  });

  await openWizard(page);
  await submitTelegramCredentials(page);
  await expect(page.locator("#telegram-code")).toBeVisible();

  // First two wrong codes keep the prompt (attempts decrement).
  await page.locator("#telegram-code").fill("00000");
  await page.getByRole("button", { name: "Submit code" }).click();
  await expect(page.getByText(/2\s+attempts remaining/)).toBeVisible();

  await page.locator("#telegram-code").fill("11111");
  await page.getByRole("button", { name: "Submit code" }).click();
  await expect(page.getByText(/1\s+attempt remaining/)).toBeVisible();

  // The third wrong code aborts (401): return to credential entry with an error.
  await page.locator("#telegram-code").fill("22222");
  await page.getByRole("button", { name: "Submit code" }).click();

  await expect(page.locator("#telegram-api-id")).toBeVisible();
  await expect(page.getByRole("alert")).toBeVisible();
});

test("buckets step gates reinitialise behind an explicit confirmation (Req 3.7)", async ({
  page,
}) => {
  // Telegram passes straight through (bot-token style authenticated=true).
  await page.route("**/admin/setup/telegram", (route) =>
    fulfilJson(route, { authenticated: true, needs_code: false, attempts_remaining: 3 }),
  );

  // The first buckets POST (no on_existing) reports an existing manifest that
  // requires a choice; the second (with on_existing) is accepted.
  let bucketsSubmits = 0;
  await page.route("**/admin/setup/buckets", (route) => {
    bucketsSubmits += 1;
    if (bucketsSubmits === 1) {
      return fulfilJson(route, {
        requires_choice: true,
        buckets: [
          {
            bucket_name: "photos",
            channel_id: -100123,
            existing_manifest: true,
            requires_choice: true,
            warning: "Channel -100123 already holds bucket data.",
          },
        ],
      });
    }
    return fulfilJson(route, {
      requires_choice: false,
      buckets: [
        {
          bucket_name: "photos",
          channel_id: -100123,
          existing_manifest: true,
          requires_choice: false,
          warning: null,
        },
      ],
    });
  });

  await openWizard(page);
  await submitTelegramCredentials(page);
  await expect(page.getByRole("heading", { name: "Assign buckets to channels" })).toBeVisible();

  await page.locator("#bucket-name-0").fill("photos");
  await page.locator("#bucket-channel-0").fill("-100123");
  await page.getByRole("button", { name: "Assign buckets" }).click();

  // The existing-channel decision appears; choosing reinitialise reveals the
  // warning and a confirmation the operator must tick before continuing.
  await expect(page.getByText("Existing channels need a decision")).toBeVisible();
  const confirmButton = page.getByRole("button", { name: "Confirm choices" });

  await page.getByRole("radio", { name: /Reinitialise/ }).check();
  // The warning is the per-channel reinitialise alert (scoped to its class so it
  // is not confused with the step's intro copy).
  await expect(page.locator(".buckets-warning")).toContainText("already holds bucket data");
  // Reinitialise stays blocked until the confirmation checkbox is ticked.
  await expect(confirmButton).toBeDisabled();

  await page.getByRole("checkbox").check();
  await expect(confirmButton).toBeEnabled();
  await confirmButton.click();

  // The accepted assignment advances to the Config step.
  await expect(page.getByRole("heading", { name: "Storage configuration" })).toBeVisible();
});

test("config out-of-range field blocks the submit and names the range (Property 5)", async ({
  page,
}) => {
  await page.route("**/admin/setup/telegram", (route) =>
    fulfilJson(route, { authenticated: true, needs_code: false, attempts_remaining: 3 }),
  );
  await page.route("**/admin/setup/buckets", (route) =>
    fulfilJson(route, {
      requires_choice: false,
      buckets: [
        {
          bucket_name: "photos",
          channel_id: -100123,
          existing_manifest: false,
          requires_choice: false,
          warning: null,
        },
      ],
    }),
  );
  // If the step ever POSTs an out-of-range value this would resolve, but the
  // client-side guard must prevent it — the test asserts we stay on the step.
  let configPosted = false;
  await page.route("**/admin/setup/config", (route) => {
    configPosted = true;
    return fulfilJson(route, { ok: true });
  });

  await openWizard(page);
  await submitTelegramCredentials(page);
  await page.locator("#bucket-name-0").fill("photos");
  await page.locator("#bucket-channel-0").fill("-100123");
  await page.getByRole("button", { name: "Assign buckets" }).click();
  await expect(page.getByRole("heading", { name: "Storage configuration" })).toBeVisible();

  // A below-minimum staging size (1 byte, under the 1 MiB floor) must be
  // rejected client-side: the step stays put, names the field + its range, and
  // does not POST.
  await page.locator("#config-staging-path").fill("/var/lib/tgo/staging");
  await page.locator("#config-staging-size").fill("1");
  await page.locator("#config-cache-path").fill("/var/lib/tgo/cache");
  await page.locator("#config-cache-size").fill(VALID_CACHE_SIZE);
  await page.locator("#config-master-key").fill(VALID_MASTER_KEY);
  await page.getByRole("button", { name: "Save configuration" }).click();

  await expect(page.getByRole("heading", { name: "Storage configuration" })).toBeVisible();
  await expect(page.getByText(/staging maximum size must be between/i)).toBeVisible();
  expect(configPosted).toBe(false);

  // Correcting the field to an in-range value advances past Config.
  await page.locator("#config-staging-size").fill(VALID_STAGING_SIZE);
  await page.getByRole("button", { name: "Save configuration" }).click();
  await expect(page.getByRole("heading", { name: "Finalize" })).toBeVisible();
});

test("finalize shows the one-time secret + Recovery Kit, then lands on admin (Req 3.9/3.10/3.12)", async ({
  page,
}) => {
  await page.route("**/admin/setup/telegram", (route) =>
    fulfilJson(route, { authenticated: true, needs_code: false, attempts_remaining: 3 }),
  );
  await page.route("**/admin/setup/buckets", (route) =>
    fulfilJson(route, {
      requires_choice: false,
      buckets: [
        {
          bucket_name: "photos",
          channel_id: -100123,
          existing_manifest: false,
          requires_choice: false,
          warning: null,
        },
      ],
    }),
  );
  await page.route("**/admin/setup/config", (route) => fulfilJson(route, { ok: true }));
  await page.route("**/admin/setup/finalize", (route: Route) =>
    fulfilJson(route, {
      access_key_id: "AKIAEXAMPLE0001",
      secret_access_key: "s3cr3t-shown-once-only",
      recovery_kit: {
        master_key_verifier: "verifier-not-the-key",
        telegram_session: "telethon-session-string",
        buckets: [{ bucket_name: "photos", channel_id: -100123 }],
      },
    }),
  );

  await openWizard(page);

  // Walk Telegram → Buckets → Config.
  await submitTelegramCredentials(page);
  await page.locator("#bucket-name-0").fill("photos");
  await page.locator("#bucket-channel-0").fill("-100123");
  await page.getByRole("button", { name: "Assign buckets" }).click();
  await expect(page.getByRole("heading", { name: "Storage configuration" })).toBeVisible();

  await page.locator("#config-staging-path").fill("/var/lib/tgo/staging");
  await page.locator("#config-staging-size").fill(VALID_STAGING_SIZE);
  await page.locator("#config-cache-path").fill("/var/lib/tgo/cache");
  await page.locator("#config-cache-size").fill(VALID_CACHE_SIZE);
  await page.locator("#config-master-key").fill(VALID_MASTER_KEY);
  await page.getByRole("button", { name: "Save configuration" }).click();
  await expect(page.getByRole("heading", { name: "Finalize" })).toBeVisible();

  await page.getByRole("button", { name: "Complete setup" }).click();

  // Success: the one-time secret and the Recovery Kit download are shown once.
  await expect(page.getByRole("heading", { name: "Setup complete" })).toBeVisible();
  await expect(page.getByText("s3cr3t-shown-once-only")).toBeVisible();
  await expect(page.getByText("AKIAEXAMPLE0001")).toBeVisible();
  await expect(page.getByRole("button", { name: "Download Recovery Kit" })).toBeVisible();

  // Handing off to the dashboard flips setup_required false for the next probe
  // so the admin screens are reached, and the secret leaves the DOM (Req 3.12).
  await mockSetupStatus(page, false);
  await page.getByRole("button", { name: "Go to dashboard" }).click();

  await expect(page).toHaveURL(/\/buckets$/);
  await expect(page.getByText("s3cr3t-shown-once-only")).toHaveCount(0);
});
