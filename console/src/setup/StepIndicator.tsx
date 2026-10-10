/**
 * Wizard step indicator (admin-dashboard Requirement 3.3; design: "A
 * `StepIndicator` renders current/completed state").
 *
 * While any step is displayed the wizard must show which of the four steps is
 * current and which are completed. This component renders the ordered steps as
 * an `<ol>`; a step is "completed" when it sits before the furthest step the
 * operator has reached, "current" when it is the displayed step, and otherwise
 * "upcoming". The current step is marked with `aria-current="step"` so assistive
 * technology announces position without relying on colour alone (Requirement
 * 10.4).
 */
import { WIZARD_STEP_LABELS, WIZARD_STEP_ORDER, stepIndex, type WizardStepId } from "./types";

/** Classification of a step relative to the current/furthest position. */
type StepStatus = "completed" | "current" | "upcoming";

function statusOf(step: WizardStepId, current: WizardStepId, furthest: WizardStepId): StepStatus {
  if (step === current) {
    return "current";
  }
  // A step is completed once it lies strictly before the furthest step reached.
  return stepIndex(step) < stepIndex(furthest) ? "completed" : "upcoming";
}

export interface StepIndicatorProps {
  readonly current: WizardStepId;
  readonly furthestReached: WizardStepId;
  /**
   * Navigate to a step. The indicator only invokes this for steps at or before
   * {@link furthestReached}; those entries render as buttons so the operator can
   * return to a completed earlier step or move forward again up to the furthest
   * step reached (Requirement 3.6). Upcoming steps render as inert text.
   */
  readonly onNavigate: (step: WizardStepId) => void;
}

/** Ordered current/completed indicator for the four wizard steps (Req 3.3, 3.6). */
export function StepIndicator(props: StepIndicatorProps): JSX.Element {
  const furthestIndex = stepIndex(props.furthestReached);
  return (
    <nav className="setup-steps" aria-label="Setup progress">
      <ol className="setup-steps-list">
        {WIZARD_STEP_ORDER.map((step, index) => {
          const status = statusOf(step, props.current, props.furthestReached);
          // A step is reachable when it has been reached before (Req 3.6);
          // upcoming steps past the furthest reached are not navigable.
          const reachable = index <= furthestIndex;
          const statusWord =
            status === "completed" ? " (completed)" : status === "current" ? " (current)" : "";
          const marker = (
            <span className="setup-step-marker" aria-hidden="true">
              {status === "completed" ? "✓" : index + 1}
            </span>
          );
          const label = <span className="setup-step-label">{WIZARD_STEP_LABELS[step]}</span>;
          const statusText = <span className="setup-step-status">{statusWord}</span>;
          return (
            <li
              key={step}
              className={`setup-step setup-step--${status}`}
              aria-current={status === "current" ? "step" : undefined}
            >
              {reachable && status !== "current" ? (
                <button
                  type="button"
                  className="setup-step-nav"
                  onClick={() => props.onNavigate(step)}
                >
                  {marker}
                  {label}
                  {statusText}
                </button>
              ) : (
                <>
                  {marker}
                  {label}
                  {statusText}
                </>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
