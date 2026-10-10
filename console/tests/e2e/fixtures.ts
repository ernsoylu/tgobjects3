/**
 * Shared Admin-API mock fixtures for the Console end-to-end suite (task 45.1).
 *
 * The Console reaches the gateway through relative `/admin/*` routes (see
 * `src/api/client.ts`). Rather than stand up a live backend, each spec installs
 * a `page.route` handler built from the helpers here, so every request is
 * fulfilled with a deterministic JSON payload whose shape matches the Admin
 * API's models exactly (snake_case fields; see the `src/api/*` type
 * declarations). This keeps the tests hermetic and lets a single run cover both
 * success and error boundaries.
 *
 * The JSON shapes mirror:
 *   - `BucketStats`   — `src/api/buckets.ts`   (Requirement 19.1)
 *   - `ObjectsPage`   — `src/api/objects.ts`   (Requirement 19.2)
 *   - `JobEntry`      — `src/api/jobs.ts`       (Requirement 19.3)
 *   - `RecoveryStatus`— `src/api/recovery.ts`   (Requirement 19.4)
 *   - `SettingsValues`— `src/api/settings.ts`   (Requirements 19.5–19.7)
 */
import { expect, type Page, type Route } from "@playwright/test";

/**
 * The CSRF token header the Admin API echoes on authenticated responses and
 * expects back on unsafe requests (mirrors `CSRF_HEADER` in `src/api/client.ts`).
 * The sign-in bootstrap `GET /admin/buckets` only succeeds in caching a token if
 * its response carries this header, so every authenticated-path mock sets it.
 */
export const CSRF_HEADER = "X-CSRF-Token";

/** A deterministic CSRF token value handed back on the bootstrap read. */
export const SAMPLE_CSRF_TOKEN = "e2e-csrf-token";

/** The admin password the auth helpers sign in with (any non-empty value works). */
export const ADMIN_PASSWORD = "correct horse battery staple";

/** One bucket row, matching the Admin API `BucketStats` model. */
export interface BucketStats {
  name: string;
  object_count: number;
  stored_bytes: number;
  pending_write_count: number;
}

/** One committed object, matching the Admin API `ObjectEntry` model. */
export interface ObjectEntry {
  key: string;
  size: number;
  etag: string | null;
  last_modified: string;
}

/** One page of the object browser, matching the Admin API `ObjectsPage` model. */
export interface ObjectsPage {
  bucket: string;
  prefix: string;
  objects: ObjectEntry[];
  next_start_after: string | null;
}

/** One upload job, matching the Admin API `JobEntry` model. */
export interface JobEntry {
  id: string;
  generation_id: string;
  state: string;
  attempts: number;
  last_error: string | null;
}

/** The five runtime settings, matching the Admin API `SettingsBody` model. */
export interface SettingsValues {
  staging_max_size: number;
  cache_max_size: number;
  cache_half_life: number;
  read_ahead: number;
  upload_concurrency: number;
}

/** Fulfil a route with a JSON body and status (default 200). */
export async function fulfilJson(
  route: Route,
  body: unknown,
  status = 200,
  headers?: Record<string, string>,
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
    ...(headers ? { headers } : {}),
  });
}

/**
 * Fulfil the bootstrap `GET /admin/buckets` read with an `X-CSRF-Token` response
 * header so the client caches the token exactly as the real backend would. Used
 * both by {@link mockAdminApi} and by {@link signIn} so the authenticated path
 * can issue unsafe (CSRF-bearing) requests afterwards.
 */
export async function fulfilBootstrap(route: Route, buckets: BucketStats[]): Promise<void> {
  await fulfilJson(route, buckets, 200, { [CSRF_HEADER]: SAMPLE_CSRF_TOKEN });
}

/** A representative bucket list used by the navigation and bucket specs. */
export const SAMPLE_BUCKETS: BucketStats[] = [
  {
    name: "photos",
    object_count: 1234,
    stored_bytes: 5_368_709_120,
    pending_write_count: 2,
  },
  {
    name: "backups",
    object_count: 7,
    stored_bytes: 1024,
    pending_write_count: 0,
  },
];

/** A single object-browser page (no further pages) used by the objects spec. */
export const SAMPLE_OBJECTS_PAGE: ObjectsPage = {
  bucket: "photos",
  prefix: "",
  objects: [
    {
      key: "photos/2024/img-001.jpg",
      size: 2_097_152,
      etag: '"abc123"',
      last_modified: "2024-05-01T12:00:00Z",
    },
    {
      key: "photos/2024/img-002.jpg",
      size: 512,
      etag: null,
      last_modified: "2024-05-02T09:30:00Z",
    },
  ],
  next_start_after: null,
};

/** Jobs including one FAILED entry the retry spec acts on. */
export const SAMPLE_JOBS: JobEntry[] = [
  {
    id: "job-committed-1",
    generation_id: "gen-aaaa",
    state: "COMMITTED",
    attempts: 1,
    last_error: null,
  },
  {
    id: "job-failed-1",
    generation_id: "gen-bbbb",
    state: "FAILED",
    attempts: 3,
    last_error: "flood wait: 42s",
  },
];

/** The current settings snapshot used to seed the settings form. */
export const SAMPLE_SETTINGS: SettingsValues = {
  staging_max_size: 17_179_869_184,
  cache_max_size: 8_589_934_592,
  cache_half_life: 3600,
  read_ahead: 8,
  upload_concurrency: 4,
};

