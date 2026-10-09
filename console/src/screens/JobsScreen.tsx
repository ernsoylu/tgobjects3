/**
 * Upload Jobs screen (Requirement 19.3).
 *
 * Lists upload jobs returned by `GET /admin/jobs` in a table — id, generation
 * id, state (as a status badge), attempt count, and the last error — and
 * auto-refreshes via the `useJobs` hook's `refetchInterval`. Each FAILED job
 * gets a Retry button that POSTs `POST /admin/jobs/{id}/retry` through the
 * shared admin client (which attaches the CSRF header for unsafe methods); on
 * success the backend transitions the job back to STAGED and the jobs query is
 * invalidated so the table reflects the change. Retry outcomes are surfaced via
 * a Radix toast.
 *
 * The data layer lives in `../api/jobs` so this screen stays presentational.
 */
import * as Toast from "@radix-ui/react-toast";
import axios from "axios";
import { useCallback, useState } from "react";

import { JOB_STATE_FAILED, useJobs, useRetryJob, type JobEntry } from "../api/jobs";

/** A transient notification surfaced after a retry attempt. */
interface Notice {
  readonly title: string;
  readonly description: string;
  readonly variant: "success" | "error";
}

/**
 * Turn an unknown thrown value into an operator-readable message. The Admin API
 * reports failures as JSON `{ "detail": "..." }` (e.g. a 409 for a non-FAILED
 * job); fall back to the axios/Error message when no detail is present.
 */
function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return detail;
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "Unknown error";
}

/** Render a job's state as a coloured badge keyed off the state literal. */
function StateBadge(props: { state: string }): JSX.Element {
  const modifier = props.state.toLowerCase();
  return (
    <span className={`badge badge--${modifier}`} data-state={props.state}>
      {props.state}
    </span>
  );
}

export function JobsScreen(): JSX.Element {
  const jobs = useJobs();
  const retry = useRetryJob();
  const [notice, setNotice] = useState<Notice | null>(null);
  // Radix Toast remounts on an incrementing key so a repeated outcome re-opens
  // the toast rather than silently reusing the still-open one.
  const [noticeKey, setNoticeKey] = useState(0);

  const showNotice = useCallback((next: Notice) => {
    setNotice(next);
    setNoticeKey((key) => key + 1);
  }, []);

  const onRetry = useCallback(
    (job: JobEntry) => {
      retry.mutate(job.id, {
        onSuccess: (updated) => {
          showNotice({
            title: "Retry submitted",
            description: `Job ${updated.id} is now ${updated.state}.`,
            variant: "success",
          });
        },
        onError: (error) => {
          showNotice({
            title: "Retry failed",
            description: errorMessage(error),
            variant: "error",
          });
        },
      });
    },
    [retry, showNotice],
  );

  return (
    <Toast.Provider swipeDirection="right">
      <section className="screen">
        <header className="screen-header">
          <h1 className="screen-title">Upload Jobs</h1>
          <p className="screen-description">
            Upload jobs and their status. Retry a FAILED job to re-queue it for upload.
          </p>
        </header>

        <JobsContent
          jobs={jobs}
          onRetry={onRetry}
          retryingId={retry.isPending ? (retry.variables ?? null) : null}
        />
      </section>

      {notice ? (
        <Toast.Root
          key={noticeKey}
          className={`toast toast--${notice.variant}`}
          duration={6_000}
          onOpenChange={(open) => {
            if (!open) {
              setNotice(null);
            }
          }}
        >
          <Toast.Title className="toast-title">{notice.title}</Toast.Title>
          <Toast.Description className="toast-description">{notice.description}</Toast.Description>
          <Toast.Close className="toast-close" aria-label="Dismiss">
            ×
          </Toast.Close>
        </Toast.Root>
      ) : null}
      <Toast.Viewport className="toast-viewport" />
    </Toast.Provider>
  );
}

/**
 * The loading / error / empty / table body of the screen, split out so the
 * outer component only wires the toast provider and retry handler.
 */
function JobsContent(props: {
  jobs: ReturnType<typeof useJobs>;
  onRetry: (job: JobEntry) => void;
  retryingId: string | null;
}): JSX.Element {
  const { jobs, onRetry, retryingId } = props;

  if (jobs.isPending) {
    return <p className="screen-note">Loading upload jobs…</p>;
  }

  if (jobs.isError) {
    return (
      <div className="screen-error" role="alert">
        <p>Could not load upload jobs: {errorMessage(jobs.error)}</p>
        <button type="button" className="btn" onClick={() => void jobs.refetch()}>
          Try again
        </button>
      </div>
    );
  }

  if (jobs.data.length === 0) {
    return <p className="screen-note">No upload jobs.</p>;
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">Job ID</th>
          <th scope="col">Generation ID</th>
          <th scope="col">State</th>
          <th scope="col" className="data-table__num">
            Attempts
          </th>
          <th scope="col">Last error</th>
          <th scope="col">
            <span className="sr-only">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {jobs.data.map((job) => {
          const canRetry = job.state === JOB_STATE_FAILED;
          const isRetrying = retryingId === job.id;
          return (
            <tr key={job.id}>
              <td className="data-table__mono">{job.id}</td>
              <td className="data-table__mono">{job.generation_id}</td>
              <td>
                <StateBadge state={job.state} />
              </td>
              <td className="data-table__num">{job.attempts}</td>
              <td className="data-table__error">
                {job.last_error ?? <span className="data-table__muted">—</span>}
              </td>
              <td>
                {canRetry ? (
                  <button
                    type="button"
                    className="btn btn--retry"
                    disabled={isRetrying}
                    onClick={() => onRetry(job)}
                  >
                    {isRetrying ? "Retrying…" : "Retry"}
                  </button>
                ) : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
