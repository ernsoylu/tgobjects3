/**
 * Shared setup-wizard step/state model (admin-dashboard Requirements 3.2, 3.3,
 * 3.6; design: "SetupWizard — console/src/setup/ (new)").
 *
 * This module is the single source of truth for the wizard's step identity and
 * per-step state slices. {@link SetupWizard} owns an immutable {@link WizardState}
 * and threads each slice into the matching step component; the sibling step
 * tasks (7.2 TelegramStep, 8.1 BucketsStep, 8.2 ConfigStep, 8.4 FinalizeStep)
 * import these types rather than redefining them, so the shape stays consistent
 * as the real steps land.
 *
 * The design states each step is driven by a React Query mutation against its
 * matching `/admin/setup/*` endpoint, so the per-step slices here model only the
 * *retained form/phase state* the wizard must keep across navigation — not the
 * mutation lifecycle, which the step owns. Keeping the previously entered values
 * in {@link WizardState} is what lets the operator return to a completed earlier
 * step and see what they typed, then move forward again up to
 * {@link WizardState.furthestReached} (Requirement 3.6).
 */

/**
 * The four ordered wizard steps (design: `WizardStepId`). The order is fixed and
 * drives both the {@link StepIndicator} and all forward/back navigation
 * (Requirement 3.2).
 */
export type WizardStepId = "telegram" | "buckets" | "config" | "finalize";

/**
 * The four steps in presentation order (Requirement 3.2). Shared so the wizard
 * and the indicator agree on ordering and so step index math has one source.
 */
export const WIZARD_STEP_ORDER: readonly WizardStepId[] = [
  "telegram",
  "buckets",
  "config",
  "finalize",
] as const;

/** Human-readable label for each step, used by the indicator and headings. */
export const WIZARD_STEP_LABELS: Readonly<Record<WizardStepId, string>> = {
  telegram: "Telegram login",
  buckets: "Buckets",
  config: "Configuration",
  finalize: "Finalize",
};

/** Zero-based position of a step in {@link WIZARD_STEP_ORDER}. */
export function stepIndex(step: WizardStepId): number {
  return WIZARD_STEP_ORDER.indexOf(step);
}

/**
 * Phase of the interactive Telegram login (design: "phase: creds | code | 2fa |
 * done"; Requirement 2.x / 3.x). The TelegramStep (task 7.2) advances this as it
 * drives the needs_code / two-factor loop; the wizard retains the entered
 * credential fields so a return to this step restores them (Requirement 3.6).
 */
export type TelegramPhase = "creds" | "code" | "2fa" | "done";

/**
 * Retained Telegram-step state. Holds the operator-entered credential fields so
 * the step can be revisited with its previous values. `api_hash` and `code` are
 * sensitive and kept only in this in-memory form state across a retry
 * (design/API table: never persisted client-side); the real step (task 7.2)
 * decides exactly when they are cleared.
 */
export interface TelegramStepState {
  readonly phase: TelegramPhase;
  readonly apiId: string;
  readonly apiHash: string;
  readonly login: string;
  readonly code: string;
  readonly attemptsRemaining: number;
}

/**
 * A single bucket→channel assignment as entered in the Buckets step. Mirrors the
 * design's `BucketAssignmentBody`, with `onExisting` carrying the adopt /
 * reinitialise choice when a channel already holds a manifest (Requirement 3.7).
 */
export interface BucketAssignmentState {
  readonly bucketName: string;
  readonly channelId: string;
  readonly onExisting: "adopt" | "reinitialise" | null;
}

/**
 * Retained Buckets-step state: the list of assignments the operator entered. The
 * adopt/reinitialise confirm gate (Requirement 3.7) is owned by the step (task
 * 8.1); this slice only preserves the chosen assignments across navigation.
 */
export interface BucketsStepState {
  readonly assignments: readonly BucketAssignmentState[];
}

/**
 * Retained Config-step state (design: `ConfigStepBody`). Sizes and the master
 * key are kept as strings because this is the raw form input the step validates
 * against the stated ranges before advancing (Requirements 3.4/3.5). The master
 * key is sensitive and lives only in this in-memory form state.
 */
export interface ConfigStepState {
  readonly stagingPath: string;
  readonly stagingMaxSize: string;
  readonly cachePath: string;
  readonly cacheMaxSize: string;
  readonly masterKey: string;
}

/** Lifecycle of the finalize step (design: "idle | submitting | result | error"). */
export type FinalizePhase = "idle" | "submitting" | "result" | "error";

/**
 * Retained Finalize-step state. Only the coarse phase is retained here; the
 * one-time access-key secret and Recovery Kit are owned by the step (task 8.4)
 * and deliberately not stored in the wizard state so navigating away drops the
 * secret (Requirements 3.10, 11.3).
 */
export interface FinalizeStepState {
  readonly phase: FinalizePhase;
}

/**
 * The whole wizard's immutable state (design: `WizardState`).
 *
 * `current` is the displayed step; `furthestReached` is the furthest step the
 * operator has advanced to, which bounds forward navigation when returning to an
 * earlier step (Requirement 3.6). The four per-step slices retain previously
 * entered values so a revisited step shows what the operator typed.
 */
export interface WizardState {
  readonly current: WizardStepId;
  readonly furthestReached: WizardStepId;
  readonly telegram: TelegramStepState;
  readonly buckets: BucketsStepState;
  readonly config: ConfigStepState;
  readonly finalize: FinalizeStepState;
}

/**
 * Props shared by every step component. The wizard passes the step its retained
 * slice plus the navigation callbacks it may invoke. `onComplete` advances to
 * the next step (and extends `furthestReached`); `onPatch` persists edited form
 * values back into the retained slice so they survive navigation. The real step
 * components (sibling tasks) refine the slice type via the generic.
 */
export interface StepComponentProps<TState> {
  readonly state: TState;
  readonly onPatch: (patch: Partial<TState>) => void;
  readonly onComplete: () => void;
  readonly onBack: (() => void) | null;
}
