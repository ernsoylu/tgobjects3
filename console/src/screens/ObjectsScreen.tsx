import * as Label from "@radix-ui/react-label";
import { useMemo, useState } from "react";

import { useBucketNames, useObjectsPage, type ObjectEntry } from "../api/objects";

/** Binary byte-size units, ascending (Requirement 19.2 human-readable sizes). */
const SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/**
 * Format a byte count as a human-readable size (e.g. `1.5 MB`). Uses binary
 * (1024) steps and trims to at most one decimal place, matching the units the
 * Console shows elsewhere.
 */
function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < SIZE_UNITS.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = Math.round(value * 10) / 10;
  return `${rounded} ${SIZE_UNITS[unitIndex]}`;
}

/** Format an ISO-8601 timestamp for display, falling back to the raw string. */
function formatLastModified(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return parsed.toLocaleString();
}

/** Render one object row's ETag, which the backend may return as `null`. */
function formatEtag(etag: string | null): string {
  return etag ?? "—";
}

/**
 * Object Browser screen (Requirement 19.2, task 44.2).
 *
 * Lets the operator pick a bucket, filter by key prefix, and page through the
 * bucket's committed objects 100 at a time. Data comes from `GET /admin/objects`
 * via {@link useObjectsPage}; the bucket selector is fed by
 * {@link useBucketNames} (`GET /admin/buckets`).
 *
 * Pagination follows the backend's opaque-cursor model: each page returns a
 * `next_start_after` cursor (or `null` on the last page). We keep a stack of the
 * cursors used to reach the current page so the operator can move forward (push
 * the current page's `next_start_after`) and back (pop). Changing the bucket or
 * prefix resets the stack to page one.
 */
export function ObjectsScreen(): JSX.Element {
  const bucketsQuery = useBucketNames();
  const bucketNames = useMemo(() => bucketsQuery.data ?? [], [bucketsQuery.data]);

  const [bucket, setBucket] = useState("");
  const [prefix, setPrefix] = useState("");
  // `cursorStack[i]` is the `start_after` used to fetch page `i`; the first page
  // uses `null`. The current page is always the last element.
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([null]);

  // Default the selection to the first bucket once the list loads, without an
  // effect: derive the effective bucket for the query from state + loaded names.
  const effectiveBucket = bucket !== "" ? bucket : (bucketNames[0] ?? "");

  const currentCursor = cursorStack[cursorStack.length - 1] ?? null;
  const objectsQuery = useObjectsPage({
    bucket: effectiveBucket,
    prefix,
    startAfter: currentCursor,
  });

  const page = objectsQuery.data;
  const pageNumber = cursorStack.length;
  const hasNextPage = page?.next_start_after != null;
  const hasPreviousPage = cursorStack.length > 1;

  function resetPaging(): void {
    setCursorStack([null]);
  }

  function handleBucketChange(next: string): void {
    setBucket(next);
    resetPaging();
  }

  function handlePrefixChange(next: string): void {
    setPrefix(next);
    resetPaging();
  }

  function goToNextPage(): void {
    const next = page?.next_start_after ?? null;
    if (next == null) {
      return;
    }
    setCursorStack((stack) => [...stack, next]);
  }

  function goToPreviousPage(): void {
    setCursorStack((stack) => (stack.length > 1 ? stack.slice(0, -1) : stack));
  }

  return (
    <section className="screen">
      <h1 className="screen-title">Objects</h1>
      <p className="screen-description">
        Browse committed objects in a bucket, filtered by key prefix.
      </p>

      <div className="toolbar">
        <div className="field">
          <Label.Root className="field-label" htmlFor="object-bucket">
            Bucket
          </Label.Root>
          <select
            id="object-bucket"
            className="field-control"
            value={effectiveBucket}
            disabled={bucketsQuery.isLoading || bucketNames.length === 0}
            onChange={(event) => handleBucketChange(event.target.value)}
          >
            {bucketNames.length === 0 ? (
              <option value="">No buckets</option>
            ) : (
              bucketNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))
            )}
          </select>
        </div>

        <div className="field field--grow">
          <Label.Root className="field-label" htmlFor="object-prefix">
            Prefix
          </Label.Root>
          <input
            id="object-prefix"
            className="field-control"
            type="text"
            placeholder="e.g. photos/2024/"
            value={prefix}
            onChange={(event) => handlePrefixChange(event.target.value)}
          />
        </div>
      </div>

      <ObjectsTableBody
        isLoading={objectsQuery.isLoading}
        isError={objectsQuery.isError}
        error={objectsQuery.error}
        objects={page?.objects ?? []}
      />

      <div className="pagination">
        <span className="pagination-info">Page {pageNumber}</span>
        <div className="pagination-controls">
          <button
            type="button"
            className="button"
            onClick={goToPreviousPage}
            disabled={!hasPreviousPage || objectsQuery.isFetching}
          >
            Previous
          </button>
          <button
            type="button"
            className="button"
            onClick={goToNextPage}
            disabled={!hasNextPage || objectsQuery.isFetching}
          >
            Next
          </button>
        </div>
      </div>
    </section>
  );
}

/** Props for the object table body, split out to keep the screen readable. */
interface ObjectsTableBodyProps {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  objects: ObjectEntry[];
}

/**
 * Renders the object table along with the loading, error, and empty states.
 * Kept as a child component so the parent screen stays focused on selection and
 * pagination state.
 */
function ObjectsTableBody(props: ObjectsTableBodyProps): JSX.Element {
  if (props.isError) {
    const message = props.error instanceof Error ? props.error.message : "Failed to load objects.";
    return (
      <p className="state state--error" role="alert">
        {message}
      </p>
    );
  }

  if (props.isLoading) {
    return <p className="state">Loading objects…</p>;
  }

  if (props.objects.length === 0) {
    return <p className="state state--empty">No objects match this prefix.</p>;
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Key</th>
          <th scope="col" className="data-table-cell--numeric">
            Size
          </th>
          <th scope="col">ETag</th>
          <th scope="col">Last modified</th>
        </tr>
      </thead>
      <tbody>
        {props.objects.map((object) => (
          <tr key={object.key}>
            <td className="data-table-cell--key">{object.key}</td>
            <td className="data-table-cell--numeric">{formatBytes(object.size)}</td>
            <td className="data-table-cell--mono">{formatEtag(object.etag)}</td>
            <td>{formatLastModified(object.last_modified)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
