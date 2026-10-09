import { type BucketStats, useBuckets } from "../api/buckets";
import { formatBytes, formatCount } from "../lib/format";

/**
 * Bucket List screen (Requirement 19.1).
 *
 * Lists every bucket with its object count, human-readable stored bytes, and
 * pending-write count, fetched from `GET /admin/buckets`. The underlying query
 * auto-refreshes on an interval (see `useBuckets`) so the view tracks
 * background commits and GC without a manual reload.
 */
export function BucketsScreen(): JSX.Element {
  const query = useBuckets();

  return (
    <section className="screen">
      <header className="screen-header">
        <div>
          <h1 className="screen-title">Buckets</h1>
          <p className="screen-description">
            Per-bucket object count, stored bytes, and pending-write count.
          </p>
        </div>
        {query.isFetching ? (
          <span className="screen-badge" role="status">
            Refreshing…
          </span>
        ) : null}
      </header>
      <BucketsBody query={query} />
    </section>
  );
}

/**
 * Render the loading / error / empty / table states. Narrows on the query's
 * discriminated status union directly so `data` is known to be defined before
 * the table reads it (TanStack Query v5 result typing).
 */
function BucketsBody(props: { query: ReturnType<typeof useBuckets> }): JSX.Element {
  const { query } = props;

  if (query.isPending) {
    return (
      <p className="screen-state" role="status">
        Loading buckets…
      </p>
    );
  }

  if (query.isError) {
    return (
      <p className="screen-state screen-state--error" role="alert">
        Failed to load buckets: {query.error.message}
      </p>
    );
  }

  if (query.data.length === 0) {
    return <p className="screen-state">No buckets yet.</p>;
  }

  return <BucketTable buckets={query.data} />;
}

function BucketTable(props: { buckets: readonly BucketStats[] }): JSX.Element {
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Name</th>
          <th scope="col" className="data-table__num">
            Objects
          </th>
          <th scope="col" className="data-table__num">
            Stored
          </th>
          <th scope="col" className="data-table__num">
            Pending writes
          </th>
        </tr>
      </thead>
      <tbody>
        {props.buckets.map((bucket) => (
          <tr key={bucket.name}>
            <td className="data-table__name">{bucket.name}</td>
            <td className="data-table__num">{formatCount(bucket.object_count)}</td>
            <td className="data-table__num">{formatBytes(bucket.stored_bytes)}</td>
            <td className="data-table__num">{formatCount(bucket.pending_write_count)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
