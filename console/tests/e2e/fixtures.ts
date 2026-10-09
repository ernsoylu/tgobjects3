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
import type { Page, Route } from "@playwright/test";

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
export async function fulfilJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
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

  await page.route("**/admin/buckets", (route) => fulfilJson(route, buckets));
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
