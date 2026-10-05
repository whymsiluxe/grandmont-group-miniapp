# Session handoff — autonomous execution 2026-09-08 (EXECUTION_PLAN.md)

## 2026-09-25 Stages/Roadmap router extraction (`codex/split-stages`)

Current task: split Stages/Roadmap planning routes out of `backend/main.py`
without changing runtime behavior. This branch is stacked on
`codex/split-objects` at `5c390dc9932a7ef9843b5c4019475dfb1f430659`.

Implemented so far:
- Added `backend/routes/stages.py`.
- Moved stage CRUD/status/swap/complete/blocker handlers, roadmap
  category/item/status/note handlers, and stage request approval handlers into
  the new route module.
- Left `/api/objects/{object_id}/blocker-photo...` in `main.py`.
- `main.py` keeps the canonical roadmap singleton, JSON transaction helpers,
  shared loaders, and critical JSON store registration; route module receives
  everything via `StagesRouteDeps`.
- `main.py` registers the router through canonical `app.include_router(...)`
  and re-exports legacy handler/model names for direct-call tests.

Verification so far:
- `python3 -m py_compile backend/main.py backend/routes/stages.py backend/routes/objects.py`.
- Route manifest smoke: `186` HTTP routes, duplicate routes `[]`, all 21
  stage/roadmap routes owned by `routes.stages`, flattened through
  `iter_app_routes()`.
- Added `tests/test_stages_routes_extraction.py`.
- Targeted extraction/stage/roadmap/object-access/history suite: `120 passed`.
- Full suite: `1148 passed, 1 skipped`.

Next:
- Push branch `codex/split-stages` if a remote handoff is needed. Do not
  merge or deploy. Rebase only after Claude's auth/roles/profiles split is
  complete and `main` is updated.

## 2026-09-25 Objects router extraction (`codex/split-objects`)

Current task: split Object API routes out of `backend/main.py` without changing
runtime behavior.

Implemented so far:
- Added `backend/routes/objects.py` and `backend/routes/__init__.py`.
- Moved these handlers into the new route module: `list_objects`,
  `my_assignments`, object history, assignment assign/update/delete/respond,
  assignment candidates, batch assign, object description/info-items,
  create object, and object status.
- Left image/photo, documents, tasks, stages/roadmap/blockers, and daily-plan
  routes in `main.py` for later domain-specific splits.
- `main.py` wires `ObjectsRouteDeps`, registers the extracted routes as flat
  `APIRoute` records, and re-exports legacy handler/model names.
- Added `tests/test_objects_routes_extraction.py`.

Verification so far:
- `python3 -m py_compile backend/main.py backend/routes/objects.py`.
- Focused object/assignment/access/history/storage suite: `140 passed`.

Next:
- Run the new extraction test plus targeted manifest checks.
- Run full `pytest`.
- Commit in at least two steps before any push. Do not deploy.

## 2026-09-17 Document gallery P2 slice handoff

Current autonomous slice: Section H continuation, fourth P2 item
(`Document gallery/preview grid with object filters and quick actions`).

Implemented in this slice:
- `frontend/app.html`: Documents view now includes an object-document gallery
  above the existing Angebot/Rechnung PDF builder.
- `frontend/app.html`: the Documents page now uses `ios-page`/`ios-section`
  structure, a stable page title, object filter rail, gallery grid, and builder
  section title.
- `frontend/js/document-gallery.js`: new module fetches `/api/objects`, loads
  `/api/objects/{object_id}/documents` for each accessible object, and renders
  filtered cards with authenticated image thumbnails or PDF tiles.
- `frontend/js/document-gallery.js`: quick actions reuse the existing
  authenticated document viewer, open Object Detail, and expose owner-only delete.
- `tests/test_document_gallery_frontend_contract.py`: new frontend contract locks
  markup, script/init wiring, endpoint reuse, filters, quick actions, and styles.

Verification so far:
- `node --check frontend/js/document-gallery.js`.
- `node --check frontend/js/objects.js`.
- Focused document gallery/object UI checks: `17 passed`.
- Full suite: `842 passed, 1 skipped`.

Next recommended slice:
- Finish release verification/deploy for this P2 item, then continue to P2 item
  5: extended calendar week/month/year and day bottom sheet polish.

## 2026-09-17 Object task Kanban P2 slice handoff

Current autonomous slice: Section H continuation, third P2 item
(`Object task Kanban: To Do -> In Progress -> Done`).

Implemented in this slice:
- `frontend/js/objects.js`: Object Info now appends a Kanban board after the
  budget dashboard and before object history.
- `frontend/js/objects.js`: the board reuses object-scoped `/api/tasks`, groups
  `открыто`, `в работе`, and `закрыто` into `Нужно`, `В работе`, and `Готово`,
  and keeps task chat access on every card.
- `frontend/js/objects.js`: owner users can advance cards with inline buttons or
  drag/drop between lanes; workers see the board read-only.
- `frontend/js/tasks.js`: global task status changes now refresh the Object Info
  Kanban when it is open.
