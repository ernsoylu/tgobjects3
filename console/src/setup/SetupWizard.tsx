/**
 * Setup Wizard shell (admin-dashboard Requirements 3.2, 3.3, 3.6; design:
 * "SetupWizard — console/src/setup/ (new)" and the "Console SPA component
 * diagram").
 *
 * This is the wizard container (task 7.1). It owns the immutable
 * {@link WizardState} step/state model and composes a {@link StepIndicator}
 * above the four ordered steps — Telegram → Buckets → Config → Finalize
 * (Requirement 3.2). The `/setup` route in `App.tsx` renders this component
 * behind `RequireAuth`, replacing the task-5.2 placeholder at the same path, so
 * routing stays wired.
 *
 * State model
 * -----------
 * `WizardState.current` is the displayed step and `furthestReached` is the
 * furthest step the operator has advanced to. Each per-step slice retains the
 * values entered on that step. Advancing (`handleComplete`) moves `current`
 * forward and extends `furthestReached`; a patch (`patchStep`) writes edited
 * form values back into the retained slice so they survive navigation.
 *
 * Navigation (Requirement 3.6)
 * ----------------------------
 * The operator may return to any already-completed earlier step: that step
 * re-renders from its retained slice, showing the previously entered values.
 * From there they may move forward again, but only up to `furthestReached` —
 * forward navigation never jumps past a step not yet reached. The indicator
 * reflects current/completed state throughout (Requirement 3.3).
 *
 * Step ownership
 * --------------
 * The four step components are owned by sibling tasks (7.2 TelegramStep, 8.1
 * BucketsStep, 8.2 ConfigStep, 8.4 FinalizeStep). Task 7.1 ships lightweight
 * placeholders at the design-named paths (`./TelegramStep` etc.); when a sibling
 * task lands its real component at the same path, it drops in here with no change
 * to this shell, because every step consumes the shared {@link StepComponentProps}
 * contract and its retained slice from {@link ./types}.
 */
import { useCallback, useMemo, useState } from "react";

import { BucketsStep, type BucketRow } from "./BucketsStep";
import { ConfigStep } from "./ConfigStep";
import { FinalizeStep } from "./FinalizeStep";
import { StepIndicator } from "./StepIndicator";
import { TelegramStep } from "./TelegramStep";
import {
  WIZARD_STEP_LABELS,
  WIZARD_STEP_ORDER,
  stepIndex,
  type BucketsStepState,
  type ConfigStepState,
  type FinalizeStepState,
  type TelegramStepState,
  type WizardState,
  type WizardStepId,
} from "./types";

/** Initial retained Telegram slice: empty credential form at the creds phase. */
const INITIAL_TELEGRAM: TelegramStepState = {
  phase: "creds",
  apiId: "",
  apiHash: "",
  login: "",
  code: "",
  attemptsRemaining: 3,
};

const INITIAL_BUCKETS: BucketsStepState = { assignments: [] };

const INITIAL_CONFIG: ConfigStepState = {
  stagingPath: "",
  stagingMaxSize: "",
  cachePath: "",
  cacheMaxSize: "",
  masterKey: "",
};

const INITIAL_FINALIZE: FinalizeStepState = { phase: "idle" };

/** The wizard's starting state: on the first step, nothing reached beyond it. */
const INITIAL_STATE: WizardState = {
  current: "telegram",
  furthestReached: "telegram",
  telegram: INITIAL_TELEGRAM,
  buckets: INITIAL_BUCKETS,
  config: INITIAL_CONFIG,
  finalize: INITIAL_FINALIZE,
};

/** Return the step after `step`, or `null` when `step` is the last step. */
function nextStep(step: WizardStepId): WizardStepId | null {
  const next = WIZARD_STEP_ORDER[stepIndex(step) + 1];
  return next ?? null;
}

/** Return the step before `step`, or `null` when `step` is the first step. */
function previousStep(step: WizardStepId): WizardStepId | null {
  const index = stepIndex(step);
  return index > 0 ? (WIZARD_STEP_ORDER[index - 1] ?? null) : null;
}

