/**
 * FinalizeStep — the first-run wizard's final step (admin-dashboard
 * Requirements 3.8–3.12, 11.2, 11.3; design: "SetupWizard — FinalizeStep").
 *
 * This step completes setup through a React Query mutation against
 * `POST /admin/setup/finalize` (no request body — the backend finalises the
 * in-process wizard session it accumulated across the earlier steps). It owns:
 *
 *   - **Submit + in-flight guard (3.8)** — a single "Complete setup" action
 *     fires the mutation; while it is in flight the control is disabled so the
 *     operator cannot resubmit (the backend rejects a second finalise anyway,
 *     but the UI prevents the double POST).
 *   - **Recovery Kit download (3.9, 11.2)** — on success the Recovery Kit is
 *     offered as a downloadable JSON bundle built in-memory from the result.
 *     The bundle carries the master-key *verifier* (never the master key), the
 *     Telegram session credentials, and the bucket channel assignments.
 *   - **One-time secret (3.10, 11.3)** — the first access key's
 *     `secret_access_key` is shown exactly once with a copy-to-clipboard action.
 *     It lives only in this component's state and is cleared on unmount (the
 *     operator navigating away), so it never lingers in the DOM or any store.
 *   - **Failure + retry (3.11)** — a failed finalise keeps the operator on the
 *     step with a readable error (surfaced through the wizard's shared
 *     `errorMessage` region) and the "Complete setup" action enabled to retry.
 *   - **Leave the wizard (3.12)** — on success a "Go to dashboard" control
 *     invalidates {@link SETUP_STATUS_QUERY_KEY} (so the next `RequireSetup`
 *     read reports `setup_required = false`) and navigates to the admin screens.
 *
 * The retained {@link FinalizeStepState} slice tracks only the coarse phase; the
 * secret and Recovery Kit are deliberately *not* stored in the wizard state so
 * navigating away drops the secret (Requirements 3.10, 11.3). This step consumes
 * the shared {@link StepComponentProps} contract so the wizard shell wiring is
 * unchanged.
 *
 * Accessibility: the error and success regions use `role="alert"`; the download
 * and copy controls are plain buttons with programmatic labels; the one-time
 * secret is rendered in a `<code>` block that is announced in place.
 */
import { useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { SETUP_STATUS_QUERY_KEY, type SetupStatus } from "../api/setup";
import { useFinalizeSetup, type FinalizeResult } from "../api/setup-finalize";
import type { FinalizeStepState, StepComponentProps } from "./types";

/** Admin route the wizard hands off to once setup is complete (Requirement 3.12). */
const ADMIN_HOME_ROUTE = "/buckets";

/**
 * Turn an unknown thrown value into an operator-readable message (Requirement
 * 3.11). The Admin API reports failures as JSON `{ "detail": ... }` (a 409 for
 * an incomplete wizard carries a plain-string detail); fall back to the
 * axios/Error message otherwise.
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
  return "Setup could not be completed. Please try again.";
}

/**
 * Build the downloadable Recovery Kit bundle (Requirements 3.9, 11.2).
 *
 * Only the Recovery Kit is serialised — the access-key *secret* is never written
 * to the download, and the master key never appears anywhere in the result, so
 * the bundle carries the verifier (not the key). The JSON is pretty-printed so a
 * recovering operator can read it directly.
 */
function recoveryKitJson(result: FinalizeResult): string {
  return JSON.stringify(
    {
      master_key_verifier: result.recovery_kit.master_key_verifier,
      telegram_session: result.recovery_kit.telegram_session,
      buckets: result.recovery_kit.buckets.map((bucket) => ({
        bucket_name: bucket.bucket_name,
        channel_id: bucket.channel_id,
      })),
    },
    null,
    2,
  );
}

export function FinalizeStep(props: StepComponentProps<FinalizeStepState>): JSX.Element {
  const { onPatch, onBack } = props;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const finalize = useFinalizeSetup();

  // The one-time finalize result. Held only in component state; set on success
  // and cleared on unmount so the secret never lingers (Req 3.10, 11.3).
  const [result, setResult] = useState<FinalizeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // Keep the latest patch callback in a ref so the unmount cleanup can reset the
  // retained phase without re-running when `onPatch` identity changes.
  const onPatchRef = useRef(onPatch);
  onPatchRef.current = onPatch;

  // Defence in depth for Requirement 3.10 / 11.3: when the step unmounts (the
  // operator navigates away, including the success hand-off to the dashboard),
  // drop the result — and therefore the secret — from memory. React also removes
  // it from the DOM on unmount.
  useEffect(() => {
    return () => {
      setResult(null);
    };
  }, []);

  const onFinalize = useCallback(() => {
    if (finalize.isPending) {
      return;
    }
    setError(null);
    onPatchRef.current({ phase: "submitting" });
    finalize.mutate(undefined, {
      onSuccess: (data) => {
        setResult(data);
        setCopied(false);
        onPatchRef.current({ phase: "result" });
      },
      onError: (err) => {
        // Stay on the step with a readable error; the action stays enabled so
        // the operator can retry (Req 3.11).
        setError(errorMessage(err));
        onPatchRef.current({ phase: "error" });
      },
    });
  }, [finalize]);

  const onCopySecret = useCallback(() => {
    if (!result) {
      return;
    }
    const secret = result.secret_access_key;
    void navigator.clipboard
      ?.writeText(secret)
      .then(() => setCopied(true))
      .catch(() => setCopied(false));
  }, [result]);

  const onDownloadKit = useCallback(() => {
    if (!result) {
      return;
    }
    const blob = new Blob([recoveryKitJson(result)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "tgobjects3-recovery-kit.json";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }, [result]);

  const onGoToDashboard = useCallback(() => {
    // Seed the setup-status cache with `setup_required: false` *synchronously*
    // so the `RequireSetup` guard reads it on the very next render and admits
    // the admin screens instead of bouncing the just-finalised operator back to
    // the wizard on a stale `true` (Req 3.12). Invalidating as well keeps a
    // later background refetch honest. Clearing the result drops the secret
    // before the hand-off.
    queryClient.setQueryData<SetupStatus>(SETUP_STATUS_QUERY_KEY, { setup_required: false });
    void queryClient.invalidateQueries({ queryKey: SETUP_STATUS_QUERY_KEY });
    setResult(null);
    navigate(ADMIN_HOME_ROUTE, { replace: true });
  }, [queryClient, navigate]);

  // --- Success view: Recovery Kit download + one-time secret (Req 3.9, 3.10) ---
  if (result) {
    return (
      <div className="setup-step-body">
        <h2 className="setup-step-title">Setup complete</h2>
        <p className="setup-step-intro" role="alert">
          Setup is complete. Download the Recovery Kit and copy the access-key secret now — the
          secret is shown only once and cannot be retrieved again.
        </p>

        <section className="finalize-kit" aria-label="Recovery Kit">
          <h3 className="finalize-subtitle">Recovery Kit</h3>
          <p className="finalize-description">
            The Recovery Kit holds the master-key verifier (not the master key itself), the Telegram
            session credentials, and your bucket channel assignments. Store it securely — the
            recovery CLI needs it to rebuild the index.
          </p>
          <button type="button" className="btn btn--primary" onClick={onDownloadKit}>
            Download Recovery Kit
          </button>
        </section>

        <section className="finalize-secret" aria-label="First access key">
          <h3 className="finalize-subtitle">First access key</h3>
          <dl className="keys-secret">
            <div className="keys-secret-row">
              <dt>Access key id</dt>
              <dd>
                <code>{result.access_key_id}</code>
              </dd>
            </div>
            <div className="keys-secret-row">
              <dt>Secret access key</dt>
              <dd>
                <code className="keys-secret-value">{result.secret_access_key}</code>
              </dd>
            </div>
          </dl>
          <button type="button" className="btn" onClick={onCopySecret}>
            {copied ? "Copied" : "Copy secret"}
          </button>
        </section>

        <div className="setup-step-actions">
          <button type="button" className="btn btn--primary" onClick={onGoToDashboard}>
            Go to dashboard
          </button>
        </div>
      </div>
    );
  }

  // --- Submit view: finalise action, in-flight guard, failure + retry ---
  return (
    <div className="setup-step-body">
      <h2 className="setup-step-title">Finalize</h2>
      <p className="setup-step-intro">
        Completing setup creates your first S3 access key and generates the Recovery Kit. The secret
        is shown only once, so be ready to copy it.
      </p>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="setup-step-actions">
        {onBack ? (
          <button type="button" className="btn" onClick={onBack} disabled={finalize.isPending}>
            Back
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn--primary"
          onClick={onFinalize}
          disabled={finalize.isPending}
        >
          {finalize.isPending ? "Completing…" : error ? "Retry" : "Complete setup"}
        </button>
      </div>
    </div>
  );
}