- `frontend/app.html`: added compact responsive Kanban, lane, card, drop-active,
  and action styling.
- `tests/test_object_task_kanban_frontend_contract.py`: new frontend contract
  locks render order, status mapping, owner actions, drag/drop, chat integration,
  sync hook, and styling hooks.

Verification so far:
- `node --check frontend/js/objects.js`.
- `node --check frontend/js/tasks.js`.
- Focused object task Kanban/budget/history/object/needs checks: `36 passed`.
- Full suite: `827 passed, 1 skipped`.

Next recommended slice:
- Finish release verification/deploy for this P2 item, then continue to P2 item
  4: document gallery/preview grid with object filters and quick actions.

## 2026-09-17 Object budget dashboard P2 slice handoff

Current autonomous slice: Section H continuation, second P2 item
(`Object budget dashboard with budget/spent/remaining/risk`).

Implemented in this slice:
- `frontend/js/objects.js`: Object Info now renders an owner-only budget
  dashboard after the object control center and before object history.
- `frontend/js/objects.js`: budget parsing handles existing budget/spent fields
  plus the known percent aliases (`потрачено в % от бюджета`, `% бюджета`,
  `Потрачено %`), computes remaining/overrun, and maps risk to normal/yellow/red
  states.
- `frontend/app.html`: added compact iOS-style budget dashboard, meter, risk
  pill, stat, and overrun-note styling.
- `tests/test_object_budget_frontend_contract.py`: new frontend contract locks
  render order, budget aliases, owner-only guard, risk states, retry state, and
  styling hooks.

Verification so far:
- `node --check frontend/js/objects.js`.
- Focused object budget/history/object checks: `10 passed`.
- Full suite: `822 passed, 1 skipped`.

Next recommended slice:
- Finish release verification/deploy for this P2 item, then continue to P2 item
  3: object task Kanban.

## 2026-09-17 Team drag-to-assign P2 slice handoff

Current autonomous slice: Section H continuation, first P2 item
(`Drag/drop worker to object, opening assignment sheet with from/to/task/work type`).

Implemented in this slice:
- Re-verified P1 backlog overlap as already shipped by the Stage 2/3 work:
  offline check-in/finish outbox, shared API timeout/retry/abort, DailyPlan
  validation/acknowledgment, owner daily cockpit, and object history.
- `frontend/js/team-drag-assign.js`: new Team-view enhancer adds drag handles to
  unassigned workers, marks object blocks as drop zones, and opens the existing
  Assignment Sheet with both worker and object preselected.
- `frontend/js/assignment-sheet.js`: added known worker+object mode so drops go
  straight from work type and period to task note/confirmation, and the existing
  `openAssignFromProfile()` `initialDate` argument is now honored.
- `frontend/app.html`: loads the new helper after `assignment-sheet.js` and adds
  drag handle, drop-zone, ghost, and reduced-motion styles.
- `tests/test_team_drag_assignment_frontend_contract.py`: new frontend contract
  locks the P2 drag-to-assign wiring.

Verification so far:
- `node --check frontend/js/assignment-sheet.js`.
- `node --check frontend/js/team-drag-assign.js`.
- Focused Team/assignment checks: `87 passed`.

Next recommended slice:
- Finish release verification/deploy for this P2 item, then continue to P2 item
  2: object budget dashboard with budget/spent/remaining/risk.

## 2026-09-15 Unified autonomous plan completion handoff

Current status: `docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md` is marked
`AUTONOMOUS_STATUS: DONE`.

Completed scope:
- Stage 1 premium UI polish is covered by the shared iOS primitives and the
  screen-by-screen Home, Feed/comments, Chat, Profile, Tools, Calendar,
  Team/Control, Needs, Alerts, and Splash passes.
- Stage 2 worker start/finish, geo, photos, voice affordance, chat location,
  shared API timeout/retry/abort, offline outbox, and DailyPlan validation/ack
  requirements are shipped with focused contracts.
- Stage 3 manager daily cockpit, assignment escalation, Russian command parser,
  central AI command entry, quick broadcast controls, and object history are
  shipped with backend/frontend coverage.
- Overlapping P1 items from the backlog are handled by the shipped Stage 2/3
  slices.

Verification baseline:
- Latest full suite before this docs-only completion commit: `811 passed,
  1 skipped`.
- Production was healthy at deployed version `7f0827e` before the final DONE
  status commit; the final deploy step should publish the DONE SHA and re-check
  `/api/health`.

Tiny remaining note:
- Deeper Home label micro-polish is deferred as cosmetic legacy-surface debt.
  The owner cockpit data, warning signals, and clean-state behavior required by
  the plan are already implemented and tested.

## 2026-09-15 Object history frontend section handoff

