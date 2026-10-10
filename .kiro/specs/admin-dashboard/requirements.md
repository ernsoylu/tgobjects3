# Requirements Document

## Introduction

The **admin-dashboard** feature delivers a working, deployable operator dashboard for the tgobjects3 gateway: a single web application through which an operator can set up the system from scratch (interactive Telegram login, bucket→channel assignment, storage configuration, finalisation) and manage all operator-editable settings afterwards (runtime tunables, S3 access keys), while monitoring system health.

tgobjects3 already ships most of the backend surface this feature depends on — an Admin API (`gateway/app/admin/router.py`) with buckets/objects/jobs/settings/keys endpoints, a first-run setup wizard API (`gateway/app/admin/setup.py`), observability endpoints (`/health/live`, `/health/ready`, `/metrics`), and a React + Vite Console SPA (`console/`) with five screens. This feature does **not** re-specify those; it references them (notably the existing tgobjects3 Requirement 2 "Guided Setup Wizard", Requirement 17 "S3 Authentication and Access Control", Requirement 18 "Observability", and Requirement 19 "Console UI") and closes the concrete gaps that currently prevent the dashboard from working end-to-end:

- The Console's authentication model does not match the backend's, and there is no login screen, so the dashboard cannot authenticate at all.
- The setup wizard's Telegram login step is non-functional because no interactive Telegram login adapter is wired in, so an operator cannot complete first-run setup.
- The Console exposes no setup-wizard UI, so the "set up the system" flow is unreachable from the dashboard.
- The Console is not deployable: the compose file references a console image build that does not exist.
- Settings management is incomplete: runtime tunables are editable but access-key management and a clear read-only/environment configuration view are not surfaced as a coherent "change all settings" experience.
- The dashboard is to be captured into a Figma design file as a visual design deliverable, underpinned by a documented design system.