export function SetupWizard(): JSX.Element {
  const [state, setState] = useState<WizardState>(INITIAL_STATE);

  /**
   * Merge a patch into one retained step slice. Edited form values persist so a
   * later return to the step shows the previous values (Requirement 3.6).
   */
  const patchStep = useCallback(
    <K extends "telegram" | "buckets" | "config" | "finalize">(
      key: K,
      patch: Partial<WizardState[K]>,
    ): void => {
      setState((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));
    },
    [],
  );

  /**
   * Advance from the current step to the next one, extending `furthestReached`
   * so forward return (Requirement 3.6) reaches the new step. A no-op on the
   * last step.
   */
  const handleComplete = useCallback((): void => {
    setState((prev) => {
      const next = nextStep(prev.current);
      if (next === null) {
        return prev;
      }
      const furthest =
        stepIndex(next) > stepIndex(prev.furthestReached) ? next : prev.furthestReached;
      return { ...prev, current: next, furthestReached: furthest };
    });
  }, []);

  /**
   * Navigate to `target`. Permitted only when `target` is at or before the
   * furthest step reached (Requirement 3.6): returning to a completed earlier
   * step, or moving forward again up to `furthestReached`. `furthestReached` is
   * never reduced, so the retained slices for later steps are preserved.
   */
  const goTo = useCallback((target: WizardStepId): void => {
    setState((prev) => {
      if (stepIndex(target) > stepIndex(prev.furthestReached)) {
        return prev;
      }
      return { ...prev, current: target };
    });
  }, []);

  /**
   * Return to the immediately preceding step. Computed inside the updater from
   * the latest state so this callback is stable and needs no dependency on
   * `current`. Any earlier step has already been reached, so the move is always
   * allowed (Requirement 3.6).
   */
  const handleBack = useCallback((): void => {
    setState((prev) => {
      const prevId = previousStep(prev.current);
      return prevId === null ? prev : { ...prev, current: prevId };
    });
  }, []);

  // The "Back" action is available only when a previous step exists; returning
  // to it is always allowed because any earlier step has already been reached.
  const canGoBack = previousStep(state.current) !== null;

  // --- Buckets step adapter (real step, task 8.1) ---
  // Seed the real BucketsStep from the retained slice so a return shows the
  // previously entered rows (Req 3.6). Its `BucketRow` uses a numeric channel
  // id, so empty/unparseable retained values map to 0.
  const bucketsSeed = useMemo<readonly BucketRow[]>(
    () =>
      state.buckets.assignments.map((assignment) => ({
        bucketName: assignment.bucketName,
        channelId: Number.parseInt(assignment.channelId, 10) || 0,
      })),
    [state.buckets.assignments],
  );

  // When the real BucketsStep reports completion it hands back the committed
  // rows; persist them into the retained slice, then advance to Config.
  const handleBucketsComplete = useCallback(
    (assignments: readonly BucketRow[]): void => {
      patchStep("buckets", {
        assignments: assignments.map((row) => ({
          bucketName: row.bucketName,
          channelId: String(row.channelId),
          onExisting: null,
        })),
      });
      handleComplete();
    },
    [patchStep, handleComplete],
  );

  const stepElement = useMemo(() => {
    switch (state.current) {
      case "telegram":
        return (
          <TelegramStep
            state={state.telegram}
            onPatch={(patch) => patchStep("telegram", patch)}
            onComplete={handleComplete}
            onBack={canGoBack ? handleBack : null}
          />
        );
      case "buckets":
        // The real BucketsStep (task 8.1) defines its own props and does not
        // consume the shared StepComponentProps contract, so the shell adapts
        // between its `BucketRow` form shape and the retained `buckets` slice:
        // seed it from the previously entered assignments (Req 3.6), and on
        // completion persist the committed rows back before advancing.
        return <BucketsStep initialAssignments={bucketsSeed} onComplete={handleBucketsComplete} />;
      case "config":
        return (
          <ConfigStep
            state={state.config}
            onPatch={(patch) => patchStep("config", patch)}
            onComplete={handleComplete}
            onBack={canGoBack ? handleBack : null}
          />
        );
      case "finalize":
        return (
          <FinalizeStep
            state={state.finalize}
            onPatch={(patch) => patchStep("finalize", patch)}
            onComplete={handleComplete}
            onBack={canGoBack ? handleBack : null}
          />
        );
    }
  }, [state, patchStep, handleComplete, handleBack, canGoBack, bucketsSeed, handleBucketsComplete]);

  return (
    <main className="setup-wizard">
      <section className="setup-card setup-card--wizard">
        <header className="setup-wizard-header">
          <h1 className="setup-wizard-title">Set up tgobjects3</h1>
          <p className="setup-wizard-subtitle">
            Complete these four steps to configure the gateway from first launch.
          </p>
        </header>

        <StepIndicator
          current={state.current}
          furthestReached={state.furthestReached}
          onNavigate={goTo}
        />

        <section
          className="setup-step-panel"
          aria-label={`${WIZARD_STEP_LABELS[state.current]} step`}
        >
          {stepElement}
        </section>
      </section>
    </main>
  );
}