Current autonomous slice: Stage 3 object-history frontend contract from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/objects.js`: after the existing Object Info tab render completes,
  appends a human-readable `История` section without editing root-owned
  `frontend/js/object-info.js`.
- `frontend/js/objects.js`: loads
  `GET /api/objects/{object_id}/history?limit=12`, renders event title,
  subtitle, actor, kind, and time metadata, and includes empty/retry states.
- `frontend/app.html`: added compact timeline row styling for the object history
  section.
- `tests/test_object_history_frontend_contract.py`: new contract locks the Info
  append, endpoint usage, status/team/plan/document/defect/finish/broadcast
  event labels, retry state, and styling hooks.

Verification:
- `node --check frontend/js/objects.js`.
- `git diff --check`.
- Focused object-history frontend/backend checks: `8 passed`.
- Full suite: `811 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Reassess `AUTONOMOUS_STATUS` and remaining DONE criteria. The main Stage 3
  items now have backend/frontend contracts; root-owned `frontend/js/home.js`
  still limits deeper owner cockpit label polish only.

## 2026-09-15 Central AI management command entry handoff

Current autonomous slice: Stage 3 central voice/action entry point from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: added a compact owner AI `Команда` button beside the
  model selector plus bottom-sheet styling for the management command composer.
- `frontend/js/ai.js`: added the command sheet, typed command parsing,
  shared `attachVoiceInputButton()` dictation support via `/api/transcribe`,
  `/api/manager/command/parse` integration, and safe draft rendering for
  worker/date/object/task/comment fields.
- `frontend/js/ai.js`: the entry only renders a confirmation-required draft and
  does not create assignments or tasks automatically.
- `tests/test_ai_manager_command_frontend_contract.py`: new frontend contract
  locks the compact entry point, voice hook, parser API call, and draft-only UI.

Verification:
- `node --check frontend/js/ai.js`.
- `git diff --check`.
- Focused AI/parser/frontend checks: `19 passed`.
- Full suite: `810 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Reassess final DONE criteria. Object-history frontend display and deeper owner
  cockpit polish remain tied to root-owned `frontend/js/object-info.js` and
  `frontend/js/home.js` in this checkout, so either document those as tiny
  ownership blockers or correct ownership before doing that UI pass.

## 2026-09-15 Manager quick broadcast controls handoff

Current autonomous slice: Stage 3 quick broadcast controls from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `backend/main.py`: added owner-only `POST /api/manager/broadcast` for sending
  announcements either to the whole company group chat or to an object chat.
- `backend/main.py`: company broadcasts append regular group-chat messages with
  broadcast metadata; object broadcasts validate the object id, append to
  `obj:<object_id>`, return the audience count, and append a
  `broadcast_sent` object-history event.
- `frontend/app.html`: added an owner-only Chat header announcement icon, sheet
  styling, and broadcast bubble label styling.
- `frontend/js/chat.js`: added the broadcast bottom sheet with company/object
  target selection, object picker, validation, `/api/manager/broadcast` send
  flow, toast/haptic feedback, and chat-list refresh.
- `tests/test_manager_broadcast.py`: new coverage for company/object broadcast
  persistence, validation, history integration, and owner-only route dependency.
- `tests/test_chat_frontend_contract.py`: locked the owner broadcast button,
  sheet contract, API call, and broadcast bubble label.
- `tests/test_assignment_lifecycle.py`: production route count updated to 181.

Verification:
- `python3 -m py_compile backend/main.py`.
- `node --check frontend/js/chat.js`.
- `git diff --check`.
- Focused broadcast/chat/frontend checks: `56 passed`.
- Full suite: `809 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Assess remaining Stage 3 DONE criteria. `frontend/js/home.js` and
  `frontend/js/object-info.js` are still root-owned in this checkout, so owner
  cockpit/central action entry-point polish in those files remains blocked until
  ownership is corrected or an owner-capable process edits them.

## 2026-09-15 Object history backend timeline handoff

Current autonomous slice: Stage 3/P1 human-readable object history from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `backend/core/paths.py`: added `OBJECT_HISTORY_FILE` at
  `object_history.json` under the active data root.
- `backend/main.py`: added object history helpers and
  `GET /api/objects/{object_id}/history`, protected by the existing object
  access dependency and returning newest entries first with a bounded `limit`.
- `backend/main.py`: appends readable object events for object status changes,
  worker assignments, stage status changes, worker stage completion, document
  upload, defect creation, and finish submission.
- `backend/main.py`: history appends are best-effort around primary mutations;
  failures are logged as warnings and do not block the operational action.
- `tests/test_object_history.py`: new coverage for filtering/sorting/limits,
  route registration, readable assignment/status/stage/defect events, and event
  metadata.
- `tests/test_assignment_lifecycle.py`: production route count updated to 180.