/**
 * Install the default happy-path Admin API mock covering every read endpoint
 * the five screens call. Individual specs can register more specific handlers
 * (e.g. a write endpoint) BEFORE calling this, since Playwright matches the
 * most recently added route first.
 */
export async function mockAdminApi(
  page: Page,
  overrides: {
    buckets?: BucketStats[];
    objectsPage?: ObjectsPage;
    jobs?: JobEntry[];
    settings?: SettingsValues;
    recoveryStatus?: unknown;
    recoveryStatusCode?: number;
  } = {},
): Promise<void> {
  const buckets = overrides.buckets ?? SAMPLE_BUCKETS;
  const objectsPage = overrides.objectsPage ?? SAMPLE_OBJECTS_PAGE;
  const jobs = overrides.jobs ?? SAMPLE_JOBS;
  const settings = overrides.settings ?? SAMPLE_SETTINGS;

  // `GET /admin/buckets` doubles as the auth bootstrap read, so it must echo the
  // CSRF token header for sign-in to cache it (Requirement 1.2). The real
  // backend returns the header on every authenticated response.
  await page.route("**/admin/buckets", (route) => fulfilBootstrap(route, buckets));
  await page.route("**/admin/objects*", (route) => fulfilJson(route, objectsPage));
  await page.route("**/admin/jobs", (route) => fulfilJson(route, jobs));
  await page.route("**/admin/settings", (route) => {
    if (route.request().method() === "GET") {
      return fulfilJson(route, settings);
    }
    // Default: echo back a PUT body as the applied settings.
    return fulfilJson(route, settings);
  });
  await page.route("**/admin/recovery", (route) =>
    fulfilJson(route, overrides.recoveryStatus ?? null, overrides.recoveryStatusCode ?? 404),
  );
}

/**
 * Mock `POST /admin/setup/status` — the probe `RequireSetup` runs on entry to a
 * guarded admin route. `setup_required: false` lets authenticated navigation
 * reach the admin screens; `true` diverts every guarded route into the wizard
 * (design: "Setup-required routing"; Property 9).
 */
export async function mockSetupStatus(page: Page, setupRequired: boolean): Promise<void> {
  await page.route("**/admin/setup/status", (route) =>
    fulfilJson(route, { setup_required: setupRequired }),
  );
}

/**
 * Drive the login UI to an authenticated session, then land on the admin
 * screens.
 *
 * The pre-existing screen specs navigate straight to a screen (e.g. `/settings`)
 * which, under the auth/setup guards, now redirects to `/login`. This helper
 * installs the setup-status mock (`setup_required: false` by default so the
 * guards admit the admin screens), navigates to the destination (which bounces
 * to `/login`), fills the single password field, and submits. On success the
 * LoginScreen redirects to the admin area (landing on `/buckets`); the helper
 * then performs a *client-side* sidebar navigation to the requested `to` screen
 * so the in-memory session survives (a full `page.goto` after sign-in would
 * drop the memory-only credentials and bounce back to `/login`).
 *
 * The caller must register its screen's own `page.route` handlers (and
 * `mockAdminApi`, whose `GET /admin/buckets` serves the CSRF-bearing bootstrap)
 * BEFORE calling this, since Playwright matches the most recently added route
 * first.
 */
export async function signIn(
  page: Page,
  options: {
    to?: string;
    setupRequired?: boolean;
    buckets?: BucketStats[];
    /**
     * Skip registering the bootstrap `GET /admin/buckets` route. Set this when
     * the spec has already registered its own CSRF-bearing `/admin/buckets`
     * handler (e.g. the Buckets screen specs, which assert on specific rows and
     * need their payload to serve both bootstrap and the screen fetch).
     */
    skipBootstrapRoute?: boolean;
  } = {},
): Promise<void> {
  const destination = options.to ?? "/buckets";
  await mockSetupStatus(page, options.setupRequired ?? false);

  // Install the bootstrap `GET /admin/buckets` with the CSRF header so sign-in
  // caches the token, unless the spec already registered its own handler (which
  // must then carry the CSRF header itself — see `fulfilBootstrap`).
  if (!options.skipBootstrapRoute) {
    await page.route("**/admin/buckets", (route) =>
      fulfilBootstrap(route, options.buckets ?? SAMPLE_BUCKETS),
    );
  }

  // Navigate to the destination; while unauthenticated the auth guard performs
  // a *client-side* redirect to `/login` (same document).
  await page.goto(destination);
  await expect(page).toHaveURL(/\/login$/);

  await page.locator("#login-password").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // On success the session flips to authenticated: the LoginScreen redirects off
  // `/login`. With setup required the guards divert to the wizard — stop there.
  await expect(page).not.toHaveURL(/\/login$/);
  if (options.setupRequired === true) {
    return;
  }

  // Otherwise we land on the admin area (`/buckets`). If a different screen was
  // requested, reach it via the sidebar link (client-side nav preserves the
  // in-memory session). The nav labels mirror `AppLayout`'s NAV_ITEMS.
  const navLabelByPath: Record<string, string> = {
    "/buckets": "Buckets",
    "/objects": "Objects",
    "/jobs": "Jobs",
    "/recovery": "Recovery",
    "/settings": "Settings",
  };
  const label = navLabelByPath[destination];
  if (label !== undefined && destination !== "/buckets") {
    await page.getByRole("link", { name: label }).click();
    await expect(page).toHaveURL(new RegExp(`${destination}$`));
  }
}