This document defines capabilities and outcomes. Mechanisms (whether authentication aligns the Console to the backend's existing scheme or introduces a session login, exact component structure, exact Figma capture steps) are deferred to design, except where a gap is itself the point of the feature and must be stated concretely.

## Glossary

- **Dashboard**: The operator-facing web application delivered by this feature — the tgobjects3 Console SPA extended with authentication, a setup wizard, and settings management, served as a deployable artifact. Referred to elsewhere in tgobjects3 as the "Console".
- **Operator**: The human administrator of a tgobjects3 deployment who sets up and configures the gateway. The single admin persona; there are no multi-user roles.
- **Admin_API**: The gateway's authenticated administrative HTTP surface served on the admin port (default 8080) under `/admin/*`, including the setup wizard endpoints under `/admin/setup/*`. Defined by tgobjects3 Requirements 17 and 19.
- **Setup_Wizard**: The guided first-run flow (tgobjects3 Requirement 2) comprising the status, Telegram, buckets, config, and finalize steps, driven by `POST /admin/setup/*`.
- **Telegram_Login**: The interactive Telegram authentication exchange (supply API id/hash and a phone number or bot token, receive a login code, submit the code, satisfy any two-factor prompt) required to establish the gateway's single MTProto session.
- **Recovery_Kit**: The downloadable bundle produced at the end of setup containing the master-key verifier (never the master key), the Telegram session credentials, and the bucket→channel assignments (tgobjects3 Requirement 2.5, 15.7).
- **Access_Key**: An S3 access key pair (access key id + secret). The secret is shown exactly once at creation and never stored or displayed again (tgobjects3 Requirement 17.2).
- **Master_Key**: The root symmetric key (`TGOS3_MASTER_KEY`, minimum 16 characters) that wraps all bucket KEKs. Plaintext master key material is never displayed, logged, persisted outside the encryption boundary, or placed in the Recovery_Kit.
- **Runtime_Setting**: An operator-editable configuration value that takes effect without restarting the gateway: `staging_max_size`, `cache_max_size`, `cache_half_life`, `read_ahead`, `upload_concurrency` (tgobjects3 Requirement 19.5).
- **Environment_Setting**: A configuration value materialised from the environment at startup by `Settings` (`gateway/app/config.py`) that cannot be changed at runtime and requires a redeploy or restart to change (for example `TGOS3_MASTER_KEY`, `DATABASE_URL`, `S3_PORT`, `ADMIN_PORT`, `CHUNK_RECORDS`).
- **CSRF_Token**: A per-session token the Admin_API binds to the authenticated session, required in a request header on every state-mutating Admin_API request (tgobjects3 Requirement 17.7).
- **Setup_Required_State**: The condition in which the gateway reports that first-run setup has not completed — equivalently, that no Access_Key exists (tgobjects3 Requirement 2.1) — and `/health/ready` returns 503 because Telegram is not yet configured (tgobjects3 Requirement 18.4).
- **Figma_Design_File**: A file in the operator's Figma team capturing the implemented Dashboard as a design record (the Figma integration captures a running web page into Figma).
- **Design_System**: The documented set of visual design tokens, components, and interaction patterns (colour, typography, spacing, states, accessibility conventions) that the Dashboard UI is built from.

## Requirements

### Requirement 1: Dashboard Authentication

**User Story:** As an operator, I want to log in to the dashboard with the admin credentials, so that I can reach the administrative screens through a flow that actually authenticates against the gateway.

Closes the auth-mismatch gap: the Console currently assumes a session cookie plus a readable `tgo_csrf` cookie, but the Admin_API uses HTTP Basic auth and returns the CSRF token in a response header and sets no cookie, and there is no login screen. This requirement states the capability; the design decides whether to align the Dashboard to the backend's Basic + header-CSRF model or introduce a session login on the backend.

#### Acceptance Criteria

1. WHEN an unauthenticated Operator opens the Dashboard, THE Dashboard SHALL present a login screen that collects the admin credentials and SHALL NOT present any administrative screen until an authenticated session is established.
2. WHEN the Operator submits credentials that the Admin_API accepts, THE Dashboard SHALL establish an authenticated session and navigate to the administrative screens.
3. WHILE an authentication submission is in progress, THE Dashboard SHALL indicate that the submission is in progress and SHALL prevent a second submission of the same login form until the in-progress submission resolves.
4. IF the Operator submits credentials that the Admin_API rejects, THEN THE Dashboard SHALL remain on the login screen, SHALL display an error message indicating that authentication failed, and SHALL retain the entered admin identifier while clearing the entered secret.
5. WHILE an authenticated session is active, THE Dashboard SHALL attach the credentials required by the Admin_API to each Admin_API request, and SHALL attach the CSRF_Token on every state-mutating Admin_API request (tgobjects3 Requirement 17.7).
6. IF an Admin_API request returns HTTP 401, THEN THE Dashboard SHALL end the current session and redirect the Operator to the login screen.
7. WHEN the Operator chooses to log out, THE Dashboard SHALL discard the authenticated session and return to the login screen.
8. WHEN the Operator is redirected to the login screen after a session ends due to an HTTP 401 response, THE Dashboard SHALL display a message indicating that the session is no longer valid.

---

### Requirement 2: Interactive Telegram Login Through the Dashboard

**User Story:** As an operator, I want to complete Telegram login from the dashboard by entering my API credentials and the login code, so that the gateway's single MTProto session is established without touching the server shell.

Closes the Telegram-login-adapter gap: there is no Telethon-backed `TelegramLoginPort` implementation wired into the setup router, so `POST /admin/setup/telegram` currently returns HTTP 503 "Telegram login is not available on this deployment". This feature makes interactive Telegram login functional end-to-end, extending tgobjects3 Requirement 2.2–2.4.

#### Acceptance Criteria

1. THE Setup_Wizard SHALL accept an operator-supplied Telegram API id, Telegram API hash, and Telegram login (a phone number or a bot token) entered through the Dashboard at runtime, and SHALL NOT require those values to be stored in the repository or hardcoded.
2. WHEN the Operator submits the Telegram API id, API hash, and login, THE Setup_Wizard SHALL initiate Telegram_Login against the live Telegram service.
3. WHILE a Telegram_Login step submission is in progress, THE Dashboard SHALL indicate that the step is in progress and SHALL prevent re-submission of that step until the submission resolves.
4. WHEN Telegram_Login requires a login code or a two-factor code, THE Dashboard SHALL prompt the Operator to enter the code and SHALL submit it to the Setup_Wizard, allowing up to 3 code attempts per Telegram_Login (tgobjects3 Requirement 2.3).
5. IF the Operator exceeds 3 failed code attempts, THEN THE Setup_Wizard SHALL abort the Telegram_Login step, THE Dashboard SHALL display an error indicating that authentication failed, and THE Dashboard SHALL return the Operator to the start of the Telegram credential entry step (tgobjects3 Requirement 2.4).
6. WHEN Telegram_Login completes successfully, THE Setup_Wizard SHALL record the resulting single-session credentials for use by the gateway's one MTProto session and for inclusion in the Recovery_Kit (tgobjects3 Requirement 2.5).
7. IF the Telegram service is unreachable during Telegram_Login, THEN THE Dashboard SHALL display an error indicating Telegram is unavailable, SHALL preserve the entered Telegram API id and API hash, and SHALL allow the Operator to retry the step.

---

### Requirement 3: Setup Wizard User Interface

**User Story:** As an operator, I want a guided setup wizard in the dashboard, so that I can configure a freshly deployed gateway from first launch to a working system without reading internal documentation.

Closes the no-setup-UI gap: the Console has no setup screens. This requirement exposes the existing `POST /admin/setup/*` steps as Dashboard UI (tgobjects3 Requirement 2).

#### Acceptance Criteria

1. WHILE the gateway is in the Setup_Required_State, THE Dashboard SHALL route the Operator into the Setup_Wizard on any Dashboard request (tgobjects3 Requirement 2.1).
2. THE Setup_Wizard SHALL present the following four ordered steps in sequence: Telegram_Login, then bucket→channel assignment, then storage configuration, then finalisation.
3. WHILE any Setup_Wizard step is displayed, THE Setup_Wizard SHALL indicate which of the four steps is current and which steps are completed.
4. WHILE collecting storage configuration, THE Setup_Wizard SHALL collect the staging volume path and maximum size (between 1 MiB and 1 TiB inclusive), the record cache path and maximum size (between 1 MiB and 100 GiB inclusive), and the Master_Key (minimum 16 characters), and SHALL validate each value against the stated range before allowing the step to proceed (tgobjects3 Requirement 2.2).
5. IF the Operator submits a storage configuration value outside its stated range, THEN THE Setup_Wizard SHALL remain on the storage configuration step, SHALL identify the field at fault and its accepted range, and SHALL NOT advance to the next step.
6. WHEN the Operator chooses to return to an already-completed earlier step, THE Setup_Wizard SHALL display that step with its previously entered values and SHALL allow the Operator to return forward to the furthest step previously reached.
7. WHEN the Operator assigns a channel that already contains bucket manifest messages, THE Setup_Wizard SHALL offer the choice to adopt the existing bucket or to reinitialise it, and IF the Operator chooses to reinitialise, THEN THE Setup_Wizard SHALL display a warning that existing bucket data will become inaccessible and SHALL require explicit Operator confirmation before proceeding (tgobjects3 Requirement 2.7).
8. WHILE finalisation is in progress, THE Setup_Wizard SHALL indicate that finalisation is in progress and SHALL prevent the Operator from submitting finalisation a second time.
9. WHEN the Operator finalises setup successfully, THE Setup_Wizard SHALL present the Recovery_Kit as a downloadable bundle (tgobjects3 Requirement 2.5).
10. WHEN the Operator finalises setup successfully, THE Setup_Wizard SHALL display the first Access_Key secret exactly once and SHALL stop displaying the secret once the Operator navigates away from the confirmation screen (tgobjects3 Requirement 2.6, 17.2).
11. IF finalisation fails, THEN THE Setup_Wizard SHALL remain on the finalisation step, SHALL display a readable error describing the failure, and SHALL allow the Operator to retry finalisation.
12. WHEN setup finalisation completes successfully, THE Dashboard SHALL leave the Setup_Wizard and present the administrative screens.

---

### Requirement 4: Runtime Settings Management

**User Story:** As an operator, I want to view and change all runtime-tunable settings from the dashboard, so that I can adjust the running gateway without a restart.

Builds on tgobjects3 Requirement 19.5–19.7 and the existing `GET`/`PUT /admin/settings` endpoints.

#### Acceptance Criteria

1. THE Dashboard SHALL display the current values of all five Runtime_Settings: `staging_max_size`, `cache_max_size`, `cache_half_life`, `read_ahead`, and `upload_concurrency`.
2. THE Dashboard SHALL allow the Operator to edit the Runtime_Settings and SHALL validate each value against the Admin_API's accepted range (`read_ahead` an integer between 0 and 32 inclusive; every other Runtime_Setting an integer of at least 1) before submitting a change.
3. IF the Operator submits a Runtime_Setting value outside its accepted range, THEN THE Dashboard SHALL identify the field at fault and its accepted range and SHALL NOT submit the change to the Admin_API.
4. WHILE the Operator has edited one or more Runtime_Settings values without a successful save, THE Dashboard SHALL indicate that unsaved changes are present and SHALL offer the Operator the choice to submit the changes or to discard them and restore the last-displayed values.
5. WHEN a Runtime_Settings change submission is in progress, THE Dashboard SHALL indicate that the save is in progress and SHALL prevent the Operator from submitting the same change a second time until the submission resolves.
6. WHEN the Operator submits a valid Runtime_Settings change, THE Dashboard SHALL apply it through the Admin_API such that the new values take effect within 5 seconds without restarting the gateway (tgobjects3 Requirement 19.5).
7. IF a submitted change would reduce `staging_max_size` below the amount currently reserved by in-flight uploads, THEN THE Admin_API SHALL reject the change with HTTP 409 and THE Dashboard SHALL display an error indicating the minimum permitted value and SHALL NOT present the change as applied (tgobjects3 Requirement 19.6, 19.7).
8. WHEN a Runtime_Settings change is applied successfully, THE Dashboard SHALL re-display the applied values and SHALL clear the unsaved-changes indication.

---

### Requirement 5: Access Key Management

**User Story:** As an operator, I want to create, list, and delete S3 access keys from the dashboard, so that I can control which clients may use the gateway after setup.

Builds on tgobjects3 Requirement 17 and the existing `GET`/`POST /admin/keys` and `DELETE /admin/keys/{id}` endpoints.

#### Acceptance Criteria

1. THE Dashboard SHALL display the list of existing Access_Keys by access key id and label, and SHALL NOT display any secret for an existing key.
2. WHILE no Access_Key exists, THE Dashboard SHALL display an empty-state indication in the access-key list rather than an empty or broken view.
3. WHEN the Operator creates an Access_Key with an optional label, THE Dashboard SHALL display the new secret exactly once, SHALL offer an action to copy the secret, and SHALL stop displaying it once the Operator dismisses or navigates away from the creation result (tgobjects3 Requirement 17.2).
4. THE Dashboard SHALL require explicit Operator confirmation before deleting an Access_Key, and SHALL delete the key only after that confirmation.
5. WHEN the Operator confirms deletion of an Access_Key, THE Dashboard SHALL remove it through the Admin_API and SHALL update the displayed list to omit the deleted key.
6. IF a deletion request targets an Access_Key the Admin_API reports as absent, THEN THE Dashboard SHALL display a readable error and SHALL refresh the displayed list to reflect the Admin_API's current state.

---

### Requirement 6: Configuration Visibility

**User Story:** As an operator, I want the dashboard to clearly separate settings I can change at runtime from configuration fixed at deployment, so that I understand what is editable and what requires a redeploy.

Addresses the "change all settings" request by enumerating the editable-vs-fixed surface grounded in `gateway/app/config.py` and the existing Admin_API endpoints.

#### Acceptance Criteria

1. THE Dashboard SHALL present the Runtime_Settings as editable and the Access_Keys as manageable within its settings area.
2. THE Dashboard SHALL display the non-secret Environment_Setting values as read-only, each labelled as requiring a redeploy or restart to change.
3. THE Dashboard SHALL NOT display the plaintext Master_Key or any other secret Environment_Setting value, indicating instead whether the value is set without revealing it.
4. WHERE an Environment_Setting is read-only, THE Dashboard SHALL NOT present an editing control for that value.

---

### Requirement 7: System Health and Observability Surfacing

**User Story:** As an operator, I want the dashboard to show whether the gateway is ready and which dependencies are healthy, so that I can tell at a glance whether the system is serving traffic.

Surfaces tgobjects3 Requirement 18 (`/health/live`, `/health/ready`, `/metrics`) in the Dashboard.

#### Acceptance Criteria

1. THE Dashboard SHALL display the gateway's readiness state obtained from `/health/ready` and SHALL refresh it at least once every 10 seconds while the health view is displayed.
2. WHEN `/health/ready` reports HTTP 200, THE Dashboard SHALL display the gateway as ready and SHALL display the PostgreSQL and Telegram dependencies as healthy.
3. WHEN `/health/ready` reports HTTP 503 because the PostgreSQL dependency is unhealthy, THE Dashboard SHALL display the gateway as not ready and the PostgreSQL dependency as unhealthy (tgobjects3 Requirement 18.3).
4. WHEN `/health/ready` reports HTTP 503 because the Telegram dependency is disconnected or not yet configured, THE Dashboard SHALL display the gateway as not ready and the Telegram dependency as unhealthy (tgobjects3 Requirement 18.4).
5. IF a health poll fails to complete or times out, THEN THE Dashboard SHALL display the readiness state as unknown and SHALL indicate that readiness could not be retrieved.
6. THE Dashboard SHALL provide a control that retrieves and presents the gateway's metrics exposed at `/metrics` on the admin port (tgobjects3 Requirement 18.1).
7. WHILE the gateway is in the Setup_Required_State, THE Dashboard SHALL indicate that readiness is pending until setup completes.

---

### Requirement 8: Deployable Dashboard

**User Story:** As an operator, I want the dashboard to come up with a single `docker compose up`, so that I can run it alongside the gateway without extra build steps.

Closes the not-deployable gap: `deploy/docker-compose.yml` references `deploy/Dockerfile.console`, which does not exist.

#### Acceptance Criteria

1. WHEN `docker compose up` is run from the deploy directory, THE deployment SHALL build the Dashboard as a static web bundle and bring it up on the console port (default 8081) alongside the gateway and PostgreSQL.
2. IF the Dashboard bundle build fails, THEN THE deployment SHALL fail the console image build and SHALL NOT serve a partial or empty bundle.
3. THE served Dashboard SHALL proxy requests to the gateway's `/admin`, `/health`, and `/metrics` paths so that the Dashboard reaches the Admin_API and observability endpoints from the same origin as the Dashboard.
4. THE console service SHALL expose a container healthcheck that reports healthy once the Dashboard is being served.
5. WHEN the Operator opens the console port in a browser after `docker compose up` while the gateway is not in the Setup_Required_State, THE Dashboard SHALL load and present the login screen.
6. WHEN the Operator opens the console port in a browser after `docker compose up` while the gateway is in the Setup_Required_State, THE Dashboard SHALL load and present the Setup_Wizard.

---

### Requirement 9: Figma Design Deliverable and Design System

**User Story:** As an operator, I want the dashboard captured in a Figma design file and built on a documented design system, so that the dashboard's visual design is recorded and consistent.

Addresses the explicit request to design the dashboard in Figma; the Figma integration captures a running web page into a Figma file rather than authoring from a prompt.

#### Acceptance Criteria

1. WHEN the Dashboard implementation is complete, THE delivery SHALL capture the running Dashboard into a Figma_Design_File in the Operator's Figma team as a design record.
2. THE Dashboard SHALL be built on a documented Design_System that defines, for each of the following categories, at least one named token or specification: colour, typography, spacing, component states, and interaction patterns.
3. THE Design_System SHALL define, for every interactive control type used in the Dashboard, its default, hover, focus, active, and disabled states.
4. THE Figma_Design_File SHALL contain a frame for each of the Dashboard's primary screens: the login screen, each ordered step of the Setup_Wizard, the runtime settings area, the access-key management area, the configuration-visibility area, and the system-health area.

---

### Requirement 10: Accessibility

**User Story:** As an operator who relies on assistive technology, I want the dashboard to be accessibility compliant, so that I can operate it with a keyboard and a screen reader.

The existing screens already use Radix primitives and ARIA; this requirement makes accessibility a stated capability for the new screens too.

#### Acceptance Criteria

1. THE Dashboard SHALL conform to WCAG 2.1 Level AA across the login screen, the Setup_Wizard steps, and the settings area.
2. THE Dashboard SHALL make every interactive control reachable and operable using the keyboard alone, with no control requiring a pointing device to activate.
3. THE Dashboard SHALL associate a programmatic label with every form control such that the label is exposed to assistive technology.
4. THE Dashboard SHALL render text and interactive-control labels at a contrast ratio of at least 4.5:1 against their background (at least 3:1 for text at 18pt or 14pt bold and larger).
5. WHEN a modal, dialog, or Setup_Wizard step opens, THE Dashboard SHALL move keyboard focus to the first interactive control within it, and WHEN that modal, dialog, or step closes, THE Dashboard SHALL return keyboard focus to the control that triggered its opening.
6. WHEN the Dashboard reports an error or a status change, THE Dashboard SHALL expose the message text to assistive technology without requiring the Operator to move focus to it.

---

### Requirement 11: Security of Displayed Secrets

**User Story:** As an operator, I want the dashboard to never leak key material, so that compromising a dashboard session does not expose durable secrets.

#### Acceptance Criteria

1. THE Dashboard SHALL NOT display, log, or transmit the plaintext Master_Key or any unwrapped key or plaintext payload.
2. THE Recovery_Kit presented by the Dashboard SHALL carry the master-key verifier and SHALL NOT carry the Master_Key itself (tgobjects3 Requirement 2.5, 15.7).
3. THE Dashboard SHALL display an Access_Key secret exactly once, at creation, and SHALL NOT make it retrievable afterward (tgobjects3 Requirement 17.2).
4. THE Dashboard SHALL require an authenticated session for every administrative screen and Admin_API request (tgobjects3 Requirement 17.5).
5. WHEN the Operator submits a state-mutating request, THE Dashboard SHALL include the CSRF_Token the Admin_API requires in that request (tgobjects3 Requirement 17.7).
6. IF a state-mutating Admin_API request carries a missing or mismatched CSRF_Token, THEN THE Admin_API SHALL reject the request with HTTP 403 and SHALL NOT apply the requested change (tgobjects3 Requirement 17.7).

---

### Requirement 12: Error Handling and Recovery Routing

**User Story:** As an operator, I want the dashboard to explain failures in readable terms and route me to the right place, so that I always know what to do next.

#### Acceptance Criteria

1. IF an Admin_API request returns HTTP 401, THEN THE Dashboard SHALL redirect the Operator to the login screen (Requirement 1.6).
2. IF an Admin_API request returns HTTP 409, 422, or 503, THEN THE Dashboard SHALL display a readable message, derived from the response, that describes the failure and SHALL remain on the current screen.
3. IF an Admin_API request fails to complete because of a network error or does not receive a response within 30 seconds, THEN THE Dashboard SHALL display an error message indicating the gateway could not be reached and SHALL offer the Operator a way to retry the request.
4. IF an Admin_API request returns any HTTP 5xx status other than 503, THEN THE Dashboard SHALL display a readable message indicating an unexpected gateway error and SHALL NOT present the request as succeeded.
5. IF the gateway reports the Setup_Required_State, THEN THE Dashboard SHALL redirect the Operator into the Setup_Wizard (Requirement 3.1).
6. WHEN the Dashboard displays a validation failure for a submitted value, THE Dashboard SHALL identify the specific field at fault and SHALL state the accepted range or constraint for that field.