Verification:
- `python3 -m py_compile backend/main.py backend/core/paths.py`.
- `git diff --check`.
- Focused object-history/assignment/stage/defect checks: `67 passed`.
- Full suite: `803 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 3 with quick broadcast controls. The object-history backend is
  ready for a frontend timeline pass once root-owned frontend files are writable.

## 2026-09-14 Assignment confirmation escalation handoff

Current autonomous slice: Stage 3 task confirmation escalation from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `backend/main.py`: added `pending_since` for newly created pending assignments
  and for accepted assignments reset to pending after a meaningful owner edit.
- `backend/main.py`: added pending-assignment escalation helpers with the product
  thresholds: yellow warning after 2h, red unconfirmed alert after 4h.
- `backend/main.py`: `/api/dashboard/shifts-today` now returns `assignment_id`,
  `pending_since`, `response_wait_seconds`, `response_escalation`, and
  `response_alert_type` on `awaiting_response` rows.
- `backend/main.py`: `/api/alerts` now emits owner assignment-confirmation alerts
  with worker/object context and stable yellow/red IDs.
- `tests/test_assignment_confirmation_escalation.py`: new coverage for threshold
  calculation, owner alert generation, role-aware alerts integration, and the
  dashboard payload contract.

Verification:
- `python3 -m py_compile backend/main.py`.
- `git diff --check`.
- Focused assignment/needs alert checks: `39 passed`.
- Full suite: `798 passed, 1 skipped` with only existing deprecation warnings.

Known follow-up:
- `frontend/js/home.js` is currently root-owned (`root:root`, `0644`) in this
  repo checkout, so the owner cockpit renderer polish for distinct 2h/4h labels
  could not be patched by the `promonta` user in this slice. The backend API and
  alerts are ready for that UI pass once file ownership is corrected or the edit
  is run by an owner-capable process.

Next recommended slice:
- Continue Stage 3 with broadcast controls or human-readable object history,
  unless the frontend ownership issue is fixed first and the cockpit renderer can
  be updated to use `response_escalation` directly.

## 2026-09-14 Manager command parser draft handoff

Current autonomous slice: Stage 3 Russian management command parser from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `backend/main.py`: added `parse_management_command()` and supporting lookup
  helpers for Russian owner commands that assign work to a worker on a date for
  an object with an optional comment.
- `backend/main.py`: added owner-only `/api/manager/command/parse`, returning a
  safe draft with `requires_confirmation: true` instead of creating tasks or
  assignments directly.
- `tests/test_management_command_parser.py`: new unit/route contract covers the
  target command, relative dates, unresolved names, worker/object resolution,
  route registration, and worker access denial.
- `tests/test_assignment_lifecycle.py`: updated the production route-count
  assertion for the new parse endpoint.

Verification:
- `python3 -m py_compile backend/main.py`.
- `git diff --check`.
- Focused parser/route-count checks: `5 passed`.
- Full suite: `794 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 3 with task confirmation escalation: worker accept/confirm
  state, 2h yellow warning, 4h red/unconfirmed alert, and owner cockpit surfacing.

## 2026-09-14 Check-in offline evidence outbox handoff

Current autonomous slice: Stage 2 durable offline finish/check-in outbox from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/shared.js`: added `promonta-offline-outbox` IndexedDB helpers for
  durable records with binary `File` objects, indexed by kind/state.
- `frontend/js/checkin.js`: start-shift confirmation queues photos, geo metadata,
  start/DailyPlan fields, and the persistent idempotency key when offline or when
  a transient upload failure occurs; reconnect/startup retries replay the same
  payload and delete only after backend confirmation.
- `frontend/js/finish-wizard.js`: finish submission now builds a replayable record
  containing finish photos, summary/report fields, DailyPlan execution report,
  needs/defects payloads, geo metadata, and the persistent idempotency key; queued
  records retry on reconnect and preserve the existing post-finish task/defect
  best-effort behavior.
- `tests/test_checkin_offline_outbox_frontend_contract.py`: new frontend contract
  locks IndexedDB store setup, start/finish queue payloads, reconnect retry hooks,
  and idempotency-key reuse.
- `docs/DATA_PROTECTION.md`: noted temporary browser IndexedDB outbox storage for
  queued GPS/photo evidence before server sync.

Verification:
- `node --check frontend/js/shared.js`.
- `node --check frontend/js/checkin.js`.
- `node --check frontend/js/finish-wizard.js`.
- `git diff --check`.
- Focused offline/check-in contracts: `17 passed`.
- Adjacent check-in/photo/DailyPlan tests: `79 passed, 1 skipped`.
- Full suite: `790 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 2 by surfacing visible outbox status/counts in worker Home or
  Finish UI, then move to the remaining Stage 3 manager cockpit/voice/broadcast
  work once the owner-visible offline status is in place.

## 2026-09-14 Shared API timeout/retry layer handoff

Current autonomous slice: Stage 2 shared frontend API timeout/retry/abort layer
from `docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/shared.js`: `api()` now composes caller abort signals with a
  default 18s timeout via `AbortController`.
- `frontend/js/shared.js`: transient retries are enabled by default only for
  safe `GET`/`HEAD` requests and only for timeout, network, 408/425/429, and
  5xx-style failures.
- `frontend/js/shared.js`: writes (`POST`, `PATCH`, `DELETE`, etc.) do not retry
  unless a caller explicitly passes `retry: true`, preserving the existing
  no-duplicate-write posture for non-idempotent mutations.
- `tests/test_shared_api_frontend_contract.py`: new static contract locks the
  timeout, abort, retry-status, safe-method, and no-write-retry behavior.
- `docs/PROJECT_STATE.md`: removed the stale open-issue note that `api()` had no
  frontend timeout.

Verification:
- `node --check frontend/js/shared.js`.
- `git diff --check`.
- Focused shared/feed/needs/finish contracts: `22 passed`.
- Full suite: `785 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 2 with durable offline finish/check-in outbox and persisted
  retry states, building on the now-central request timeout and safe retry
  foundation.

## 2026-09-14 Check-in geo evidence metadata handoff

Current autonomous slice: Stage 2 worker start/finish evidence from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/checkin.js`: `_getGeolocation()` now returns `lat`, `lon`,
  `accuracy`, and `timestamp`; start-shift photo upload sends the optional
  `accuracy` and `geo_timestamp` fields with the existing idempotent form
  payload.
- `frontend/js/finish-wizard.js`: finish submission sends the same geo evidence
  metadata gathered during the required geolocation step.
- `backend/main.py`: `/api/checkin/start` and
  `/api/checkin/{session_id}/finish` accept optional geo metadata and persist it
  as start/finish accuracy and timestamp fields in `checkin_meta.json`, without
  making older clients fail.
- `tests/test_checkin_geo_metadata_contract.py` and
  `tests/test_finish_wizard_frontend_contract.py`: locked backend persistence
  and frontend payload contracts.
- `docs/DATA_PROTECTION.md`: updated the GPS storage row to include the new
  metadata fields.

Verification:
- `node --check frontend/js/checkin.js`.
- `node --check frontend/js/finish-wizard.js`.
- `python3 -m py_compile backend/main.py`.
- `git diff --check`.
- Focused check-in/DailyPlan tests: `76 passed, 1 skipped`.
- Full suite: `783 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 2 with the shared frontend API timeout/retry/abort layer or the
  durable offline finish/check-in outbox, since photo, voice, required
  geolocation, DailyPlan acceptance validation, and geo metadata now have
  concrete coverage.

## 2026-09-14 Splash iOS markup cleanup handoff

Current autonomous slice: Stage 1 Splash visual alignment follow-up from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`, continued after deployed
commit `afc64c0` already carried the light splash CSS.

Implemented in this slice:
- `frontend/app.html`: replaced the old black/gold splash SVG scene and
  astronaut placeholder with a compact light Promonta worksite mark that matches
  the iOS splash styling.
- `frontend/app.html`: removed unused legacy splash scene/star/hook CSS while
  keeping the existing splash bootstrap IDs and retry/error behavior intact.
- `tests/test_splash_ios_frontend_contract.py`: new static contract for the
  light splash markup, iOS token styling, reduced-motion guard, bootstrap ID
  wiring, and no-legacy-scene regression checks.

Verification:
- `git diff --check`.
- Focused Splash/feed/home/object/profile checks: `18 passed`.
- Adjacent premium frontend contracts: `48 passed`.
- Full suite: `779 passed, 1 skipped` with only existing deprecation warnings.

Next recommended slice:
- Continue Stage 2 worker start/finish evidence, geolocation, retry/offline, and
  DailyPlan validation, since Stage 1 visual polish is now substantially covered
  by the recent Team/Control, Alerts, Needs, Calendar, Tools, Profile, Dashboard,
  Feed/Object, and Splash passes.

## 2026-09-14 Team/Control iOS cockpit polish handoff

Current autonomous slice: Stage 1 Team/Control premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: marked `view-working-objects` as an `ios-page`, converted
  the summary/plan mode switch to button-based `ios-segmented` controls, and
  added a scoped iOS bridge for Team stat tiles, list rows, status pills, plan
  chips, object/plan list groups, and assignment bottom sheets.
- `frontend/js/home.js`: added a local `WO_ICONS` SVG set for plan date picking,
  Control Day, and collapsible chevrons; replaced Team plan emoji controls and
  tagged existing Team render output with `ios-stat-tile`, `ios-list-row`,
  `ios-status-pill`, `ios-action-button`, and `ios-bottom-sheet` classes.
- `tests/test_team_ios_frontend_contract.py`: new static contract for the Team
  iOS page markup, shared primitives, SVG controls, status pills, assignment
  sheet, and no-emoji Team controls.

Verification:
- `node --check frontend/js/home.js`.
- `git diff --check`.
- Focused Team/Home/team-hours checks: `15 passed`.
- Adjacent premium frontend contracts: `42 passed`.
- Full suite: `773 passed, 1 skipped` with only existing deprecation warnings.

Next recommended Stage 1 slice:
- Continue with Splash/Feed/Objects polish, then move into Stage 2
  start/finish evidence, geo, retry/offline, and DailyPlan validation.

## 2026-09-14 Alerts iOS sheet polish handoff

Current autonomous slice: Stage 1 Alerts sheet premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/home.js`: added a local `HOME_ALERT_ICONS` SVG set for the Alerts
  title, severity states, activity comments, close action, and chevron.
- `frontend/js/home.js`: converted Alerts filter tabs to real buttons, replaced
  emoji severity/activity markers with semantic SVG icons, and kept alert
  deep-link and dismissal behavior unchanged.
- `frontend/app.html`: added scoped iOS bottom-sheet, segmented filter, grouped
  list, alert row, and safe-area action styling for `#alerts-modal`.
- `tests/test_alerts_ios_frontend_contract.py`: new static contract for the
  Alerts icon set, button tabs, iOS CSS bridge, semantic icon classes, and
  no-emoji severity rendering.

Verification:
- `node --check frontend/js/home.js`.
- `git diff --check`.
- Focused Alerts/Home/Needs checks: `13 passed`.
- Adjacent premium frontend contracts: `39 passed`.
- Full suite: `770 passed, 1 skipped` with only existing deprecation warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with Team/Control surfaces, then decide whether
  to finish Splash/Feed/Objects polish or move into Stage 2 evidence/offline
  work.

## 2026-09-14 Needs iOS summary/list polish handoff

Current autonomous slice: Stage 1 `Потребности` premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: restyled Needs with an iOS page shell, explicit SVG add
  action, compact request form, stat tiles, segmented filters, restrained cards,
  and bottom-nav-safe spacing.
- `frontend/js/tasks.js`: rendered Needs counters as `ios-stat-tile` cards,
  upgraded request-card actions/statuses to shared iOS classes with local SVG
  chat/overflow icons, removed visible warning emoji fallback text, and made
  filters/form controls one-shot wired.
- `frontend/js/shared.js`: voice input buttons now restore their original rich
  HTML after recording/transcription instead of degrading to a bare emoji.
- `tests/test_needs_ios_frontend_contract.py`: new static contract for Needs
  markup, CSS bridge, stat tiles, SVG actions, plain labels, voice restore
  behavior, and one-shot listeners.

Verification:
- `node --check frontend/js/tasks.js`.
- `node --check frontend/js/shared.js`.
- Focused Needs tests: `24 passed`.
- Adjacent frontend contracts: `36 passed`.
- Full suite: `767 passed, 1 skipped` with only existing deprecation warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with Team/Control/Alerts surfaces, then move into
  Stage 2 worker start/finish evidence and offline validation work.

## 2026-09-14 Calendar light iOS polish handoff

Current autonomous slice: Stage 1 Calendar premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: mapped Calendar/absence palette variables to the shared
  light iOS tokens, added an iOS bridge for month navigation, day cells,
  availability states, period filters, metrics, request cards, and form controls.
- `frontend/app.html`: marked Calendar as an `ios-page`, replaced month arrows
  with accessible SVG buttons, and removed emoji prefixes from absence reason
  options.
- `frontend/js/abwesenheit.js`: added local SVG icons for chat/date/time
  affordances in absence cards, removed visible emoji fallback text, and made
  Calendar month/sheet/save event binding idempotent.
- `tests/test_calendar_ios_frontend_contract.py`: new static contract for the
  light Calendar palette, SVG controls, iOS CSS bridge, plain reason labels,
  SVG request-card icons, and one-shot listeners.

Verification:
- `node --check frontend/js/abwesenheit.js`.
- Focused Calendar/absence checks: `21 passed`.
- Adjacent frontend contracts: `32 passed`.
- Full suite: `763 passed, 1 skipped` with only existing deprecation warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with Team/Control/Needs/Alerts list surfaces, or
  move into Stage 2 start/finish location/photo validation if evidence capture
  is the higher priority.

## 2026-09-14 Tools iOS search/list polish handoff

Current autonomous slice: Stage 1 Tools premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: restyled the Tools screen with an iOS page shell,
  segmented summary filters, compact search field with icon, restrained tool
  cards, and bottom-nav-safe spacing.
- `frontend/app.html`: replaced the generic owner `+` button with an explicit
  SVG add action using `ios-icon-button` language and accessible labels.
- `frontend/js/tools.js`: made add/search/filter event binding idempotent for
  repeated SPA view initialization and kept filter `aria-pressed` states in sync.
- `tests/test_tools_ios_frontend_contract.py`: new static contract for the Tools
  iOS markup, CSS bridge, non-clipped search placeholder, owner add icon, and
  one-shot listener wiring.

Verification:
- `node --check frontend/js/tools.js`.
- Focused Tools tests: `20 passed`.
- Adjacent frontend contracts: `28 passed`.
- Full suite: `759 passed, 1 skipped` with only existing deprecation warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with Calendar/Team/Control/Needs/Alerts list
  surfaces, or move into Stage 2 finish/start validation if operational evidence
  capture is the higher priority.

## 2026-09-14 Profile iOS Settings polish handoff

Current autonomous slice: Stage 1 Profile premium UI polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/profile.js`: added a local `PROFILE_ICONS` SVG set and replaced
  Profile's avatar/edit/settings accordion emoji controls with inline SVGs.
- `frontend/js/profile.js`: tagged owner action, app status, access, and worker
  accordion surfaces as `profile-settings-list` groups, with worker KPI cards
  marked as `ios-card`.
- `frontend/app.html`: added a late Profile iOS bridge for the identity card,
  segmented tabs, Settings-style list groups, app/status rows, access rows,
  worker accordions, inputs, buttons, and group-label typography.
- `tests/test_profile_ios_settings_contract.py`: new static contract for the
  Profile Settings-style markup, SVG icon usage, compact iOS rows, and CSS
  selectors.

Verification:
- `node --check frontend/js/profile.js`.
- Focused Profile contracts: `41 passed`.
- Adjacent frontend/profile tests: `21 passed`.
- Full suite: `756 passed, 1 skipped` with only existing deprecation/cache
  warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with Tools/Calendar/Team/Needs list surfaces, or
  move into Stage 2 finish/start validation now that Chat location send is already
  present.

## 2026-09-13 Chat list and stale-load polish handoff

Current autonomous slice: Stage 1 Chat list/object chat polish from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/app.html`: moved Chat search into an inline iOS-style field above
  the contact strip, compacted the contact strip, and restyled the thread list as
  a grouped iOS list surface with smaller avatars, tighter rows, and the shared
  premium tokens.
- `frontend/js/chat.js`: added `_isCurrentChatRequest`,
  `_setChatThreadLoading`, and `_renderChatLoadError` so initial thread loads
  never repaint stale chat content and can recover through an in-pane retry.
- `frontend/js/chat.js`: closing a thread now clears hidden message DOM, draft
  text/height, reply state, render signatures, and loading state before returning
  to the chat list.
- `tests/test_chat_frontend_contract.py`: extended the frontend contract for
  inline search, iOS list styling, stale-load guard, retry state, and cleanup.

Verification:
- `node --check frontend/js/chat.js`.
- Focused chat tests: `22 passed`.
- Adjacent frontend contracts: `41 passed`.
- Full suite: `754 passed, 1 skipped` with only existing deprecation/cache
  warnings.

Next recommended Stage 1 slice:
- Continue screen-by-screen premium polishing with Profile as an iOS
  Settings-style list, or move to Stage 2 start/finish location/photo validation
  since chat location send is already wired.

## 2026-09-13 Home Today cockpit handoff

Current autonomous slice: Stage 1 owner Home dashboard replacement from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/js/home.js`: added the owner `Сегодня` cockpit above the legacy Home
  widgets, using existing objects, workers, shifts, tasks, blockers, and
  daily-plan endpoints to summarize today's operational state.
- The cockpit surfaces compact stats, active shifts, workers who have not
  started, awaiting confirmations, overdue needs/tasks, stage blockers, budget
  risks, daily-plan risks, and a calm `Остальное спокойно` state.
- `frontend/app.html`: added Home cockpit styling, flattened legacy KPI/quick
  cards into the shared iOS surface system, removed heavy depth transforms, and
  changed the visible Home title from `Dashboard` to `Сегодня`.
- `tests/test_home_today_cockpit_frontend_contract.py`: new contract test for
  the cockpit shell, endpoint usage, risk categories, clean state, and Home
  visual flattening.

Verification:
- `node --check frontend/js/home.js`.
- Focused Home/backend date contracts: `57 passed`.
- Frontend contract subset: `25 passed`.
- Full suite: `753 passed, 1 skipped` with only existing deprecation/cache
  warnings.

Next recommended Stage 1 slice:
- Continue the premium UI pass with the Chat list/object chat stale-load polish,
  then wire the Stage 2 location send button into the same composer pattern.

## 2026-09-13 Premium iOS foundation handoff

Current autonomous slice: Stage 1 shared visual system foundation from
`docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`.

Implemented in this slice:
- `frontend/css/tokens.css`: added the canonical `.ios-*` primitives for page,
  section, card, list, list row, segmented control, chip, action button, icon
  button, bottom sheet, empty state, status pill, and stat tile surfaces.
- `frontend/app.html`: added a late compatibility bridge that maps the current
  feed/object/profile/team/date filters, saved switches, stat tiles, sheets,
  cards, list rows, and bottom nav onto the same iOS-style token system.
- `tests/test_premium_ios_design_contract.py`: new contract test so the
  component primitives and bridge selectors stay present during incremental
  screen restyling.

Verification:
- Focused frontend contracts: `44 passed`.
- Full suite: `751 passed, 1 skipped` with only existing deprecation/cache
  warnings.

Next recommended Stage 1 slice:
- Use these primitives to rebuild the Home owner "Today" cockpit, replacing
  oversized dashboard/action cards with compact KPI, shift, warning, and list
  rows.

## 2026-09-13 Feed UX handoff

Current task: owner requested Instagram-like photo feed/comments, one comment
system everywhere, Apple/iPhone UI polish, fixed photo likes, fixed comment
`⋯` actions/replies, AI input visibility, and server structure documentation.

Implemented in this session:
- `backend/main.py` + `backend/core/paths.py`: persistent photo reactions in
  `feed_photo_reactions.json`; photo list returns `likes`/`liked_by_me`; photo
  comments store `reply_to`.
- `frontend/js/feed.js`: shared `IG_ICONS`, photo like API call, photo/news
  unified comment renderer, reply bars, menu actions, weather/news/photo action
  icons normalized.
- `frontend/app.html`: comments are bottom-sheet popups, not route pages;
  action menu z-index raised above the popup; iOS-safe 16px form inputs; AI
  screen hides bottom nav; object FAB motion/position tweak.
- `frontend/css/tokens.css`: Apple/SF system fonts first, Manrope fallback.
- Tests added/updated in `tests/test_feed_photo_integrity.py`,
  `tests/test_feed_photo_frontend_contract.py`, and
  `tests/test_assignment_lifecycle.py`.

Verification: `node --check frontend/js/feed.js`, `python3 -m py_compile
backend/main.py backend/core/paths.py`, and full `.venv-test/bin/python -m
pytest tests/ -q` passed (`737 passed, 1 skipped`).

After deploy, update the server-side structure file (`server-structure.md` per
repo references) with this same change summary if it is not part of the git
repo.

## 2026-09-13 Premium UI direction pause

Owner paused autonomous execution before a large UI pass and asked to send fresh
screenshots first. Do not start `autonomous-miniapp.timer` until explicitly
requested.

Prepared:
- `docs/PREMIUM_UI_DIRECTION_13sep2026.md`: Apple/iPhone premium design brief
  derived from static code audit. It identifies the current mixed visual systems
  (Old Money, Instagram, Telegram, Connecteam, dark chat) as the main reason the
  app feels raw. After the screenshot batch, it now includes screen-by-screen
  findings for Home, worker card, Feed, Comments, Chat, Object Detail, Profile,
  Tools, Calendar, Team, Needs and Alerts.
- `docs/PRODUCT_GAP_BACKLOG_13sep2026.md`: owner-pasted product gaps split into
  P1 daily-use gaps, P2 product UI, and P3 architecture/platform work.
- `docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md`: single source of truth
  combining the premium screenshot audit, previous 3 large autonomous stages,
  owner-pasted product backlog, and deploy/verification rules.
- `docs/AUTONOMOUS_3_STAGES_13sep2026.md`: superseded compatibility pointer to
  the unified master plan.

Server state at pause: Codex runner installed and resumable, but timer disabled
and inactive.

## Status

**Phase 0** (test isolation + incident root cause fix): COMPLETE  
**Phase 1** (security/correctness fixes): COMPLETE  
**Phase 2** (Production Control re-verification, 16 items): NOT STARTED  
**Phase 3** (Navigation restructure): NOT STARTED  
**Phase 4** (Worker glove-UX fixes): NOT STARTED  
**Phase 5** (Architecture preparation): NOT STARTED  
**Phase 6** (Network resilience — offline cache): NOT STARTED  
**Phase 7** (Performance + observability): NOT STARTED  

Tests: 557 passed, 1 skipped. Branch: main.

## Resumption note

Read `docs/EXECUTION_PLAN.md` and `docs/AUTONOMOUS_EXECUTION_RULES.md` before
resuming. The rules file mandates:
```bash
export PROMONTA_ENV=test
export MINIAPP_DATA_ROOT=$(mktemp -d)
test "$MINIAPP_DATA_ROOT" != "/home/promonta/agent/miniapp" || { echo 'REFUSING'; exit 1; }
```
before ANY pytest invocation. No production deployment without explicit owner instruction.

## What Phase 0 fixed (2026-09-08 incident root cause)

Watchdog loop was running tests with no `MINIAPP_DATA_ROOT` isolation, writing to production
`roles.json`. Three-layer fix:
1. `tests/conftest.py` — module-level env vars set before `import main` (import-time guard).
2. `backend/main.py` — RuntimeError at import if `PROMONTA_ENV=test` + prod DATA_ROOT.
3. `tests/test_worker_calendar_birthday.py` — env set at top before `import main as backend`.

## What Phase 1 fixed (security/correctness)

- **Budget percent column drift**: Live Sheets column is `'потрачено в % от бюджета'`;
  `objekte_lib.py` was using `'% бюджета'`. Added `get_budget_percent()` accessor in
  objekte_lib, expanded `BUDGET_FIELDS` in main.py to cover all three historical aliases.
- **Chat attachment crash**: `m.get('attachment', {})` → `(m.get('attachment') or {})` —
  explicit `None` attachment on plain-text messages caused AttributeError.
- **Task file race condition**: `create_task`/`update_task_status` now use
  `_lock_for(TASKS_FILE)` + `_load_tasks()`/`_save_tasks()` (old `update_json_transaction`
  bypassed locks and test mocks).
- **Angebot ACL**: `require_angebot_access` dependency enforces owner-only. Dead `manager`
  branch removed (set_role hard-rejects manager assignment anyway).
- **Module identity fix (test)**: `tests/test_phase1_security.py` uses
  `backend._load_repo_objekte_lib()` instead of `import objekte_lib` to get the repo's copy
  (main.py inserts `/home/promonta/agent` into sys.path[0], which would shadow the repo copy).

## Next step: Phase 2 — Production Control re-verification

Verify each of the 16 Production Control items (Rounds 1–7 in EXECUTION_PLAN.md) still works
correctly end-to-end. See `docs/EXECUTION_PLAN.md` §Phase 2 for the full item list.
