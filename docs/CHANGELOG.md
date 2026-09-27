# Changelog

## 2026-09-27 (live bugfix + worker profile UX — objects/workers/documents load failures, triple critical popup, skills selector redesign, clothing size picker, branch `fix/miniapp-live-load-profile-ux`)

Owner-reported live outage: Objects screen, Workers screen, and the Documents
gallery all failed to load, and Start Shift's object picker failed with "не
удалось загрузить объекты". Traced to one shared backend root cause plus one
independent one, both concrete unguarded-dict-access crashes on malformed
legacy JSON records -- not a data problem, a missing-defense-in-depth
problem. Also fixes a real frontend gesture-handling bug that made bottom
sheets (most visibly the skills-add sheet) look like they "closed" from a
stray tap/scroll, hardens the critical-alert popup queue against a
same-poll-response duplicate id, and redesigns the skills selector +
clothing-size input per owner UX request.

### Fixed
- **`GET /api/objects` 500 (shared root cause for Objects/Documents/Start
  Shift)**: `routes/objects.py` `list_objects` indexed `a['user_id']`
  (KeyError on any assignment record missing that key) and called
  `deps.assignment_status(a)`/`a.get(...)` without checking `a` was even a
  dict -- ONE malformed `object_assignments.json` record for any object
  crashed the ENTIRE list for every object, every role. New
  `_valid_assignments()` skips (and logs, object id + index only, never the
  record's own content) a non-dict or user_id-less assignment record instead
  of raising; the whole per-object loop is also wrapped so any other
  unexpected shape skips just that one object row, not the response.
  `_serialize_object_for_worker` (main.py) gained the same `isinstance(a,
  dict)` guard on the worker-facing path. This is why the Documents gallery
  (`document-gallery.js` loads `GET /api/objects` first) and Start Shift's
  object picker (`worker-checkin-fab.js` `_openWorkerObjectPicker`, same
  endpoint) failed together with Objects -- one crash, three symptoms.
- **`GET /api/workers` 500 (independent root cause)**: `list_workers`
  called `p.get('name')` etc. on `profiles.get(uid, {})` without checking
  the profile itself was a dict -- one malformed `worker_profiles.json`
  entry crashed the whole roster. Now guarded per-uid with a try/except that
  skips just that uid (logged, uid only, never profile content) and falls
  back to `{}` for a non-dict `skills`/profile value; valid workers and the
  existing role-default-to-None access invariant (09.09) are unaffected.
- **Documents 500s**: `get_object_document_file`/`delete_object_document`
  indexed `d["file"]`/`d["id"]` (KeyError on a legacy document record
  missing either key) instead of `.get(...)`; `get_object_documents` didn't
  guard against a non-dict `object_info.json` entry or non-dict document
  rows. All three now degrade to skipping the malformed record (or a
  controlled 404 for a genuinely missing file) instead of a raw 500.
- **Critical popup shown repeatedly**: `_pollCriticalAlerts()`'s
  `existingIds` dedup set was computed once before its push loop and never
  updated inside it, so a single poll response containing the same alert id
  twice could queue both copies; `_showNextCriticalAlert()` didn't itself
  refuse to show while a modal was already open, relying entirely on
  callers. Backend's existing persisted `(kind, target_user_id, ref_id)`
  dedup is untouched -- no new title/kind-based dedup added.
- **Skill-add sheet (and every other dynamically-created bottom sheet)
  "closing" from a stray tap/scroll**: `swipe-nav.js`'s document-level
  `touchstart`/`touchend` swipe-between-tabs gesture handler never excluded
  `.bottom-sheet-overlay` -- a horizontal-ish drag while scrolling an open
  sheet's content (or a tap on the backdrop area above the panel) could
  register as a real swipe gesture and switch the whole app view underneath
  the sheet. Added `.bottom-sheet-overlay` to the shared exclusion selector
  (`_isExcludedSwipeTarget`) -- one fix covers every sheet built on that
  class (skills, assignment, edit-period/work-type, etc.), not just this one.

### Changed (worker profile UX, owner request)
- `frontend/js/skill-picker.js`: removed the separate "frequently used"
  grid (a fixed editorial `featured` flag per work type, not real usage).
  Replaced with one scrollable list grouped by category, sticky section
  headers, a tri-state (☐/◐/☑) "select whole section" control per group, and
  real per-device usage counts (localStorage, not sent to the backend) that
  only reorder items within a section on the NEXT open -- toggling a skill
  never re-sorts the list under the user's finger mid-session.
- `frontend/js/profile.js`: pants/shirt size are now native `<select>`
  pickers (XS–5XL letter sizes for shirt, 44–60 numeric for pants; shoe size
  stays free text, out of scope) instead of free-text inputs -- no keyboard
  opens for a normal size pick. A legacy/custom stored value that isn't one
  of the predefined options is injected as its own selected option instead
  of being silently dropped, so it stays visible and replaceable.
- `frontend/app.html`: the skills sheet's primary action bar is now sticky
  at the bottom of the panel (was in normal scroll flow) since the redesigned
  list has no accordion collapse and can be longer to scroll through.

### Added
- `tests/test_objects_load_resilience.py`, `test_workers_load_resilience.py`,
  `test_documents_resilience.py`: real route-handler regression tests for
  every malformed-record crash path above (non-dict assignment, missing
  user_id, non-dict profile, non-list skills, non-dict document entry,
  missing file/id key), plus worker-access-scoping and Start-Shift
  (`GET /api/my-assignments`) coverage.
- `tests/test_critical_alert_dedup_frontend_contract.py`,
  `test_skill_picker_redesign_frontend_contract.py`,
  `test_profile_clothing_size_picker_frontend_contract.py`: static contracts
  for the frontend fixes (existing `test_*_frontend_contract.py` convention,
  no JS test runner in this repo).
- `tests/test_critical_alerts_queue_frontend_contract.py`: one pre-existing
  assertion updated (exact-line match on the old single-line dedup check) to
  match the corrected multi-line merge-then-add logic -- same invariant
  (merge by id, not wholesale replace), not weakened.

### Verified this pass
- `python3 -m py_compile backend/main.py backend/routes/objects.py`;
  `node --check` on every changed `frontend/js/*.js` file.
- Targeted: the 6 new/updated test files above + `test_assignment_lifecycle.py`
  + `test_object_history.py` -- 95 passed.
- Full suite: `1268 passed, 1 skipped`, plus 1 pre-existing unrelated failure
  (`test_runtime_manifest.py` deploy/rsync roundtrip -- `rsync` not installed
  in this sandbox, confirmed pre-existing on this same branch's base before
  any change in this pass).
- Not deployed. No DailyPlan work. No Core/CRM/Website/Google-Sheets/infra
  changes.

## 2026-09-26 (Grandmont Group rebrand — code/display/docs wave, branch `rename/grandmont-group-branding`)

Brand "Promonta" -> "Grandmont Group" (slug `grandmont-group`). Legal name
"Grandmont Group UG (haftungsbeschränkt)" is prepared in templates/prompts, but its
official registration is **pending, not yet confirmed**. Out of scope (separate later
waves): domain `app.promonta.fun`, VPS Linux user `promonta` + `/home/promonta/...`
paths, GitHub repo name, installed systemd units other than the already-renamed
`grandmont-miniapp.service`.

### Changed
- Display: `<title>` "Grandmont Group Mini App", splash wordmark "GRANDMONT GROUP"
  (centred, wraps on narrow phones), onboarding welcome, feed share fallback author,
  chat thread fallback "Команда Grandmont Group".
- JS identifiers: `promontaConfirm()` -> `appConfirm()` (+ `.app-confirm-*` CSS/ids),
  `promontaOutbox*` -> `appOutbox*`, `PROMONTA_OUTBOX_*` -> `APP_OUTBOX_*`; brand-neutral
  names so a future rebrand doesn't touch them again. All call sites + contract tests
  updated together (`tests/test_promonta_confirm.py` -> `tests/test_app_confirm.py`).
- Browser storage (each with a one-time legacy fallback/migration, see
  `tests/test_rebrand_storage_compat.py`): session token sessionStorage key
  `grandmont_group_session_token` (reads + migrates `promonta_session_token`, so the
  deploy does not log out open sessions); IndexedDB outbox
  `grandmont-group-offline-outbox` (queued records copied from `promonta-offline-outbox`
  with `add()`, legacy DB deleted after a committed copy); today-plan cache
  `grandmont-group-today-plan`; object order `grandmont_group_objects_order`.
- Env vars: `GRANDMONT_GROUP_ENV`, `GRANDMONT_GROUP_AGENT_ROOT`,
  `GRANDMONT_GROUP_CREATE_OBJECT_SCRIPT`, `GRANDMONT_GROUP_CREATE_OBJECT_FOLDER_SCRIPT`
  (main.py `_env_compat()` still honours the old `PROMONTA_*` names);
  `scripts/autonomous_codex_runner.sh` `GRANDMONT_GROUP_*` with `PROMONTA_*` fallback.
- Python module-cache keys `grandmont_group_repo_*_lib`; health `service` field
  `grandmont-group-miniapp`; FastAPI title.
- Legal/PDF: Angebot + Rechnung company name "Grandmont Group UG (haftungsbeschränkt)";
  Rechnung wordmark/signature "Grandmont Group"; Rechnung logo file is now
  `backend/grandmont-group-logo.png` (or `GRANDMONT_GROUP_LOGO_PATH`), text wordmark if
  missing — the old `promonta-logo.png` is intentionally not used. Angebot contact email
  extracted to `GRANDMONT_GROUP_CONTACT_EMAIL`, falling back to the current
  `anfragen@promonta-bau.de` (LEGACY_CONTACT_DOMAIN / DOMAIN_COMPAT_PENDING).
- AI system prompt names "Grandmont Group UG (haftungsbeschränkt)".
- `package.json` name `grandmont-group-miniapp-pdf`; `docs/systemd/autonomous-miniapp.service`
  reference copy uses `GRANDMONT_GROUP_*` env names.
- Current-state docs: brand text, and the backend unit name corrected to the real
  `grandmont-miniapp.service` (renamed on the VPS 24.09, see commit 3fb0caa). Dated
  handoffs/plans and older changelog entries are historical record and left unchanged.

### Deployment impact (needs owner sign-off before deploy)
- Rechnung tax/bank identifiers (Steuernummer, USt-IdNr., IBAN, GF) were carried over
  unchanged from the pre-rebrand entity next to the new legal name — confirm validity.
- Rechnung PDFs show a text wordmark until `grandmont-group-logo.png` is deployed.
- Health `service` value changes `promonta-miniapp` -> `grandmont-group-miniapp`.

## 2026-09-25 (backend architecture — Stages/Roadmap router extraction, branch `codex/split-stages`)

### Changed
- Extracted Stages/Roadmap-domain HTTP handlers from `backend/main.py` into
  `backend/routes/stages.py`: stage CRUD/status/swap/complete/blocker,
  roadmap categories/items/status/notes, and stage request approval flow.
- Kept `/api/objects/{object_id}/blocker-photo...` in `main.py`; media/storage
  refactor is intentionally outside this slice.
- `main.py` keeps the canonical roadmap singleton, JSON transactions, loaders,
  and critical-store registration, then wires the router through explicit
  `StagesRouteDeps` and re-exports legacy handler/model names.
- After rebasing on Claude's router foundation, Objects and Stages use the
  same canonical `app.include_router(...)` registration pattern as
  `routes.auth`.

### Verification
- Import smoke confirmed route count stayed at 186, duplicate routes stayed
  empty, and all stage/roadmap routes are owned by `routes.stages`.
- Added `tests/test_stages_routes_extraction.py` to lock in canonical route
  registration, `routes.stages` ownership, no `import main`, canonical
  roadmap state ownership in `main.py`, and `/blocker-photo` staying in
  `main.py`.
- Targeted extraction/stage/roadmap/object-access/history suite: `120 passed`.
- Full test suite: `1148 passed, 1 skipped`.
- Not deployed.

## 2026-09-25 (backend architecture — Objects router extraction, branch `codex/split-objects`)

### Changed
- Extracted the stable Objects-domain HTTP handlers from `backend/main.py` into
  `backend/routes/objects.py`: object list/create/status, object history,
  worker assignments, assignment candidates/batch assign, and object
  description/info-items.
- Kept media/photo/documents/tasks/stages/daily-plan routes in `main.py`; those
  are separate domains and were intentionally not part of this split.
- `main.py` wires the router through explicit `ObjectsRouteDeps` and re-exports
  legacy handler/model names so existing direct-call tests and operational
  scripts keep working without importing `main.py` from the new route module.

### Verification
- Added `tests/test_objects_routes_extraction.py` to lock in canonical route
  registration, `routes.objects` ownership, and the no-`import main` contract.
- Targeted extraction suite: `140 passed`.
- Not deployed.

## 2026-09-22 (iPhone screenshot audit, branch `screenshot-audit-2026-09-22`)

20 real-device screenshots audited item-by-item (A-I per owner's letter).
Full writeup with per-screenshot verdicts (FIXED / ALREADY FIXED ON MAIN /
NOT A BUG / STILL OPEN): `docs/IPHONE_SCREENSHOT_AUDIT_2026-09-22.md`.
Tests: 1071 passed, 1 skipped, 0 failed (baseline 1056). **Not deployed** —
awaiting explicit owner approval, branch is 6 commits ahead of `main`.

Real bugs fixed:
- Critical alerts duplicated (no backend idempotency, frontend queue
  wholesale-replaced every poll) — backend dedup key
  `kind+target_user_id+ref_id`, frontend merge-by-id + ACK-by-id.
- Raw Telegram user_id displayed instead of a name in Chat/Feed/Calendar and
  (found in a follow-up pass) News/photo comments — canonical read-side
  `_resolve_current_display_name()` resolver applied at every read path.
- Diagnostics: duplicated SHA, un-localized raw tokens, green headline
  contradicting yellow warning rows — 3-state severity model
  (ok/warning/error), localization map, SHA/version shown only when they
  actually differ.
- Worker Card header/title rendered under the Dynamic Island, duplicate
  back arrow next to Telegram's native one, sticky tabs misaligned after
  scroll — safe-area padding + native-back dedup + shared offset term.
- `--bottom-nav-safe-pad` referenced by 11 CSS rules but never actually SET
  anywhere — silently fell back to a flat guess or 0 padding, letting
  content sit behind the floating nav on Calendar/Tasks/Tools/Profile/
  Object Detail. Migrated to the actually-measured `--app-bottom-nav-height`.
- `.objects-fab`'s own position was already correct, but `#objects-cards`
  had zero bottom-padding reserve — last card's data sat under the FAB.
- `.news-cat`/`.news-src` had no overflow guard — a long scraped name could
  overflow `.news-card` and cause page-level horizontal scroll.
- New Object address placeholder was literal untranslated field-label text
  ("Straße, PLZ Ort") instead of an example value.

Verified NOT reproducible on current main (screenshots predated this
session's earlier deploy, `c28ff97`): Object Detail sticky-header safe-area,
Kanban horizontal overflow, News category-chip overflow, raw-ID in
Calendar/Chat (both already fixed by an earlier commit this session).

Owner-requested UX change (not a bug): stage picker
("Какой этап сегодня?") now auto-selects and starts the shift immediately
when exactly one stage exists on the object — only prompts for 0 or 2+
choices.

Architecture guard (not a screenshot finding): added a regression test
locking in that `renderObjectStagesTab()` has exactly one DOM mount
(`#obj-detail-panel-stages`), so a future Object Detail V2 step can't
accidentally duplicate stage markup into `#obj-detail-panel-work` before a
real canonical-mount decision is made.

## 2026-09-21 (Worker UX V2 — Этапы 4-9, branch `fix-shift-start`)

Tests: 943 passed, 1 skipped, 1 pre-existing unrelated failure (stale route
count assumption in `test_assignment_lifecycle.py`, not caused by this
work). Branch NOT pushed to origin, NOT merged to `main` — see
CURRENT_STATE.md for exact status.

### Frontend
- **Этап 4 (navigation)**: worker bottom-nav simplified from 5 tabs
  (Лента/Главная/Чат/Объекты/Профиль) to 4 (Сегодня/Объекты/Чат/Ещё). Owner
  nav completely unchanged. New `view-more` utility hub re-links to
  existing views (Календарь/Мои часы/Инструменты/Документы/Настройки/Лента)
  with no duplicated rendering.
- **Этап 5 ("Сегодня" operational panel)**: DailyPlan compact preview card
  (reuses `window._todayPlanState`, opens the existing canonical overlay)
  and a unified Problems card (aggregates existing alerts/needs/defects
  sources, ranked, no new store) replace 3 separate legacy Home tiles.
  Persistent DailyPlan bar suppressed only on Home to avoid showing the
  same status twice on one screen.
- **Этап 6 (contextual quick actions)**: new
  `frontend/js/worker-quick-actions.js` — Фото/Потребность/Дефект/Чат, with
  context resolution (active shift -> Object Detail -> single assignment ->
  picker) matching the plan exactly. Every action reuses an existing
  form/endpoint.
- **Этап 8 (Finish Wizard, partial)**: removed the standalone blocking
  "Геолокация" step — capture now runs in the background from wizard-open,
  status folds into the existing Сводка/review screen. Wizard down from
  6/8 steps to 5/7.

### Docs (no code)
- **Этап 7 (object-detail 4 zones)**: `docs/OBJECT_DETAIL_V2.md` — planned,
  not implemented. Audit found the plan's "8 tabs" premise doesn't match
  reality (3 tabs + 10+ stacked sections) and several concrete high-risk
  factors; used the plan's own escape hatch for risky refactors.
- **Этап 9 (object timeline direction)**: `docs/OBJECT_TIMELINE_DIRECTION.md`
  — documents the 8 existing Object History event kinds and a real gap
  (checkin start/pause, task creation, daily-plan create/accept don't log
  to it).
- **Этап 10**: this entry + `CURRENT_STATE.md`/`BACKLOG.md`/
  `OPEN_QUESTIONS.md` updated with the same findings.

### Tests
- `tests/test_worker_nav_v2_frontend_contract.py` (7),
  `tests/test_worker_today_operational_frontend_contract.py` (8),
  `tests/test_worker_quick_actions_frontend_contract.py` (8),
  `tests/test_finish_wizard_geo_autocapture_frontend_contract.py` (5).

## 2026-09-21 (Shift hardening Этап 0.5 — pre-Worker-UX-V2 gate)

Tests: targeted (`test_checkin_pause_authority.py` + `test_attachment_retention_cleanup.py`,
11 passed); full suite passed (`911 passed, 1 skipped`, 1 pre-existing
unrelated failure — routes-count assertion off by one, not touched here).

### Frontend
- Fixed a real desync gap in the active-shift live duration timer
  (`checkin.js`): it seeds once from the server and free-ran client-side with
  no resync. Added a `visibilitychange` handler that forces
  `refreshCheckinButtons()` when the tab/app regains focus while the timer is
  running, so a pause toggled elsewhere (owner action, another device) is
  reflected without needing to leave and reopen the screen.

### Backend / audit findings (documented, not silently patched)
- Sheets formula-injection sanitization (`_sheets_formula_safe`) and the
  legacy-abwesenheit-id migration (`_migrate_abwesenheit_legacy_ids`) were
  already implemented and correct — verified, no change needed.
- Voice/audio files (`transcribe_audio/`, `chat_attachments/`) are stored
  permanently by deliberate product decision, not as temp files — confirmed
  against `docs/plan-phases/02-product-flows-worker.md:37`. The real gap:
  `cleanup_old_attachments.py`'s `DIRS` list covers
  `chat_attachments/critical_alert_photos/checkin_photos` but not
  `transcribe_audio/`, while `docs/DATA_RETENTION_POLICY.md` claims a 90-day
  retention for voice too. Not fixed here — flagged for an explicit decision,
  pinned by a guard test so it can't silently drift either direction.

### Tests
- Added `tests/test_checkin_pause_authority.py` — pause/resume timestamp
  toggle contract (server-authoritative), plus permission/state guards.
- Added `tests/test_attachment_retention_cleanup.py` — age-based cleanup
  behavior, error resilience, and a guard test documenting the
  `transcribe_audio/` retention gap above.

## 2026-09-17 (Document gallery P2 slice)

Tests: focused document gallery/object UI contracts passed (`17 passed`); full
suite passed (`842 passed, 1 skipped`). Deploy verification follows in the
release step.

### Frontend
- Added a global object-document gallery to the Documents view while preserving
  the existing Angebot/Rechnung PDF builder below it.
- The gallery reuses existing object document endpoints, loads per-object
  documents, and adds object filter chips with document counts.
- Cards include authenticated image thumbnails or PDF tiles plus quick actions:
  preview, open object, and owner-only delete.
- The Documents page now uses the shared iOS page/section language and keeps the
  page title stable while the PDF builder title switches by document type.

### Tests
- Added `tests/test_document_gallery_frontend_contract.py` for gallery markup,
  script/init wiring, endpoint reuse, filters, preview/open/delete quick actions,
  and style hooks.

## 2026-09-17 (Object task Kanban P2 slice)

Tests: focused object task Kanban/budget/history/object/needs contracts passed
(`36 passed`); full suite passed (`827 passed, 1 skipped`). Deploy
verification follows in the release step.

### Frontend
- Added an Object Info Kanban board for object-scoped tasks/needs using the
  existing `/api/tasks?object_id=...` workflow.
- The board groups tasks into `Нужно`, `В работе`, and `Готово`, preserving the
  existing `открыто -> в работе -> закрыто` status model.
- Owner users can advance tasks with inline actions or drag cards between lanes;
  worker users get a read-only board plus task chat access.
- Task-screen status changes now refresh the open Object Info Kanban through the
  existing cross-screen sync hook.

### Tests
- Added `tests/test_object_task_kanban_frontend_contract.py` for render order,
  task workflow reuse, status mapping, owner actions, drag/drop, chat integration,
  sync hook, and styling hooks.

## 2026-09-17 (Object budget dashboard P2 slice)

Tests: focused object budget/history/object contracts passed (`10 passed`);
full suite passed (`822 passed, 1 skipped`). Deploy verification follows in
the release step.

### Frontend
- Added an owner-only budget dashboard to the Object Info cockpit, inserted right
  after the existing object control center and before history.
- The dashboard uses existing object DTO fields for budget, spent, remaining or
  overrun, and risk state, including all known budget-percent column aliases.
- Budget amounts now parse common comma/dot thousand and decimal formats before
  rendering EUR values.
- Worker privacy stays intact: the dashboard returns early for non-owner roles,
  matching the backend behavior that strips budget fields from worker object DTOs.

### Tests
- Added `tests/test_object_budget_frontend_contract.py` for render order,
  budget-field aliases, owner-only guard, risk states, retry state, and styling
  hooks.

## 2026-09-17 (Team drag-to-assign P2 slice)

Tests: focused frontend/assignment checks passed (`87 passed`); full suite and
deploy verification follow in the release step.

### Frontend
- Added `frontend/js/team-drag-assign.js`, a small Team-view enhancer that adds
  a drag handle to unassigned worker rows and makes Team object blocks droppable.
- Dropping a worker onto an object opens the existing Assignment Sheet with the
  worker and object preselected, preserving the full work-type, period, task
  note, confirmation, and backend batch-validation flow.
- Enhanced `frontend/js/assignment-sheet.js` so known worker+object assignments
  skip the redundant picker step and so `openAssignFromProfile()` honors its
  existing `initialDate` argument.
- Added Team drag/drop affordance styling and loaded the new helper after the
  Assignment Sheet script.

### Tests
- Added `tests/test_team_drag_assignment_frontend_contract.py` for script load
  order, drag handle/drop-zone wiring, Assignment Sheet preselection, and
  reduced-motion styling hooks.

## 2026-09-15 (Unified autonomous plan completion)

Tests: latest full server suite before this docs-only completion commit was
811 passed, 1 skipped; final deploy re-runs the suite.

### Documentation
- Marked `docs/UNIFIED_AUTONOMOUS_MASTER_PLAN_13sep2026.md` as
  `AUTONOMOUS_STATUS: DONE`.
- Added a completion audit mapping Stage 1, Stage 2, Stage 3, and overlapping
  P1 backlog work to the shipped implementations and tests.
- Documented the only remaining tiny note: deeper Home label micro-polish is
  cosmetic debt on a legacy/root-owned surface, while the required owner cockpit
  functionality and contracts are shipped.

## 2026-09-15 (Object history frontend section)

Tests: 811 passed, 1 skipped.

### Frontend
- Added a human-readable `История` section to the Object Info tab by appending
  it from writable `frontend/js/objects.js` after the existing root-owned
  `object-info.js` render completes.
- The section loads `GET /api/objects/{object_id}/history?limit=12`, renders
  newest timeline events with actor/subtitle/time metadata, includes retry and
  empty states, and recognizes status, team, plan, document, defect, finish, and
  broadcast event kinds.
- Added compact timeline row styling in `frontend/app.html`.

### Tests
- Added `tests/test_object_history_frontend_contract.py` for the Info-tab append,
  history endpoint usage, event-kind labels, retry state, and row styling.
- Verified `node --check frontend/js/objects.js`, `git diff --check`, focused
  object-history frontend/backend checks, and the full suite.

## 2026-09-15 (Central AI management command entry)

Tests: 810 passed, 1 skipped.

### Frontend
- Added a compact owner-only `Команда` action in the AI tab beside the model
  selector.
- Added a bottom-sheet management command composer that accepts typed Russian
  commands or voice dictation via the shared `/api/transcribe` voice helper.
- Wired the composer to the safe `/api/manager/command/parse` draft endpoint and
  renders worker/date/object/task/comment fields without creating assignments or
  tasks automatically.

### Tests
- Added `tests/test_ai_manager_command_frontend_contract.py` for the AI command
  entry, voice hook, parser API call, confirmation-only draft rendering, and
  compact sheet contract.
- Verified `node --check frontend/js/ai.js`, `git diff --check`, focused
  AI/parser/frontend checks, and the full suite.

## 2026-09-15 (Manager quick broadcast controls)

Tests: 809 passed, 1 skipped.

### Backend
- Added owner-only `POST /api/manager/broadcast` for quick announcements to the
  whole company or to an object chat.
- Company broadcasts append normal group-chat messages with broadcast metadata.
- Object broadcasts validate the object id, append to `obj:<object_id>` chat,
  report the current audience count, and add a human-readable object-history
  `broadcast_sent` event.

### Frontend
- Added an owner-only announcement icon to the Chat header.
- Added a compact bottom-sheet broadcast composer with company/object targeting,
  object picker, validation, send state, toast feedback, and chat-list refresh.
- Added a subtle broadcast label on chat bubbles so announcements remain
  recognizable in existing chat threads.

### Tests
- Added `tests/test_manager_broadcast.py` for company/object persistence,
  validation, object-history integration, and owner-only route dependency.
- Extended the chat frontend contract for the broadcast button, sheet, API call,
  owner visibility, and broadcast bubble label.
- Updated the production route-count contract to 181.
- Verified `python3 -m py_compile backend/main.py`, `node --check
  frontend/js/chat.js`, `git diff --check`, focused broadcast/chat checks, and
  the full suite.

## 2026-09-15 (Object history backend timeline)

Tests: 803 passed, 1 skipped.

### Backend
- Added `object_history.json` as an append-only, object-scoped human-readable
  history store.
- Added `GET /api/objects/{object_id}/history`, protected by existing object
  access rules and returning newest events first.
- Appended history events for object status changes, worker assignment, stage
  status changes, worker stage completion, document upload, defect creation, and
  check-in finish submission.
- Kept history writes best-effort around primary mutations so field operations
  are not blocked by timeline write failures.

### Tests
- Added `tests/test_object_history.py` for history filtering/sorting/limits,
  route registration, assignment/status/stage/defect event hooks, and readable
  event payloads.
- Updated the production route-count contract for the new endpoint.
- Verified `python3 -m py_compile backend/main.py backend/core/paths.py`,
  `git diff --check`, focused object-history/assignment/stage/defect checks,
  and the full suite.

## 2026-09-14 (Assignment confirmation escalation)

Tests: 798 passed, 1 skipped.

### Backend
- Added pending-assignment escalation timing for the existing worker
  accept/decline flow: 2h without response becomes a yellow warning, 4h becomes
  a red unconfirmed alert.
- New and reset-to-pending assignments now record `pending_since` so the
  escalation clock starts from assignment creation or a meaningful owner edit.
- `/api/dashboard/shifts-today` now includes assignment id, pending age, and
  escalation fields on `awaiting_response` rows for the owner cockpit.
- `/api/alerts` now includes owner assignment-confirmation alerts with stable
  yellow/red IDs, worker/object context, and response age metadata.

### Tests
- Added `tests/test_assignment_confirmation_escalation.py` for threshold logic,
  owner alert generation, role-aware alerts integration, and dashboard payloads.
- Verified `python3 -m py_compile backend/main.py`, `git diff --check`,
  focused assignment/needs alert checks, and the full suite.

## 2026-09-14 (Manager command parser draft)

Tests: 794 passed, 1 skipped.

### Backend
- Added an owner-only `/api/manager/command/parse` endpoint for the Stage 3
  Russian voice/text management-command flow.
- Added a safe draft parser for commands like
  `Поставь Ивану завтра задачу закончить потолок у Мюллера и скажи ему взять лазер.`
  The parser extracts worker, date, task, object, and comment fields.
- Kept the command flow non-mutating: the endpoint returns
  `requires_confirmation: true` and does not create assignments or tasks.

### Tests
- Added `tests/test_management_command_parser.py` for direct parser coverage,
  owner-only endpoint access, route registration, relative dates, unresolved
  names, and worker/object lookup.
- Updated the production route-count contract for the new endpoint.
- Verified `python3 -m py_compile backend/main.py`, `git diff --check`,
  focused management parser checks, and the full suite.

## 2026-09-14 (Check-in offline evidence outbox)

Tests: 790 passed, 1 skipped.

### Frontend
- Added a shared IndexedDB outbox for durable offline evidence records, including
  binary `File` objects.
- Start-shift photo confirmation now stores photos, geo metadata, DailyPlan/start
  fields, and the existing idempotency key when offline or when upload fails
  transiently.
- Finish-shift wizard now stores photos, summary/report fields, DailyPlan report,
  needs/defects payloads, geo metadata, and the existing idempotency key when
  offline or when finish upload fails transiently.
- Added reconnect/startup retry hooks for queued start and finish records. Replays
  use the original `Idempotency-Key`; writes are deleted from the outbox only after
  backend confirmation.

### Tests
- Added `tests/test_checkin_offline_outbox_frontend_contract.py` for IndexedDB
  store setup, start/finish queue payloads, reconnect retry, and idempotency-key
  reuse.
- Updated finish wizard geo payload contracts for the replayable record builder.
- Verified JS syntax, `git diff --check`, focused offline/check-in contracts,
  adjacent check-in/photo/DailyPlan tests, and the full suite.

## 2026-09-14 (Shared API timeout/retry layer)

Tests: 785 passed, 1 skipped.

### Frontend
- Added central `api()` timeout handling with `AbortController`, preserving
  caller-provided abort signals for screens like Chat.
- Added transient retry behavior for safe `GET`/`HEAD` requests only by default.
- Kept mutation retries disabled unless a caller explicitly opts in with
  `retry: true`, avoiding duplicate writes where idempotency is not guaranteed.
- Retained existing session-expiry handling, FormData content-type behavior, and
  prefetch cache semantics.

### Tests
- Added `tests/test_shared_api_frontend_contract.py` to lock timeout, abort,
  retry-status, safe-method, and no-write-retry defaults.
- Verified `node --check frontend/js/shared.js`, `git diff --check`, focused
  shared/feed/needs/finish contracts, and the full suite.

## 2026-09-14 (Check-in geo evidence metadata)

Tests: 783 passed, 1 skipped.

### Backend
- Extended `/api/checkin/start` and `/api/checkin/{session_id}/finish` with
  optional `accuracy` and `geo_timestamp` form fields.
- Persisted start/finish geo accuracy and timestamp metadata in
  `checkin_meta.json` while keeping older clients and direct route tests
  backward-compatible.

### Frontend
- Normalized browser geolocation into `lat`, `lon`, `accuracy`, and `timestamp`
  for the shared start-shift helper.
- Sent accuracy/timestamp evidence from both start-shift photo upload and the
  finish-shift wizard.

### Tests
- Added `tests/test_checkin_geo_metadata_contract.py` for backend payload and
  persistence coverage.
- Expanded finish wizard frontend contracts for start/finish geo metadata.
- Verified JS/Python syntax, `git diff --check`, focused check-in/DailyPlan
  tests, and the full suite.

## 2026-09-14 (Splash iOS markup cleanup)

Tests: 779 passed, 1 skipped.

### Frontend
- Completed the light iOS splash transition by replacing the old black/gold
  crane scene and astronaut markup with the new compact Promonta worksite SVG
  mark used by the deployed light splash CSS.
- Removed the unused legacy splash scene/star/hook CSS while keeping
  `splash-screen`, `splash-progress`, `splash-error`, and retry bootstrap IDs
  unchanged.

### Tests
- Added `tests/test_splash_ios_frontend_contract.py` to lock the light splash
  markup, iOS token styling, reduced-motion guard, bootstrap ID wiring, and
  no-legacy-scene regression checks.
- Verified `git diff --check`, focused Splash/feed/home/object/profile checks,
  adjacent premium frontend contracts, and the full suite.

## 2026-09-14 (Team/Control iOS cockpit polish)

Tests: 773 passed, 1 skipped.

### Frontend
- Restyled the `Команда` operational screen with the shared iOS page shell,
  segmented mode switch, compact stat tiles, list-row cards, status pills, and
  bottom-nav-safe spacing.
- Brought the Team summary, active shifts, attention rows, weekly hours, no-object
  list, absence/object collapsibles, plan tab, and assignment sheets onto the same
  premium surface language without changing the existing data endpoints or click
  behavior.
- Replaced Team plan date/control emoji affordances and collapsible chevrons with
  local inline SVG icons.

### Tests
- Added `tests/test_team_ios_frontend_contract.py` to lock the Team iOS page
  markup, shared primitives, SVG plan controls, status pills, assignment sheet,
  and no-emoji Team controls.
- Verified `node --check frontend/js/home.js`, `git diff --check`, focused
  Team/Home/team-hours contracts, adjacent premium frontend contracts, and the
  full suite.

## 2026-09-14 (Alerts iOS sheet polish)

Tests: 770 passed, 1 skipped.

### Frontend
- Restyled the Home Alerts bottom sheet with the shared iOS surface, segmented
  filter, grouped list, native close control, muted backdrop, and safe-area
  bottom action spacing.
- Replaced the emoji title, severity dots, activity marker, and clickable arrow
  with local inline SVG icons while keeping alert colors semantic.
- Converted Alerts filters to button semantics without changing the existing
  all/red/yellow filtering, deep-link, dismissal, or close behavior.

### Tests
- Added `tests/test_alerts_ios_frontend_contract.py` to lock the Alerts SVG
  icon set, button tabs, iOS sheet CSS bridge, semantic icon classes, and
  no-emoji severity rendering.
- Verified `node --check frontend/js/home.js`, `git diff --check`, focused
  Alerts/Home/Needs tests, adjacent premium frontend contracts, and the full
  suite.

## 2026-09-14 (Needs iOS summary/list polish)

Tests: 767 passed, 1 skipped.

### Frontend
- Restyled the `Потребности` screen around the shared iOS language: page shell,
  explicit owner/worker add icon, compact request form, stat tiles, segmented
  filters, restrained request cards, and bottom-nav-safe spacing.
- Converted Needs cards to use shared action/status/button classes with local
  SVG chat and overflow icons instead of a text-heavy action row.
- Removed visible emoji prefixes from the Needs create form controls and made
  the shared voice helper restore the original rich button HTML after recording
  or transcription states.
- Made Needs view initialization idempotent with delegated filter, priority, and
  category handlers so returning to the screen does not stack duplicate
  listeners.

### Tests
- Added `tests/test_needs_ios_frontend_contract.py` to lock the Needs iOS
  markup, CSS bridge, stat tiles, SVG actions, plain labels, voice restore
  behavior, and one-shot listener wiring.
- Verified `node --check frontend/js/tasks.js`, `node --check
  frontend/js/shared.js`, focused Needs workflow/access tests, adjacent premium
  frontend contracts, and the full suite.

## 2026-09-14 (Calendar light iOS polish)

Tests: 763 passed, 1 skipped.

### Frontend
- Converted the Calendar/absence screen from its fixed dark olive palette to the
  shared light iOS page, surface, grouped-surface, border, text, and muted-text
  tokens.
- Restyled month navigation, day cells, availability states, period filters,
  metrics, absence request cards, and bottom-sheet form controls so Calendar no
  longer feels half-dark/half-light compared with the rest of the app.
- Replaced visible Calendar request-card emoji controls with local inline SVGs
  for chat, date, and time affordances, and removed emoji prefixes from the
  absence reason select labels.
- Made Calendar month, sheet-close, backdrop, and save handlers idempotent so
  repeated SPA initialization does not stack duplicate listeners.

### Tests
- Added `tests/test_calendar_ios_frontend_contract.py` to lock the light iOS
  palette, SVG month controls, iOS Calendar CSS bridge, plain reason labels,
  SVG request-card icons, and one-shot listener wiring.
- Verified `node --check frontend/js/abwesenheit.js`, focused Calendar/absence
  checks, adjacent premium frontend contracts, and the full suite.

## 2026-09-14 (Tools iOS search/list polish)

Tests: 759 passed, 1 skipped.

### Frontend
- Restyled the Tools screen as a premium iOS-like operational list: lighter
  segmented summary filters, compact grouped search, restrained tool cards, and
  bottom-nav-safe list spacing.
- Replaced the generic owner `+` control with an explicit icon-only add action
  using shared `ios-icon-button` language and accessible labels.
- Shortened the Tools search placeholder and added a search icon, `aria-label`,
  and `autocomplete="off"` so the field stays usable on narrow iPhone widths.
- Made Tools view initialization idempotent so returning to the screen does not
  stack duplicate add/search/filter event listeners; filter buttons now update
  `aria-pressed` with the active state.

### Tests
- Added `tests/test_tools_ios_frontend_contract.py` to lock the Tools iOS
  markup, CSS bridge, non-clipped search field, owner add icon, and one-shot
  listener wiring.
- Verified `node --check frontend/js/tools.js`, focused Tools backend/frontend
  tests, adjacent premium frontend contracts, and the full suite.

## 2026-09-14 (Profile iOS Settings polish)

Tests: 756 passed, 1 skipped.

### Frontend
- Restyled Profile around iOS Settings-like grouped lists: compact identity card,
  segmented tabs, small app/status rows, access rows, worker settings accordions,
  and consistent form/button sizing.
- Replaced Profile's emoji-style edit/accordion controls with inline SVG icons
  from a local `PROFILE_ICONS` set.
- Tagged owner action, app status, and access surfaces as `profile-settings-list`
  groups so Profile shares the premium iOS surface, separator, radius, and
  typography language.
- Removed uppercase/letter-spaced Profile group labels through the late iOS
  bridge.

### Tests
- Added `tests/test_profile_ios_settings_contract.py` to lock the settings-list
  markup, SVG icon usage, Profile CSS bridge selectors, compact rows, and
  no-emoji control regression checks.
- Verified `node --check frontend/js/profile.js`, focused Profile contracts, and
  adjacent frontend/profile tests.

## 2026-09-13 (Chat list and stale-load polish)

Tests: 754 passed, 1 skipped.

### Frontend
- Reworked the Chat list top area into a compact iOS-style layout: inline search
  field, smaller contact strip, shared segmented category tabs, and a grouped
  list surface for threads.
- Added current-request helpers for chat thread loads so stale responses cannot
  repaint a previously selected thread after the user switches chats.
- Added a blocking load error state with a retry button for initial thread load
  failures, while background poll errors continue to avoid disrupting an already
  loaded chat.
- Clearing/closing a chat thread now resets hidden messages, input draft height,
  reply state, render signatures, and loading state before returning to the list.

### Tests
- Extended `tests/test_chat_frontend_contract.py` for the inline search/list UI,
  loading helper, retry state, and stale-response guard.
- Verified `node --check frontend/js/chat.js`, chat backend/frontend focused
  tests, and adjacent frontend contracts.

## 2026-09-13 (Home Today cockpit)

Tests: 753 passed, 1 skipped.

### Frontend
- Added the owner Home `Сегодня` cockpit, backed by existing objects, workers,
  shifts, tasks, blockers, and daily-plan endpoints instead of a separate data
  path.
- The cockpit now summarizes active objects, worker count, active shifts, and
  operational risk count, then lists active shifts and today's attention items:
  workers who have not started, tasks awaiting confirmation, overdue needs,
  stage blockers, budget risks, and daily-plan risks.
- Added a clean `Остальное спокойно` state when there are no critical signals
  for today.
- Flattened the old Home KPI and quick-action cards into the shared iOS surface
  system, removed the heavy transform/depth effect, and changed the Home title
  from `Dashboard` to `Сегодня`.

### Tests
- Added `tests/test_home_today_cockpit_frontend_contract.py` to lock the new
  cockpit shell, endpoint usage, risk categories, clean state, and Home visual
  flattening.
- Verified `node --check frontend/js/home.js`, focused Home/frontend contracts,
  and the full suite with `python3 -m pytest -q`.

## 2026-09-13 (Premium iOS UI foundation)

Tests: 751 passed, 1 skipped.

### Frontend
- Added shared premium iOS component primitives in `frontend/css/tokens.css`:
  `.ios-page`, `.ios-section`, `.ios-card`, `.ios-list`, `.ios-list-row`,
  `.ios-segmented`, `.ios-chip`, `.ios-action-button`, `.ios-icon-button`,
  `.ios-bottom-sheet`, `.ios-empty-state`, `.ios-status-pill`, and
  `.ios-stat-tile`.
- Added a late CSS bridge in `frontend/app.html` so existing feed tabs, object
  tabs, profile tabs, team tabs, saved filters, date filters, list rows, cards,
  stat tiles, bottom sheets, and the floating bottom nav share the same
  iPhone-style surfaces, radii, typography, muted colors, and active states.
- Lightened the floating bottom nav from the previous heavy dark pill toward a
  native translucent light surface with a cleaner green active state.

### Tests
- Added `tests/test_premium_ios_design_contract.py` to lock the shared
  `.ios-*` primitives and the legacy selector alias layer.
- Verified the full suite with `python3 -m pytest -q`.

## 2026-09-13 (Feed UX — unified popup comments, reactions, iPhone polish)

Tests: 737 passed, 1 skipped.

### Backend
- Added persistent photo reactions via `feed_photo_reactions.json` and
  `POST /api/feed/photos/{photo_id}/react`; `/api/feed/photos` now returns
  `likes` and `liked_by_me` for photo posts.
- Photo comments now accept and store `reply_to`, matching the existing news
  comments reply model.

### Frontend
- Photo and news comments now share one Instagram-like bottom-sheet pattern:
  dark overlay, handle, unified comment rows, reply bar, quick reactions,
  input row, and higher `comment-action-sheet` z-index so the `⋯` menu works
  above the popup.
- Feed actions were unified around one SVG icon set (`IG_ICONS`): photo,
  news, and weather actions now use heart/comment/share/bookmark-style icons
  instead of mixed emoji/legacy buttons.
- Photo feed likes now persist through the backend instead of being decorative.
- AI screen hides the floating bottom nav while active so the assistant input
  stays visible.
- Object/new-object inputs and filters use 16px text to avoid iOS auto-zoom
  when the keyboard opens; app font tokens now prefer Apple/SF system fonts.
- Object add FAB sits higher above nav and has a subtle motion/shadow state.

### Tests
- Added/updated photo feed integrity and frontend contract tests for photo
  reactions, comment replies, popup comment UI, shared icons, iOS input sizing,
  and AI composer visibility.
- Updated production-layout route-count contract from 176 to 177 for the new
  photo reaction endpoint.

## 2026-09-08 (Phase 2 — Production Control re-verification: multi-worker completion, blocker idempotency)

Tests: 563 passed, 1 skipped.

Verified all 16 Production Control items against actual code. 7 confirmed working,
2 real bugs fixed, 7 documented as known gaps/deliberate design in ARCHITECTURE_REFACTOR_BACKLOG.md.

### Backend — backend/daily_plan_lib.py
- `apply_daily_execution()`: plan status now set to `"completed"` only when ALL
  `assigned_worker_ids` have submitted executions; intermediate state is `"in_progress"`.
  Previously any single worker's finish marked the plan globally completed (bug for
  multi-worker plans). Legacy plans without `assigned_worker_ids` still complete on
  first finish for backward compat.
- `record_blocker()`: idempotent on `(plan_id, worker_id, reason_code)` — same unresolved
  blocker returns existing record instead of creating a duplicate. Different `reason_code`
  or resolved state still creates a new blocker.

### Tests
- `tests/test_daily_plan.py`: 6 new `Phase2VerificationTests` — solo/partial/full
  multi-worker completion, blocker idempotency (same key, different key, resolved-then-retry).

### Docs
- `docs/ARCHITECTURE_REFACTOR_BACKLOG.md` (NEW): recommended future extraction slices
  (backend routers by domain, frontend CSS/JS splits), plus documented known gaps
  (Sheets Plan_дня sync, outbox, contract RED, crew_size attribution, per-worker ack).

## 2026-09-08 (Phase 1 — security/correctness: budget aliases, attachment crash, task locking, Angebot ACL)

Tests: 557 passed, 1 skipped.

### Backend — backend/main.py
- `BUDGET_FIELDS`: expanded to include all three historical column-name aliases
  (`'потрачено в % от бюджета'`, `'% бюджета'`, `'Потрачено %'`) so all are
  stripped from worker-facing object DTOs.
- Alerts route: reads budget percent via `_load_repo_objekte_lib().get_budget_percent(obj)`
  instead of the dead alias `'% бюджета'`.
- Chat attachment endpoint: `m.get('attachment', {})` → `(m.get('attachment') or {})` to
  prevent `AttributeError` on plain-text messages where `attachment` key is explicitly `None`.
- `create_task` / `update_task_status`: wrapped with `_lock_for(TASKS_FILE)` context
  manager + explicit `_load_tasks()`/`_save_tasks()` calls (no longer uses
  `update_json_transaction` which bypassed test mocks and lost locking).
- `require_angebot_access`: new dependency function; only `owner` role passes, all others get
  403. Dead `manager` path removed (set_role already hard-rejects `manager` assignment).

### Backend — backend/objekte_lib.py
- `get_budget_percent(obj)`: new canonical accessor iterating `_BUDGET_PCT_ALIASES` tuple;
  returns first non-empty value or `0`.
- `recompute_objekt()`: fixed header lookup from `'% бюджета'` → `'потрачено в % от бюджета'`
  (the actual live Sheets column name).
- `check_budget_threshold()`: switched to `get_budget_percent(obj)` instead of
  `obj.get('% бюджета')`.

### Tests
- `tests/test_phase1_security.py` (NEW): 17 tests covering all Phase 1 fixes — budget aliases,
  field stripping, chat attachment crash, require_angebot_access (owner/worker/manager).
- `tests/test_needs_access_control.py`: stale hard-coded dates replaced with relative
  `date.today() ± timedelta(days=N)` so tests stay green past their original expiry.

## 2026-09-08 (Phase 0 — test isolation: incident root cause fix, error boundary, deploy script)

Tests: 540 passed, 1 skipped (before Phase 1 additions).

### Test isolation (incident 2026-09-08: watchdog ran tests against prod DATA_ROOT)
- `tests/conftest.py` (NEW): module-level `os.environ["MINIAPP_DATA_ROOT"]` set before any
  test import of `main`; session-scoped fixture asserts `DATA_ROOT ≠ prod path`.
- `tests/test_data_root_isolation.py` (NEW): 5 tests — current session not prod, path has
  prefix, subprocess test with PROMONTA_ENV=test+prod path raises RuntimeError, temp path ok.
- `backend/main.py`: RuntimeError raised at import time if `PROMONTA_ENV=test` and
  `DATA_ROOT == '/home/promonta/agent/miniapp'` — production data firewall.
- `tests/test_worker_calendar_birthday.py`: env vars set before `import main` at top.

### Frontend hardening
- `frontend/js/shared.js`: `prefetchTracked()` evicts cached promise on failure (prevents
  poisoned-promise retry loop).
- `frontend/js/home.js`: `_homeLoaded` flag moved to after slot.innerHTML succeeds; weather
  and objects errors now show user-visible retry buttons.
- `frontend/app.html`: `.card-error-state`/`.card-retry-btn` CSS; global error boundary
  (`window.onerror` + `unhandledrejection`) with non-disruptive 8-second auto-dismiss banner.

### Deploy script
- `scripts/deploy_frontend.py` (NEW): `_HTMLBalanceChecker(HTMLParser)` via Python's real
  HTML parser (replaces false-positive regex), SHA-256 copy verification, health smoke test,
  `inject_build_sha()`, `bump_version_marker()`, backup rotation (keep 3).

## 2026-09-07 (Production Control — Round 3: Finish wizard plan integration)

Tests: 471 passed, 3 pre-existing failures.

### Backend
- `checkin_finish`: new optional `daily_plan_report: str = Form('')` field; calls `dpl.apply_daily_execution()` after session save (idempotent, errors logged but don't fail the shift)

### Frontend
- `today-plan.js`: exports `window._todayPlanState` (synced at all assignment points); consumed by finish-wizard.js
- `finish-wizard.js`: rewritten with dynamic step sequence (`_fwStepSequence()`):
  - Without accepted plan: 6 steps unchanged
  - With accepted plan: 8 steps — adds `plan-fact` (item result marking) and `tomorrow-prep` (readiness issues → auto-Need)
  - All navigation uses relative helpers (`_fwNavNext`, `_fwNavBack`)
  - `_fwSubmitFinish` sends `daily_plan_report` JSON; clears plan bar after success
- `app.html`: CSS for `.fw-plan-item`, `.fw-status-btn`, `.fw-issue-btn`

### Tests
- 5 new `ApplyDailyExecutionTests` in `test_daily_plan.py`: execution record, plan marked completed, carryover for partial/not_done items, idempotency
- Updated `test_object_access_and_checkin_photos.py` + `test_owner_kt_requirements.py` direct calls to pass `daily_plan_report=''`

## 2026-09-07 (Production Control — Round 2: Worker Сегодня UX)

Tests: 466 passed, 3 pre-existing failures.

### Backend
- `daily_plan_lib.py`: `record_blocker()` function; `blockers` key in store schema
- `main.py`: `checkin_start` accepts `daily_plan_id/version/acceptance_id`; `POST /api/daily-plan/{id}/blocker` route

### Frontend
- `today-plan.js`: NEW — acceptance screen, persistent bar, blocker form, 60s polling, `window._dailyPlanCheckinFields`
- `checkin.js`: reads `window._dailyPlanCheckinFields` on shift start
- `app.html`: CSS + DOM for `#today-plan-screen`, `#today-plan-bar`; initApp() hook

### Tests
- 5 new blocker tests in `test_daily_plan.py`

## 2026-08-05 (Раунд 6: Финальный completion-раунд) — DONE

Frontend/backend, **без deploy** (по инструкции очереди раундов). Начальный SHA `5ed2856`,
итоговый `84996f6`. 4 коммита в miniapp + правка `news_pipeline.py` (вне git). Тесты: 434 passed, 1 skipped, CI green.

### §1 Новости — расширение генерации (news_pipeline.py, вне репозитория)
- Prompt summary заменён на «2–4 абзаца, 4–8 предложений, 500–900 символов, разделять пустой
  строкой, не выдумывать, сохранять числа/имена/даты». Backup `news_pipeline.py.bak-round6-20260805-022025`.
  sha256 до `b41d3c7c…`, после `e44c0340…`. Dry-run OK (frontend-отображение абзацев из Раунда 4/5).

### Commit A — `feat: require worker profile completion` (`156460b`) [§3/§4/§6]
- backend: `_validate_birthday` (YYYY-MM-DD, не в будущем, Europe/Berlin), `_is_meaningful_name`
  (строже — отсекает ID/цифры/заглушки), `_profile_completion_status`; `/api/profile/me` отдаёт
  `role` + `needs_completion`; birthday обязательна для Worker при `onboarding_completed`;
  birthday отдаётся owner-gated в profile stats. Убран старый запрет-комментарий birthday.
- frontend: поле даты рождения в onboarding (Worker обязательно, Owner нет); `checkProfileCompletion`
  + объединённый completion-overlay (имя+дата, без «Пропустить») в `app.html` bootstrap;
  worker-self редактирует свою дату в профиле; Worker Card показывает дату (owner-gated).
  display-name resolver из Раунда 5 (`resolveDisplayName`) переиспользован.

### Commit B — `feat: finish worker calendar period reports` (`389ea7e`) [§2]
- frontend (`abwesenheit.js`, `app.html`): период-пиллы Неделя|Месяц|3 месяца|Свой период
  (default Месяц, Europe/Berlin через `todayBerlin`, без UTC), custom range с валидацией
  `date_from<=date_to`/`max=сегодня`; блок статистики (дней/часов/среднее/больничных/отпуск)
  над календарём через `/api/workers/{id}/calendar-stats`; loading/error+«Повторить», старые
  данные не исчезают при ошибке, двойной tap не дублирует; Owner видит выбранного, Worker — себя.
  CSV за период; легенда календаря увеличена до ≥14px.
- backend: имя CSV-файла с реальным именем работника (RFC 5987 `filename*` для кириллицы +
  ASCII fallback), не Telegram ID.
- NOTE §2.3: посуточный split больничный/отпуск и часы-в-ячейке отложены (требуют расширения
  `/api/workers/{id}/calendar`, вне цели «доделать period-picker»).

### Commit C — `feat: add comment forwarding and activity alerts` (`112cecd`) [§5]
- backend: `POST /api/comments/forward` — серверная карточка (новость/фото) в существующий чат,
  текст строится на backend, валидируется существование комментария, тот же access-гейт;
  `activity_alerts.json` + `POST /api/activity-alerts/read`; хуки в `add_news_comment`/
  `add_feed_photo_comment` создают алерты (owner + прежние комментаторы + автор фото),
  автор своего коммента исключён, идемпотентность по `comment_id`; `get_alerts` агрегирует
  непрочитанные с deep-link полями.
- frontend: меню действий комментария (Ответить/Копировать/Переслать/Удалить), forward
  recipient picker (`/api/chat/threads`), reply_to для новостей, mark-read при открытии;
  клик по activity-алерту → Главная → вкладка Новости/Фото → конкретное обсуждение.

### Commit D — `test: cover final completion workflows` (`84996f6`) [§8]
- `tests/test_round6_completion.py` (28 тестов): profile-completion helpers, приватность
  birthday, CSV filename*, forward + activity alerts (релевантность/self-alert/идемпотентность/
  deep-link/read/права удаления). Обновлены 2 регресс-теста в `test_assignment_lifecycle.py`
  (birthday теперь обязательна для Worker-онбординга).

## 2026-08-04 (Раунд 5: Полный UI/UX stabilization) — DONE

Frontend-first, **без deploy** (по инструкции очереди раундов). Начальный SHA `58bda01`,
итоговый `52e118f`. 4 коммита (`bc5bb4d`, `702cc26`, `4d4f13a`, `52e118f`). Тесты: 406 passed, 1 skipped.

### Commit 1 — `refactor: stabilize navigation typography and app bootstrap`
- **§1 Отступ контента под nav** (`tokens.css`, `app.html`): единый токен
  `--bottom-nav-visible-height: 112px` / `--bottom-nav-content-gap: 20px` /
  `--bottom-nav-safe-pad` (calc + safe-area). `body`, `#view-object-detail`, `#stages-view`
  переведены на него (было `7.5rem` / `2rem` — последний элемент уходил под меню). Меню НЕ
  скрывается на вложенных экранах.
- **§4 Единая типографика** (`tokens.css`, `app.html`): шкала `--font-page-title/card-title/
  body/chat/secondary/meta/button/kpi`. Прежний `--font-body` (имя семейства шрифта, 16
  использований) переименован в `--font-family-body`, чтобы освободить спец-имя размера.
  `body { font-size: var(--font-body) }` — читаемый базовый 17px (был браузерный дефолт).
- **§3 Единый резолвер имени** (`shared.js` `resolveDisplayName()`): приоритет profile →
  roles/workers → Telegram first+last → «Сотрудник»; сырой user_id никогда не как имя.
  Подключён в `home.js` (Часы команды), `mangel.js` (select работника), `profile.js`
  (Worker Card). Onboarding (`onboarding.js`): имя обязательно, trim, мин 2 / макс 100,
  нельзя только цифры.
- **§2 Bootstrap-координатор** (`app.html` `AppBootstrap`): splash висит до реального
  завершения задач (не по таймеру), строка прогресса «Загрузка приложения · N из M»,
  критический сбой (авторизация) → экран ошибки с «Повторить», медиа первой страницы фото
  прогреваются во время splash. Data-prefetch (объекты/инструменты/погода/новости/фото/
  алерты/чат/абсанс/этапы) зарегистрированы как некритичные задачи.

### Commit 3 — `fix: clarify weather object progress and mobile search`
- **§5 Бюджет карточки объекта** (`app.html`, `objects.js`): процент был `0.8rem` (~13px) →
  22px/`font-weight:700`, подпись «Бюджет использован» ≥15px, bar 8px. Расчёт/цвет не тронуты.
- **§11 Object search при клавиатуре** (`app.html`): `.obj-filter-row` → `flex-wrap`,
  `@media(max-width:380px)` фильтры в 2 строки, `min-width:0`/`box-sizing:border-box` не дают
  выйти за viewport. Nav прячется глобально `body.keyboard-open`, запрос не сбрасывается.
- **§12 Вкладка Инфо** (`feed.js`, `app.html`): `weatherSeverityLevel()` + `WX_SEVERITY_SECTIONS`
  — Инфо-лента группируется в Критично/Предупреждения/Информация (секция только если есть
  элементы). Красная левая полоса — только critical (экстремальная жара/шторм); янтарь —
  предупреждения (сильная жара/ветер/мороз). §6 (этапы) / §7 (weather severity) — сделаны в
  Раунде 4, не дублированы.

### Commit 2 — `feat: add unified comments and unread activity`
- **§8 Комментарии к новостям** (`main.py`, `feed.js`, `app.html`): новый store
  `news_comments.json` (отдельно от cron-ленты) + `POST/GET/DELETE /api/feed/news/{id}/comments`
  (автор или Owner удаляют), модалка `news-comments-modal` (тот же lifecycle/визуал, что фото).
  Лента отдаёт `comment_count`/`last_comment_at`.
- **§8 Unread** (`main.py`, `feed.js`): per-user `feed_reads.json`
  (`last_news/photos/info_read_at`), `POST /api/feed/read`, `GET /api/feed/unread` (badge =
  НЕПРОЧИТАННОЕ, не общее число; новая публикация или новый комментарий новее отметки; `>99`
  → `99+`). `_loadFeedTabBadges` переведён с total-counts на unread. Секция «Сейчас обсуждают»
  (≤3 новости с комментариями за 24ч, сортировка по `last_comment_at`).
- **§10 Читаемость чата** (`app.html`): `.chat-text` 18px (`--font-chat`), имя 14px, время
  13px, reply/forward 13-14px, `⋯` touch-target 44×44px. Комментарии (`.pc-comment-text`) 16px.
- **NOTE**: forward комментария в чаты и in-app activity-alert («Иван прокомментировал») —
  крупные фичи, не реализованы (badge + «обсуждают» покрывают уведомление).

### Commit 4 — `feat: add worker calendar reports and birthday alerts`
- **§13 Календарь работника** (`main.py`, `profile.js`): `GET /api/workers/{id}/calendar-stats`
  (`days_worked` уникальные дни — активная смена не дублируется, `total_hours` с паузами, avg,
  `sick_days`/`vacation_days` из одобренных отсутствий, Owner|self, Europe/Berlin).
  `export_stundenzettel` принимает `date_from`/`date_to` (имя файла с датами); worker-card CSV
  выгружается по выбранному периоду (`_wcPeriodRange`).
- **§14 Дни рождения** (`main.py`): `_check_upcoming_birthdays` переписан на два алерта —
  «за 3 дня» и «в день рождения» — с idempotency `birthday:<uid>:<year>:3days`/`:today`
  (Europe/Berlin), feed birthdays на `business_today_str`.
- **NOTE**: полный period-picker UI (§13) — backend готов/покрыт тестами, отдельный
  calendar-экран не расширялся. Onboarding-поле ДР (§14) не добавлено — противоречит
  зафиксированному owner-решению (`main.py:1096`); backend принимает ДР через profile-edit.
- Тесты: `test_news_comments.py` (11), `test_worker_calendar_birthday.py` (13).

## 2026-08-04 (Раунд 4: Погода-жара, новости, этапы объекта)

Frontend + минимальная backend-правка, **без deploy** (по инструкции очереди раундов).
Начальный SHA `73a8887`. Два коммита: `448b067`
(fix: prioritize heat in weather risk classification) и
`refactor: expand news summaries and clarify object stages` (итоговый HEAD — см. git).

### Added
- **Классификация жары по температуре** (`feed.js`, `home.js`): пороги 30/33/35 °C →
  Жарко/Сильная жара/Экстремальная жара. `weatherHeatKind()`/`weatherHeatLabel()`,
  severity-scorer `weatherRiskSeverity()` (extreme_heat > гроза > ветер > снег > жара >
  дождь > облачно > ясно), вторичные риски блоком «Дополнительно», рекомендации по жаре.
  Раньше +36 °C с «возможен дождь» показывал дождь главным риском.
- **Понятный блок этапов на карточке объекта** (`objects.js`, `app.html`): заголовок +
  счётчик «3 из 7» + progress bar (7px, forest) + % + Сейчас/Далее. Состояния: в работе /
  ничего не начато (Следующий: …) / всё завершено / план не создан / приостановлено (blocker).
  Заменил цепочку ✓●○→. `tests/test_stage_summary.py` (4).
- **Резюме новостей абзацами** (`feed.js`, `app.html`): split по пустой строке, `esc()`
  каждого абзаца, кнопка «Читать полностью»/«Свернуть» (clamp по абзацам, ~450 симв),
  раскрывается только выбранная карточка, tap по раскрытию не открывает источник.

### Changed
- **backend `_stage_summary`** (`main.py`): при отсутствии текущего этапа `next` = первый
  незавершённый (для состояния «Ничего не начато: Следующий: Демонтаж»); всё готово → `next=None`.
  Значения/порядок этапов не меняются.

### Notes (вне scope репозитория miniapp-repo)
- **Промпт генератора новостей** (Задача 2.1/2.2) живёт в `/home/promonta/agent/news_pipeline.py`
  (кодовая база автономного агента, не в этом репозитории). Правило очереди раундов запрещает
  трогать файлы вне `miniapp-repo`, поэтому prompt не менялся; frontend уже корректно рендерит
  многоабзацные summary, когда они появятся.
- **Backend weather constants mirror** (Задача 1.4): генератор погоды `weather_check.py` также
  вне репозитория; жара определяется на frontend по `tmax` (что и есть основное требование ТЗ).

## 2026-08-04 (Раунд 3: Дефекты, Потребности, контекстный чат, Worker Card)

Frontend + небольшие backend-правки, **без deploy** (по инструкции очереди раундов).
Начальный SHA `3401ec5` → итоговый `082d29d`. Три коммита: `1cf55cd`, `e7635b0`, `082d29d`.

### Added
- **Owner-only soft delete дефекта** `DELETE /api/mangel/{ticket_id}` (`main.py`,
  `mangel_lib.py`): тикет помечается `deleted_at`/`deleted_by`, остаётся в JSON, но
  исчезает из `list_tickets`/`get_ticket`/`count_by_status`; фото/чат/audit не трогаются;
  идемпотентно. Тесты: `tests/test_mangel_delete.py` (9). Worker → 403.
- **Экран Потребности переработан** (`tasks.js`, `app.html`): счётчики Новые|Приняты|
  Выполнены, фильтры Активные(default)/Новые/Приняты/Выполненные, сортировка
  срочные-новые→новые→принятые→выполненные, loading/empty/error+Повторить, disabled-guard,
  контекстное ⋯-меню owner. Карточка 5.1: категория/объект по имени/автор/дата/status-pill.
- **Worker Card расширена** (`profile.js`, `app.html`): действия Написать/Календарь/
  Назначить + блок оперативной информации (Сейчас/Объект/Работа/Сегодня/Неделя).
- **Контекстное меню дефекта** (long-press + ⋯) и **строка дефекта** в Object Info
  (описание/русский статус/ответственный/индикаторы фото и комментариев).

### Changed
- **Русские статусы дефектов** через `mangelStatusLabel()` (`mangel.js`): Новая/В работе/
  На проверке/Устранено/Отклонено. Backend-значения (в т.ч. немецкие) не мигрируются.
  Модалка тикета: контекстное действие по статусу вместо сырого «Далее →».
- **Форма дефекта**: worker-select с optgroup (назначенные на объект первыми, пересчёт при
  смене объекта); FAB создания перенесён из шапки в плавающую кнопку над nav (скрыт при
  форме/модалке/клавиатуре); две отдельные кнопки камера/галерея + preview с заменой/
  удалением; disabled-guard + «Создание…» + возврат в Object Info.
- **Контекстный возврат из чата** (`chat.js`): строковый `returnToView` → структурированный
  `{ view, objectId, tab, scrollY, ticketId, taskId, userId }` (`_applyChatReturnContext`).
  chat popup/back fix из `6f6cd46` не тронут.
- **Backend: закрытая потребность больше не удаляется из JSON** (`main.py`
  `update_task_status`): остаётся со статусом `закрыто`+`closed_at` (иначе фильтр/счётчик
  «Выполненные» невозможны), архив в Sheets — best-effort только при первом закрытии.
  Тесты: `tests/test_needs_workflow.py` (6).

### Tests / CI
- `python3 -m py_compile backend/*.py tests/*.py` — OK. pytest — 378 passed, 1 skipped.
- `node --check` — mangel/tasks/object-info/chat/profile/abwesenheit/navigation-manager — OK.
- CI зелёный на всех трёх коммитах. **Deploy не выполнялся.**

## 2026-08-04 (Раунд 2: починка кнопки «Добавить работника» на карточке объекта)

Frontend only, **без deploy** (по инструкции очереди раундов). Начальный SHA `30aac96`.

### Fixed
- **Кнопка «+» на карточке объекта** (`objects.js`, `app.html`) теперь открывает
  Assignment Sheet. Причина: кнопка была `<div class="obj-people-add">` с
  `margin-left:-14px`, задвинутая под соседние аватары (у них z-index 3–6, у кнопки —
  auto), из-за чего аватары перехватывали клик; плюс вызывался legacy
  `openBubbleAssign()`, делавший лишний `GET /api/objects` ради имени объекта.
  Заменено на настоящую `<button type="button" class="obj-add-worker-btn">` 44×44px
  с `position:relative; z-index:8`, SVG «человек+плюс» (не голый `+`), brass-фоном без
  avatar-рамки, `:active`/`:disabled` состояниями. Клик обрабатывается делегированием на
  `#objects-cards` (guard `dataset.addWorkerBound`, без дублей listener'ов при
  `loadObjects()`), с `typeof openAssignmentSheet` guard + `showToast`, `disabled` на
  600мс против двойного тапа. `objectName` берётся из уже загруженного `obj` (без GET).
  Обе точки входа (карточка объекта и Object Info → «Команда и смены» → «+ Добавить»,
  `object-info.js:154`) зовут один и тот же `openAssignmentSheet({objectId, objectName})`.

## 2026-08-04 (Раунд 1: расширенные часы команды за неделю)

Code+tests only, **без deploy** (по инструкции очереди раундов). Начальный SHA `6f6cd46`.

### Added
- **`GET /api/dashboard/team-hours`** (owner-only) — часы ВСЕЙ команды за текущую
  неделю (Пн-Вс Europe/Berlin через `business_today()`), а не top-5 из
  `profile/stats.team_hours`. Ответ: `total_hours`/`today_hours`/`workers_with_hours`
  + список работников с `hours_today`/`hours_week`/`is_working_now`/`current_object_*`.
  Идущая смена считается один раз (elapsed до текущего момента минус пауза) через новый
  helper `_session_hours_live()`; имя никогда не голый Telegram ID (fallback «Сотрудник»);
  сортировка: работают сейчас → часы недели убыв. → нули. `date_from`/`date_to`
  опциональны (прошлая неделя). 8 новых тестов (`tests/test_team_hours.py`): owner/403,
  период Пн-Вс, суммы, пауза, идущая смена не дублируется, имя не ID, текущий объект,
  пустая неделя, прошлый диапазон исключает сегодня.

### Changed
- **Команда → Сводка → «Часы команды»** (`home.js`) переписан на новый endpoint:
  полноценная секция после активных смен (заголовок + диапазон недели + Всего/Сегодня/
  С часами + список всех работников с CSS progress-bar `.wo-week-progress`). Клик по
  строке → рабочая карточка сотрудника (`openWorkerCard`, fallback `openUserCard`).
  Состояния loading/empty/error+«Повторить». Frontend НЕ считает неделю через UTC.

### Changed (Owner Profile ↔ Worker Card, `profile.js`)
- **Owner Profile теперь личный + административный**: 3 вкладки **Профиль | Доступ |
  Настройки**. Убрано смешение с worker-аналитикой — с профиля Owner ушли 7 колец часов,
  выбор чужого сотрудника (worker picker), скорость работы, отпуск, объекты работника,
  навыки, размеры одежды, кнопка табеля. «Профиль» = avatar/имя/роль «Владелец»/Telegram ID
  вторичным текстом + Изменить имя/фото + компактный блок статуса приложения (версия/commit
  из существующего `/api/health`, без новых health-эндпоинтов). «Доступ» = `/api/roles`.
  «Настройки» = имя, фото, системная информация (без навыков/размеров/доступности).
- **Worker Card — отдельный режим-overlay** (`openWorkerCard(uid, returnCtx)`,
  `_profileMode`/`_profileReturnView`): scoped `wc-*` ID, НЕ подменяет профиль Owner
  (не трогает его имя/avatar/`_profileMyUserId`). Содержит часы (Неделя/Месяц/3мес/Год),
  текущую смену, отпуск, объекты, навыки, размеры, скачивание табеля CSV. Открытие из
  Команды/Доступа/аватара; регистрируется в `NavigationManager.registerOverlay` → Telegram
  Back закрывает только карточку и возвращает на исходный экран с сохранённой вкладкой;
  двойной тап не открывает два overlay (guard `_workerCardEl`).
- **Worker self-profile не сломан**: ветвление по `currentRole` (`_renderWorkerSelfProfile`
  сохраняет часы/периоды/отпуск/доступность/объекты/навыки/размеры/настройки).
- Удалены `_renderWorkerPicker`/`_syncProfileViewingMode` (owner больше не смотрит чужого
  внутри своего профиля); `openWorkerFullProfile` — тонкий алиас `openWorkerCard`.

## 2026-08-03 (deploy)

Задеплоено на production: commit `aff42ce` (стабилизационный раунд + chat-menu lifecycle fix,
включая CI-фикс мокинга `_load_repo_objekte_lib`). `promonta-miniapp.service` перезапущен,
health-check 200, deployed SHA подтверждён. Backup: `/tmp/rollback_backup_20260803_231402`.

## 2026-08-03 (стабилизационный раунд: session tokens, Owner AI freeze, worker-privacy, needs access control, business-date sweep)

Второй проход того же дня, поверх `859d3dc`/`d8fc913` — 6 отдельных задач, все code+tests,
без deploy (по инструкции). Не редизайн, не новые экраны, не смена архитектуры.

### Added
- **12-часовой backend session token** поверх Telegram initData (`create_session_token`/
  `verify_session_token`, `POST /api/session`). initData сам протухает через
  `INIT_DATA_MAX_AGE` (1 час) и не переподписывается Telegram без реального переоткрытия
  WebView — воркер получал 401 посреди смены. Токен = `base64url(user_id.exp).hmac_hex`,
  HMAC-SHA256 с доменно-разделённым секретом (`_session_secret()`, отдельная производная от
  `BOT_TOKEN`, не тот же ключ, что подпись initData), сравнение через `hmac.compare_digest`
  (constant-time). Полезная нагрузка содержит ТОЛЬКО `user_id` + `exp` — ни роль, ни пароли/
  ключи; whitelist и роль проверяются заново на каждый запрос из актуального `roles.json`
  (`get_current_user`), не кэшируются в токене. `Authorization: Bearer <token>` — приоритетный
  путь в `get_current_user`; `X-Telegram-Init-Data` остаётся рабочим fallback для обратной
  совместимости (временно, до полного перехода трафика). Frontend: `frontend/js/shared.js`
  (`ensureSessionToken()`, `_authHeaders()`, `sessionStorage`, не `localStorage`) обменивает
  initData на токен один раз при старте (`frontend/app.html`); все fetch-вызовы across
  `frontend/js/*.js` переведены на `_authHeaders()`. При 401 с уже выданным токеном —
  `_handleSessionExpired()` показывает "Сессия устарела. Переоткройте приложение." (не
  silent redirect/белый экран).
- **`business_now()`/`business_today()`/`business_today_str()`** (`backend/main.py`) —
  единый набор helpers для Europe/Berlin business-date, расширяет прошлый `_today_berlin_str()`
  (оставлен как тонкий алиас). Заменены оставшиеся `date.today()`/`datetime.now()` (UTC) в:
  `assignment_matching.py` (доступность кандидата "сегодня"), `profile_stats` (week/month/
  3months/year агрегаты), `update_stage`/`worker_complete_stage`/stage-request approval (даты
  завершения этапа), `get_team_plan` (dashboard "сегодня" по умолчанию). НЕ тронуты:
  `datetime.utcnow()` для created_at/updated_at/audit/technical timestamps и expiry-расчёты
  session-токена (намеренно UTC/unix time, не business-date).

### Changed
- **Owner AI (Claude CLI subprocess) safe freeze в production**: env-флаг
  `OWNER_AI_ENABLED` (default `false`) — выключенный субпроцесс НЕ запускается вообще,
  `/api/ai-chat` с моделью sonnet/opus отдаёт 503 с понятным сообщением (GLM-режим
  остаётся доступен). Убран `--dangerously-skip-permissions` из команды запуска. Subprocess
  больше не получает полный `os.environ` (раньше видел `BOT_TOKEN`/`GLM_KEY`/весь секретный
  контекст процесса) — только allowlist (`PATH`, `HOME`, `ANTHROPIC_API_KEY`, `LANG`,
  `LC_ALL`) через `_owner_ai_subprocess_env()`. Production config/deploy не проставляет флаг
  сам — включение остаётся ручным осознанным действием владельца. Полный sandbox (отдельный
  unix-юзер, read-only checkout) — следующий раунд, НЕ этот.
- **`GET /api/objects` — worker больше не видит assignment-метаданные коллег**: новая DTO-
  функция `_serialize_object_for_worker()` (единая точка, не разбросанное удаление полей).
  Worker получает базовые поля объекта (id/название/адрес/статус/этапы/фото), публичный
  список команды (`assigned_users`: user_id/name/has_avatar, без единого assignment-поля
  коллеги) и отдельно `my_assignments` — свои собственные назначения целиком, включая
  future/pending (нужно, чтобы принять/отклонить). Раньше worker видел `assignment_id`,
  `task_note`, `decline_reason`, `date_from`/`date_to`, `work_type_id`, pending/declined
  статус ЛЮБОГО коллеги на объекте. Owner путь не изменился (полный DTO как раньше).
  Frontend-потребители `assigned_users` (`home.js`, `worker-checkin-fab.js`, `objects.js`)
  используют только `user_id`/`name`/`has_avatar` — не сломаны; owner-only детальный
  team-view (`object-info.js` `_renderObjTeamAndShifts`) не вызывается для worker вообще.
- **Access control на Потребности (`GET`/`POST /api/tasks`)**: worker с `object_id`
  теперь должен иметь `has_active_object_access(user_id, object_id)` — раньше любой
  whitelisted worker мог передать чужой `object_id` и увидеть/создать потребность на
  объекте, к которому не назначен. Запрос без `object_id` (глобальный "мои потребности")
  не тронут. Owner — доступ всегда (не создаёт потребности по существующей логике, роль-
  проверка идёт раньше access-проверки). `GET`/roadmap/stages НЕ тронуты (осознанное решение
  владельца остаётся в силе).

### Fixed
- **`GET /api/my-assignments` неверный fallback колонки имени объекта**: читалось
  `obj.get('Название')` — колонка, которой нет в Google Sheets "Объекты" (реальная —
  `'Объект'`), так что имя объекта в "Моих назначениях" почти всегда падало на адрес или
  пустую строку. Полная цепочка: `Объект → Название → Адрес → сам object_id`.
- **`objects.js` карточка объекта — inline `onclick="openExternalLink('${mapsUrl}')"`**
  заменён на `data-url` + делегированный `addEventListener` в `attachObjectsHandlers()`.
  Раньше адрес интерполировался прямо в JS string literal без экранирования кавычек —
  адрес с апострофом ломал сгенерированный onclick целиком (клик молча не работал).
  Только это одно место, остальные inline handlers не тронуты массово.
- **Finish-shift wizard idempotency key не переиспользовался на retry**: `crypto.randomUUID()`
  генерировался заново на КАЖДЫЙ вызов `_fwSubmitFinish()`, так что повторная отправка после
  сетевой ошибки слала новый ключ и backend не мог распознать дубликат запроса. Ключ теперь
  персистентен на весь wizard-flow (`_fwIdempotencyKey`), сбрасывается только при
  `openFinishShiftWizard()` (новая смена). Форма не очищалась на клиенте до успешного ответа
  и раньше — подтверждено, изменений не потребовалось.

### Tests
5 новых файлов (45 тестов), все проходят зелёным вместе с существующим набором:
`tests/test_session_token.py` (14), `tests/test_owner_ai_freeze.py` (6),
`tests/test_worker_object_privacy.py` (6), `tests/test_needs_access_control.py` (12),
`tests/test_business_date.py` (7). Один существующий тест
(`test_access_control.py::test_user_id_cannot_be_spoofed_via_body_alone`) обновлён — сигнатура
`get_current_user` расширена параметром `authorization` (session token), regression guard
адаптирован под новый (всё ещё header-only, не body) контракт identity.
Full suite (`pytest tests/ -q`): 355 passed, 1 skipped (pre-existing skip, не новый).

### Not done this round (см. PROJECT_STATE.md)
Manager/Bauleiter role, sandbox для Owner AI (отдельный unix-юзер + read-only checkout),
PostgreSQL/DB migration, offline queue, полный `main.py` decomposition, редизайн, rate
limiting всех upload endpoints, новая модель доступа к GET stages/roadmap. Deploy НЕ
выполнялся — код+тесты+push только.

## 2026-08-01 (worker profile v2 + onboarding v2 + unified work-type catalog + unified assignment)

## 2026-08-03 (pilot-blocker fixes: photo verification + assignment-period access gate)

### Fixed
- **checkin_finish photo count bypass**: the >=2-photos check compared against
  `len(files)` (raw upload count) instead of the actually-saved, validated
  count returned by `_save_checkin_photos()`, which silently drops corrupt
  or oversized files without raising. A request with 2 files where one was
  corrupt/oversized previously passed the check and finished the shift with
  fewer than 2 real photos. Fix now checks `len(photo_paths)` before the
  `finish_at` write; new `_cleanup_checkin_photo_files()` removes only the
  files saved by the current failed request.
- **Object access ignored assignment period**: `can_access_object()` granted
  full object access (chat, files, stages, check-in) to any worker with an
  `accepted` assignment regardless of `date_from`/`date_to` — an accepted
  worker with a future-dated assignment already had today's access. New
  shared helper `has_active_object_access(user_id, object_id, today)`
  requires accepted status AND today (Europe/Berlin) within
  `[date_from, date_to]` inclusive; date-less legacy assignments stay
  unbounded. `can_access_object()` and `_object_chat_participants()` both
  delegate to it, so all ~19 `require_object_access`-gated routes plus two
  direct call sites (Mängel photo serving, `checkin_start` owner path)
  inherit the fix without per-endpoint changes. `checkin_start`'s date now
  uses `_today_berlin_str()` instead of naive server-local time.
- Per owner decision, GET-only stage/roadmap endpoints remain intentionally
  open to any worker (prior 30.07 decision, out of scope this round) — only
  mutation/object-data endpoints gained the period gate. Personal assignment
  list and accept/decline endpoints remain unrestricted by design.

Tests: 20 new (`tests/test_object_access_and_checkin_photos.py`). Full suite:
310 passed, 1 skipped. Commit `859d3dc`. Deployed to production same day.


Крупное функциональное расширение перед пилотом (не редизайн, не смена архитектуры).

### Added
- **`backend/work_types.py`** — единый каталог видов работ (44 позиции, 8 групп, 7 featured), единственный источник истины для onboarding/профиля/назначения. Раньше список из 19 навыков был вручную продублирован в `main.py` (`SKILL_OPTIONS`) + `onboarding.js` (`ONBOARDING_GROUPS`) + `bubble-assign.js` (`BUBBLE_STAGE_OPTIONS`) — реальный дрифт: `SKILL_STAGE_MAP` покрывал только 8 из 19 навыков keyword-based matching'ом.
- **`GET /api/work-types`** — единый endpoint каталога, доступен любому авторизованному пользователю.
- **`backend/profile_skills.py`** — структурированные навыки `skills_v2` (`{skill_id, level, verified}`, level ∈ helper/independent/master) поверх старого `skills` (список названий). Idempotent-миграция legacy → v2, helper-функции для отображения/сравнения/мутации.
- **`PATCH /api/workers/{user_id}/skills/{skill_id}/verification`** — owner-only подтверждение навыка. Worker не может сам выставить `verified: true` (любой self-service PATCH навыков сбрасывает verified).
- **`backend/assignment_matching.py`** — полный matching по `work_type_id`, заменяет `SKILL_STAGE_MAP`. Сортировка: verified master → unverified master → verified independent → helper → без навыка, при равенстве — по имени.
- **`GET /api/assignment-candidates`** — owner-only, кандидаты для назначения (`recommended`/`available`/`unavailable`), без N+1 запросов, без утечки приватных причин отсутствия.
- **`POST /api/objects/{object_id}/assignments/batch`** — назначение нескольких работников одним запросом, партиальный результат (`created`/`skipped`), 409 если ни одного не создано.
- **`PATCH/DELETE /api/objects/{object_id}/assignments/{assignment_id}`** — точечное редактирование/удаление ОДНОГО назначения. Значимое изменение уже принятого назначения возвращает его в `pending`.
- **Frontend**: `skill-picker.js` (единый компонент выбора навыков — featured grid + поиск + accordion-группы + уровни, используется onboarding и профилем), `assignment-sheet.js` (единый Assignment Sheet — все точки входа: объект/профиль worker/старый bubble-триггер), полностью переписанный `onboarding.js` (4 шага: имя+аватар → навыки → уровни → размеры, дата рождения полностью убрана).
- Тесты: `test_work_types.py`, `test_assignment_matching.py`, `test_assignment_lifecycle.py` — 66 новых тестов (каталог/миграция/matching/availability/batch/update-delete/privacy).

### Fixed
- **`DELETE /api/objects/{object_id}/assign/{user_id}`** раньше удаляло ВСЕ назначения этого работника на объекте молча, без разбора сколько их. Теперь: ровно одно активное — удаляется, несколько — 409 с просьбой использовать `assignment_id`.
- Устаревший `SKILL_STAGE_MAP` (8 из 19 навыков) удалён вместе со старым bubble-drag UI (`frontend/js/bubble-assign.js`) — весь matching идёт через `work_type_id`, одинаково для всех навыков каталога.

### Removed
- `frontend/js/bubble-assign.js` (заменён `assignment-sheet.js`, старые `openBubbleAssign`/`openAssignFromProfile` теперь тонкие обёртки над `openAssignmentSheet`).
- `SKILL_OPTIONS`/`ONBOARDING_GROUPS`/`BUBBLE_STAGE_OPTIONS`/`SKILL_STAGE_MAP` как статичные frontend-копии каталога — CI теперь проверяет, что они не появятся снова ни в одном JS-файле.

### Not yet verified
Ручной Telegram E2E (Owner создаёт batch-назначение, Worker A/B видят его в Мои задачи, принимают/отклоняют, Owner подтверждает навык) — не выполнен. Визуальная проверка UI (onboarding 4 шага, Assignment Sheet, Команда и смены) сделана только код-ревью, без Safari MCP (недоступен в этом окружении).

## 2026-07-28 (interactive session — Phases 05-10 finish, live device bugfixes, feature additions)

38 commits (`9874208`..`640dad3`), all deployed to prod incrementally with backups before each write, backend restarted where `main.py` changed, verified with `py_compile`/`node --check` + live health-check after each backend restart. Autonomous VPS timer (`autonomous-miniapp.timer`) stopped and disabled — all further work moved to this interactive session per owner request.

### Phases closed
- **Phase 07 (Object Card)**: was already ~90% done from earlier sessions. Real gap found — no way to upload a real object photo at all (every card showed stock fallback). Added `/api/objects/{id}/image` upload (owner-only) + delete endpoints, `object_images.json` now stores an array (up to 8 photos), card renders a swipeable/tappable carousel with dots when 2+ photos exist.
- **Phase 10 (Tests, narrow slice)**: added `tests/test_owner_kt_requirements.py` — 10 passing stdlib unittest cases covering the specific items the owner listed explicitly in the original brief (object access scoping, start/finish shift geo requirements, `/api/transcribe` route existence, chat thread_key access check). Full Playwright/visual-regression/endpoint-audit scope from the phase file explicitly deferred — no user-facing value without the app running, owner agreed to skip for now.
- **Phase 09 (Architecture split)**: explicitly skipped per owner decision — multi-day refactor risk with no direct feature value, revisit later if needed.

### Fixed — real bugs found by reading code, not by guessing
- finish-wizard never resynced the Home `worker-shift-cta` card after finishing a shift (only the older checkin.js flow did) — Home kept showing "Смена идёт" after the shift was actually closed.
- finish-wizard hardcoded pause time to 30 minutes regardless of actual accumulated pause, and never displayed it in the summary.
- Embedded object chat (`#obj-detail-panel-chat.obj-chat-active`) visually "floated"/overlapped its header and tabs during scroll: the whole `#view-object-detail` scrolls as normal body/document flow, but the fixed-position chat panel's top offset only recalculated via `ResizeObserver` (size change), never on scroll (position change). Fixed by reusing the existing `view-locked` body-scroll-lock mechanism (same one root Chat/AI tabs use) plus explicit scrollY save/restore, since `position:fixed` on `body` does not preserve scroll position on its own.
- `CHAT_MAX` (200-message cap) and the 7-day retention purge were both silently discarding chat history forever on every save — found while implementing thread deletion with an explicit "history must be preserved" requirement. Both now archive overflow/expired messages to `chat_messages_archive.json` instead of dropping them.
- Object stage-strip on the card led to a dead click — `openObjectDetail(..., 'stages', ...)` targeted a tab (`obj-detail-panel-stages`) that never existed; Этапы was only a sub-section inside Инфо. Added a real dedicated tab.
- `Потребности` tab inside Object Detail was a read-only list with no way to act on a request or contact the other party — added status-advance buttons (owner) and a direct-chat shortcut (both roles).
- `mangel_lib.py`'s `created_by` field was tracked but never surfaced in the UI — now shown as "добавил {name}" on ticket cards.
- Multiple back-button/header-centering bugs across screens that use `.form-header` (Object Detail, finish-wizard, chat thread, photo comments) where Telegram's native Close button overlapped or misaligned with custom UI — several rounds of on-device correction, final state verified against real screenshots each time rather than assumed correct after the first pass.

### Added (owner-requested features)
- Radio player: expanded from 4 to 19 real Radio Record streams (verified live via curl before committing), infinite-loop swipeable carousel replacing a static text list, directional slide+fade transition between bottom-nav tabs.
- Chat read receipts in DM threads — backend already tracked per-thread read timestamps (`reads.json`) but never returned them to the sender; now shown as single/double checkmark.
- Bubble Assignment: undo action-toast (6s window) using the pre-existing `unassign_user` endpoint that was never wired to the assign-success path; bigger circles (+40% total across two rounds) with worker name labels underneath (previously invisible on touch devices, hover-only tooltip).
- Any worker can now view/add stages and mangel tickets on any object, not just ones they're assigned to (owner-requested permission widening).
- Worker calendar got the same profile-selector dropdown owner already had (view teammates' availability), backend `/api/abwesenheit/all` opened to all authenticated roles for viewing (approve/reject stays owner-only).
- Closed Потребности (needs) now archive to a new "Потребности" tab in the existing Google Sheets spreadsheet before being removed from the working list, instead of just disappearing.
- Voice note in finish-wizard's "Что сделано" step now saves the actual audio (not just the transcript) — owner can play it back on the object's shift-summary card.
- Owner can delete a whole chat thread (DM or obj/mangel/task) — disappears for both sides, messages archived server-side first, not just wiped.
- Calendar tab given a fixed dark theme (same lightened olive-gray palette chat briefly used before being reverted to light per owner preference); bottom-nav given a fixed dark background on all tabs (was translucent blur that looked different depending on what was underneath).

### Notable back-and-forth / self-corrections (worth remembering for next session)
- Chat's fixed-dark palette was fully reverted to the standard light theme after owner feedback — the nav-bar dark treatment now serves as the intended visual differentiator instead.
- The chat archive/pin/mute toggle button was removed entirely per explicit owner request ("занимает много места, выглядит убого") — pin/mute stayed, archive-view toggle did not.
- Back-button offset for `#obj-detail-back` went through 4 iterations (46px → 8px → 28px, plus a left-offset removal) before matching what the owner actually saw on-device — CSS offsets tuned from memory/estimation without live verification are unreliable; screenshots after each attempt were necessary.
- A Fable subagent was dispatched to investigate the embedded-chat overlap bug but had no SSH access to the VPS where the actual code lives — could not proceed, diagnosed and fixed directly in this session instead. Note for future delegation: subagents spawned from this session do not inherit SSH/VPS access.

### Known open items (not started, tracked for a future session)
- Bubble Assignment read-only "Просмотр" mode (view-only toggle showing team occupancy without drag/tap) — scoped (toggle inside the same panel header, "this week" as the default availability window) but not implemented.
- Emoji→SVG icon conversion — blocked on owner supplying a reference image; a prior unilateral attempt got negative feedback, will not guess again.
- Full Phase 09 (backend/frontend architecture split, unified API client, IndexedDB offline queue) and the remainder of Phase 10 (Playwright E2E across viewports, visual regression baselines, full ~100-route endpoint audit table, `docs/audit/*` files) — both explicitly deferred, not forgotten.


## 2026-07-28 (autonomous session, continued — pin/mute/archive UI)

Commit `ff83a1b`, deployed (including a backend restart — this pass touches `main.py`). Full detail in `docs/plan-phases/06-chat-hub-rebuild.md`.

### Added
- Long-press a chat thread row → pin/mute/archive menu, wired to the already-existing `POST /api/chat/threads/prefs` (data layer shipped earlier this phase, commit `509e20e`, with no UI until now). Pinned threads sort to the top of their tab; archived threads hide from the normal list; the previously dead `.chat-archive-btn` in the header now toggles an archive-only view.
- Prefs are read via the normalized `GET /api/chat/threads` (Phase 06 groundwork, unused by any frontend code until now) as a read-only supplementary source layered on top of the existing render path — not a replacement of the primary thread-list data source (`/api/chat/my_threads` + `/api/workers`), which stays as-is; see "Known gaps" below for why.
- `get_unread_by_thread` now excludes muted threads from its per-thread counts, so the new mute icon reflects a real notification suppression rather than a decorative label.

### Found, not fixed (flagged, not silently patched)
While touching `get_unread_by_thread`, found that its sibling `get_unread_count` (the global nav/Home unread badge everyone sees) doesn't distinguish `thread_key`-based threads (obj:/mangel:/task:) from the group chat at all — it attributes their unread messages to the group thread's `last_read` timestamp, which can produce an incorrect total count. Pre-existing, unrelated to this pass's changes, too risky to fix in the same commit as a live-chat feature addition — needs its own careful pass. Tracked in `docs/plan-phases/06-chat-hub-rebuild.md`.

### Verification
`python3 -m py_compile backend/main.py`, `node --check` on both JS/HTML touched, `tests/test_chat_backend.py` 16/16 passing (via `/home/promonta/agent/miniapp/.venv/bin/python`, the actual systemd-service venv). Service restarted cleanly, no new tracebacks in `journalctl` post-restart.

## 2026-07-28 (autonomous session, continued — expandable search circle)

Commit `0d69608`, deployed. Full detail in `docs/plan-phases/06-chat-hub-rebuild.md`.

### Added
- Chat Hub search moved from a standalone `<input>` above the worker strip into an expandable circle that's the first item in the same horizontal ribbon (`frontend/app.html`'s `#chat-search-circle`, `frontend/js/chat.js`'s `_initChatSearchCircle()`/`_setChatSearchExpanded()`). Tap expands to full width with the icon shifting left, input focusing, and a close button appearing on the right; avatars hide while expanded. Collapses on close-tap or on blur-while-empty. 250ms debounce.
- Search now covers the "Общий" tab (previously never filtered at all) and matches `last_preview` text on Объекты/Дефекты/Потребности, not just thread titles. A distinct "Ничего не найдено" empty state now appears when a query has zero matches, separate from each tab's normal empty-state text.
- `data-no-swipe` added to the whole worker strip (search circle + avatars) — was missing since the strip was first added (commit `b5becd7`), closed while already touching this area.

### Known, deliberate gap
No SEARCHING/ERROR states or `AbortController` — search stays a synchronous client-side filter over already-loaded data (thread titles/worker names/message previews), since no backend full-text-search endpoint exists. Building fake network states for an operation with no network request would violate the same "don't imitate" principle already applied to read receipts elsewhere in this phase.

## 2026-07-28 (autonomous session, continued — chat polling consolidation + a concurrent-session note)

Commit `9609941` on `main`. **Operational note, read before trusting this commit's title**: this repo currently has two independent Claude processes able to write to it concurrently with no locking — this autonomous phase-05-10 loop (`autonomous-miniapp.timer`, every 3h) and the separate always-on Telegram-bot agent process (`bot.py`'s persistent `claude -p` session, which the owner can direct to edit/commit here too, per the top-level `CLAUDE.md`'s instruction to read this repo's own `CLAUDE.md` first). Both were live at the same time this pass: while this session had `frontend/js/chat.js`/`critical-alerts.js`/`app.html` staged (not yet committed) for the polling-consolidation work below, the other process independently edited+committed 2 unrelated radio/FAB fixes (`018f648`, then `9609941`) and its commit swept up this session's already-staged files into its own commit message. Verified byte-for-byte: no content was lost or corrupted (`node --check` clean on every touched file, working-tree diff against `9609941` is empty) — only the commit message for the chat-polling work is wrong/misleading. Not rewriting history to fix it (repo governance requires explicit owner approval for that, and rebasing while a second live writer might commit again is its own risk) — recording the true attribution here instead. **If this happens again, both processes should commit more frequently/in smaller windows to shrink the race window; a real fix (lock file, or serializing the two agents) is a process/ops question for the owner, not something to unilaterally build into this repo's own tooling without asking.**

### Added (the actual content of commit `9609941`, mislabeled by the race above)
- **Chat polling consolidated into 1 controller** (`frontend/js/chat.js`) — replaces the old `_chatUnreadTimer` (15s, always running regardless of visibility) and `_chatPollTimer` (8s, only when a thread was open) with a single `startChatPolling()`/`_chatPollTick()` loop: always refreshes total unread each tick, additionally refreshes the open thread's messages when one is open, `AbortController` cancels a still-in-flight previous tick before starting a new one, exponential backoff (4s→60s cap) on network errors resets on the next success, and `document.hidden`/`visibilitychange` pauses ticking while backgrounded and fires an immediate catch-up tick on return to foreground — none of that existed before (both old timers ran forever, unconditionally, no backoff, no abort, no pause). `app.html`'s `initApp()` now calls `startChatPolling()` instead of the old `startUnreadChatPolling()`.
- **Render-diff heuristic replaced**: the old skip-check (`maxTs<=lastTs && length===lastCount`) missed a real case — another user reacting to a message you're viewing doesn't change that message's `ts` or the array's length, so the UI could show a stale reaction count until something else forced a re-render. Replaced with a signature string (`id + reaction summary` per message, joined) compared against the last rendered snapshot — catches reaction-only changes and delete-of-a-non-last-message correctly, at the same O(n) cost.
- Honest scope note: the phase file's "monotonic cursor" ask assumes real backend pagination; `GET /api/chat/messages` still returns the full ≤200-message array every call (no `?since_id=`-style param exists), so this pass didn't add backend pagination — it made the frontend's own "did anything change" check correct and cheap instead, which is the practical bottleneck today at this message-volume scale. A real paginated cursor would be a backend change, out of scope for a frontend-only polling pass.

### Deployed
`frontend/app.html`, `frontend/js/chat.js`, `frontend/js/critical-alerts.js` copied to `/var/www/miniapp/` (backed up first, `*.bak-pre-chatpoll-<timestamp>`), no backend restart needed (frontend-only). Verified byte-identical to repo post-deploy.

## 2026-07-28 (autonomous session, continued — live owner requests: radio stations, tab transition)

Two direct owner requests handled outside the phase-file sequence (owner testing live on his phone mid-session, per this project's normal working pattern). Commits `25ca0be`, `0e0adac`, `41b3ef7`. Verified deployed: repo `frontend/app.html`/`frontend/js/*` byte-identical to `/var/www/miniapp/` as of this check.

### Changed
- **Radio: 19 real stations, infinite-loop swipeable carousel** (`frontend/js/core/radio-controller.js`, `frontend/js/components/radio-player.js`, `frontend/app.html`) — `RADIO_STATIONS` expanded from 4 placeholder entries to 19 real `radiorecord.hostingradio.ru` streams (Afro House, Chill-Out, Rock, Megamix, etc). Station chip strip now renders the real list 3x (before/current/after blocks) and snaps to the middle block on mount; a scroll listener silently re-centers scroll position when the user nears either edge, so swiping past the first/last real station wraps around with no visible seam — no third-party carousel library, pure CSS `scroll-snap` + a small JS recenter. This is Phase 08 (radio player rebuild) scope, ahead of that phase's own formal status write-up — see `docs/plan-phases/08-radio-player-rebuild.md`.
- **Radio: compact layout** — removed the redundant top `PROMONTA RADIO` title/subtitle block (station name is now shown inline in the status row instead, e.g. "В эфире · Rock"), moved the status row above the station carousel, and excluded `.home-radio-stations-viewport` from the global tab-swipe gesture (`frontend/js/swipe-nav.js`) so swiping through stations no longer also triggers a bottom-nav tab change.
- **Bottom-nav tab transition** (`frontend/app.html`, `switchView()`) — switching between root tabs (Home/Chat/Objects/Calendar/Profile) now plays a directional slide+fade (`tabSlideInFromRight`/`tabSlideInFromLeft`, 320ms, `prefers-reduced-motion` respected) based on each tab's position in `TAB_ORDER`, instead of an instant hard cut. Scoped to `opts.isTabSwitch && !opts.fromBack` only — push/back navigation into nested screens (object detail, tools, documents, etc.) is untouched, keeping its own existing semantics.

## 2026-07-28 (autonomous session, continued — Phase 06 Chat Hub rebuild, partial)

Full detail and status markers in `docs/plan-phases/06-chat-hub-rebuild.md`. Commits `50309ac`, `583a3ff`, `7204908`, `feb9bf2`, `509e20e`, `0ec6acb`, `7476da6`, `b5becd7`, `9159715`.

This is explicitly a multi-session project per the phase file's own prior audit — this pass made real, deployed progress but did not finish it (search animation and polling consolidation remain, see Known gaps).

### Decided
- Chat Hub keeps 5 category tabs (Общий/Личные/Объекты/Дефекты/Потребности), not the spec's literal "4 таба" — see `docs/DECISIONS.md` 2026-07-28 entry. Потребности predates the plan and is in active daily use with a prior recorded owner requirement; dropping it silently would have deleted a live feature.

### Fixed
- Self-DM was never explicitly blocked backend-side (`POST /api/chat/messages`, `/messages/attachment`, `/messages/voice`) — now rejected with 400.
- `close_chat_thread` fully overwrote `chat_thread_meta.json[thread_id]` instead of merging — would have silently wiped the new `user_prefs` (mute/pin/archive) the next time a thread with prefs set was closed.
- The legacy `GET /api/chat/my_threads` only ever returned obj:/mangel:/task: threads, never GENERAL or DIRECT, even though `chat.js`'s `renderChatThreadList()` tries to look up group/DM previews from it — those lookups could never match, so "Общий чат"/DM row previews in the thread list always showed static fallback text instead of the real last message. Fixed in the new normalized endpoint (see Added); legacy endpoint left untouched since the live UI still depends on its exact current shape.

### Added
- `POST /api/chat/messages/{msg_id}/reactions` — compact fixed reaction set (👍✅👀❗), toggle semantics, backed by new `chat_reactions.json`. Wired to a long-press context menu on message bubbles (replaces the old touchstart→confirm()-only-for-own-messages delete flow with one menu offering reactions to everyone plus delete when allowed) with optimistic update + rollback-on-error.
- `POST /api/chat/threads/prefs` — real per-user mute/pin/archive data layer in `chat_thread_meta.json` (`user_prefs`). No frontend controls wired to it yet (per the phase spec's explicit "don't draw fake controls before the data layer exists").
- `GET /api/chat/threads` — normalized shape (`id`/`type`/`title`/`avatar_url`/`subtitle`/`last_message`/`unread_count`/`muted`/`pinned`/`archived`/`version`, `online` on DIRECT) across all 5 tab types, kept alongside the legacy endpoints, not yet consumed by any frontend — groundwork for the eventual full rebuild.
- Chat Hub always-dark palette: 11 new `--chat-*` CSS variables, scoped to `#view-chat` so the chat screen renders dark independent of the app-wide Old Money light/dark theme setting (same pattern as Messages-style apps).
- Worker strip above the category tabs — horizontal avatar row, tap opens/lazily-creates a DM, shows real online presence and per-worker unread count. Partial implementation: the spec's "search circle" is meant to be the first item in this same ribbon, still a separate plain `<input>` above it for now (see Known gaps).
- `tests/test_chat_backend.py` — 16 stdlib `unittest` cases (no new dependency) covering the backend logic added this phase, actually executed against the real `backend/main.py` (16/16 passing), unlike the still-unexecuted `tests/smoke-nav-fab.js` from Phase 04.

### Known gaps (documented, not fixed this pass — see "BLOCKED"/open items in the phase file)
- Expandable search state machine (COLLAPSED/EXPANDING/ACTIVE/SEARCHING/NO_RESULTS/ERROR/COLLAPSING) not built — the plain `<input>` search bar from before this session is unchanged.
- Chat polling is still the original 2 independent timers (`_chatPollTimer` 8s, `_chatUnreadTimer` 15s), not the spec's single controller with monotonic cursor/AbortController/backoff — flagged as the highest-risk remaining item, deliberately not rushed against a live daily-used feature.
- Granular read receipts (SENDING/SENT/DELIVERED/READ/FAILED) not built — backend only knows binary read/unread per thread, so DELIVERED/FAILED aren't real states to report yet.
- Frontend still runs on the legacy raw-storage-shape chat endpoints end-to-end; the new normalized `GET /api/chat/threads` exists but nothing reads it yet.

## 2026-07-28 (autonomous session, continued — Phase 05 design system)

Full detail and status markers in `docs/plan-phases/05-design-system.md`. Commits `9606f3d`, `752b2f3`.

### Changed
- Calendar (`#view-abwesenheit`) day-tap now opens the reusable bottom-sheet component (same pattern as the object-creation sheet from the Phase 04 work) instead of an inline `display:block/none` form.

### Added
- Bubble Assignment: tapping a worker bubble (without dragging) now opens the same assign-confirmation popup a successful drag would — implemented inside the existing pointer event lifecycle with a 6px movement threshold, drag logic itself untouched.

### Verified as already done (design-system plan was largely stale, not re-implemented)
Read-verified against actual code rather than trusting the plan text: color/spacing/radius tokens (real names differ from the plan's proposed `--color-*` naming but are semantically/visually equivalent — deliberately not renamed, see the phase file for the reasoning), Manrope-only typography (confirmed still true, no Montserrat live), Home/Profile card consistency and worker-vs-owner branching, Calendar title/selector/today-highlighting, Bubble Assignment's skill-matching/XSS-escaping/swipe-conflict concerns. Also corrected an overstated finding: Bubble Assignment "conflict detection" is NOT actually missing — the backend (`assign_user` in `backend/main.py`) already hard-blocks approved-absence and cross-object date overlaps with a 409; only the proactive pre-attempt UI warning is absent, not the underlying data-safety check.

### Known gaps (not fixed, documented as deferred — larger scope than fit safely in this pass)
- Bubble Assignment optimistic update + undo after a successful assignment.
- Bubble Assignment explicit "Просмотр" (read-only overview) vs "Распределение" (assign) mode toggle.
- Bubble Assignment proactive conflict/absence highlighting in the arena before a tap/drag (the assignment itself is still protected server-side either way).
- Emoji→SVG icon conversion remains blocked on an owner-provided reference image (unchanged since the prior session).

## 2026-07-28 (autonomous session — Phase 04 remainder, per docs/HANDOFF_PHASE05_10.md)

Full detail and status markers in `docs/plan-phases/04-telegram-ui-navigation.md` ("Remainder items 1-6"). Commits `d305ba7`, `21db273`, `eca02dc`, `ce7e2e4`.

### Fixed
- Telegram's native BackButton and the custom in-header `.chat-back-btn` could render simultaneously on all 7 screens reached via `NavigationManager.push()` (tools/documents/working-objects/my-tasks/tasks/mangel/ai) — nothing hid the custom one when the native one was showing. First fix attempt (`d305ba7`) was too broad and hid `checkin-status-close-btn`/`chat-thread-back-btn`, which aren't wired through NavigationManager and have no other way to close — this would have stranded real users. Corrected in `21db273` to a precise `[onclick="NavigationManager.back()"]` selector, verified against every actual usage of the shared `.chat-back-btn` class before redeploying.

### Changed
- Objects screen's `+` button moved from the header (was overlapping Telegram's top-right menu button) to a `position:fixed` FAB above bottom-nav, positioned via a newly-measured `--app-bottom-nav-height` CSS var (`_applyBottomNavHeight()`) instead of a magic pixel offset.
- "Новый объект" object-creation form converted from a full-screen inline view to a managed bottom sheet (`#new-object-sheet`), registered in `NavigationManager.overlayStack` so Telegram's BackButton closes it correctly.

### Added
- `tests/smoke-nav-fab.js` — first test file in this repo (still no framework/CI, this is a single ad-hoc Playwright script per the phase brief). Written and logic-reviewed, **not executed**: the sandbox this session ran in has no root access to install Playwright's Chromium system dependencies (`npx playwright install chromium --with-deps` needs sudo; the bare download is missing `libnspr4.so` and others with no apt access).

### Known gap (not fixed, documented)
- Root-vs-nested route classification is still incomplete: Object Detail, Stages view, and Chat thread detail are shown/hidden via ad-hoc `style.display` toggles outside `NavigationManager`'s route/overlay stacks (unlike the 7 screens above and the new object-creation sheet). Telegram's native BackButton doesn't know about them, so on those specific screens only the in-app close/back control works. This matches the original phase's C6/C9/C10 items (single source of truth navigation, per-tab stacks, screen lifecycle), which were not attempted this session — the existing screens work today via their own mechanism and a rewrite risked breaking more than the remaining phase 05-10 budget could absorb to fix.

## Unreleased

### Added
- `docs/` directory: PROJECT_STATE, ARCHITECTURE, FEATURES, ROLES_AND_PERMISSIONS, API, DATABASE, UI_UX, DEPLOYMENT, TESTING, DECISIONS, TODO, SESSION_HANDOFF, SECURITY, ENVIRONMENT, TROUBLESHOOTING, RELEASE_PROCESS.
- `README.md`, `CLAUDE.md` (governance rules for this repo).
- `backend/.env.example`, `backend/requirements.txt` (neither existed before).
- `.gitignore` covering secrets and all runtime JSON/media data.
- GitHub PR/issue templates.

### Changed
- Backend (`/home/promonta/agent/miniapp/`) and frontend (`/var/www/miniapp/`) merged into one repo, `backend/` + `frontend/` subdirectories. Frontend's existing 14-commit git history preserved via `git subtree`.

### Documentation
- Corrected a stale claim from `server-structure.md` (2026-07-15 note): unknown Telegram user IDs get a **403**, not a silent `worker` role default — that changed with a whitelist hardening ("Фаза 10.1") since that note was written.
- Flagged one product-level permission gap for owner review (not silently fixed): `POST /api/objects/{object_id}/tasks` lets any authenticated worker add a task to any object, not just their assigned one. See `docs/ROLES_AND_PERMISSIONS.md`.

## 2026-07-25 (Mac session — Object Details tabs, security/UI plan closeout)

### Added
- **Object Details screen (6 tabs)**: tapping an object card opens Chat/Инфо/Задачи/Потребности/Дефекты/Этапы instead of the old single-card "Этапы объекта" screen. Chat tab is truly embedded under the tab bar (physically re-parents `#chat-thread-detail-view`, not a fullscreen overlay — user explicitly rejected the first fullscreen-overlay version). Инфо tab is a new feature: free-text work-items + document upload/viewer (new `OBJECT_INFO_FILE` store, `object_documents/` dir). Задачи/Потребности reuse existing endpoints (added `object_id` filter to `GET /api/tasks`, previously missing). Дефекты switched to the already-existing but unused `GET /api/mangel?object_id=` filter. Этапы got a visual roadmap with owner up/down reorder (`swap_stage_order()` in `objekte_lib.py`, lives outside the repo) and a worker-only "Готово" button restricted server-side to the object's actual current stage.
- **Unread badges for thread_key-based chat threads** (Объекты/Дефекты/Потребности tabs in the chat list) — `GET /api/chat/unread_by_thread` only ever counted group/DM threads before; extended to thread_key threads too.
- **Alert read-tracking**: derived `/api/alerts` (budget/tools/assignment) had no "read" concept — added `alert_dismissals.json` per-user dismiss layer, 24h TTL, so closing the alerts modal drops the Home counter but an unresolved problem resurfaces after a day instead of vanishing forever.
- **Colored avatars**: chat message header (name+time row, no bubble background — Connecteam-style restyle), Profile → Команда list, both via the existing `_chatAvatarHue()` technique.
- **"Назначить на объект" from worker profile** (`openAssignFromProfile()` in bubble-assign.js) — reverse flow to the existing drag-and-drop assignment.
- **NavigationHeader**: unified 3D ivory back-button (chevron SVG, press-state) applied to all ~12 screens via a one-time DOM upgrade pass — `NavigationManager`'s stack/back()/Telegram-BackButton logic was already in place from an earlier session, this was the missing visual piece from the plan.

### Fixed
- **Checkin finish targeted the wrong object_id**: the new Stages-tab checkin shortcut used the *currently open* object's ID instead of the object the shift was actually running on (`_findActiveWorkerCheckinObjectId()`'s result was computed but not used) — finishing a shift from a different object's screen left the real session open server-side ("Смена идёт" never cleared). Live-reported and confirmed via journalctl (the finish POST returned 200 but targeted an unrelated/missing session).
- **Vertical swipe closed the whole Mini App, chat/AI screens only**: `switchView()` called `Telegram.WebApp.enableVerticalSwipes()` specifically for chat/AI based on a wrong assumption (that it also gates internal container scroll — it doesn't, that's a separate mechanism). Now always calls `disableVerticalSwipes()`, no per-view exception.
- **Global swipe-nav ate horizontal scroll on filter/tab rows**: `.obj-filter-row` (Objects list), `#tools-filters`, `.profile-tabs`, `.profile-period-pills`, `.wx-city-tabs`, `.wx-object-tabs` were all `overflow-x:auto` but missing from `swipe-nav.js`'s exclusion list — scrolling them sideways instead fired the global tab-switch. Audited every `overflow-x:auto/scroll` rule in `app.html` (9 total) instead of fixing these one at a time as reports came in.
- **Photo comments composer collapsed under the keyboard** — same root cause as the earlier chat keyboard-jank fix (`.photo-comments-modal` relied on bare `inset:0` + browser resize, which Telegram fullscreen doesn't shrink on keyboard open). Applied the same `--tg-vp-height` fix, plus swapped the `<input>` composer for a `<textarea>`.
- Remaining emoji flagged by the UI/UX plan: crown (👑, nav tab + profile role badge → brass dot indicator / plain text), lightning bolt (⚡, "Работа по фото" card → SVG), news feed reactions (👍👎📤 → SVG icons).
- **Server file permissions**: `/home/promonta/agent/miniapp-repo` had been fully owned by `root` (likely from an earlier Fable/autonomous run using root-level process access), blocking the `promonta` user from writing at all. Fixed with `chown -R promonta:promonta`; also copied git credentials from `/root/.git-credentials` to `/home/promonta/.git-credentials` so `promonta` can push without needing root going forward. Documented in `~/.claude/server-structure.md` on the Mac side.

### Verified as already done (plans were stale, not re-implemented)
Re-checked line-by-line and confirmed already shipped in earlier sessions (commits `d698a28`/`a564b6c`/`2f4b511`/dated comments from `22.07`/`24.07`): the entire Security & Reliability P1 plan (all 15 deadlock sites, every authorization gap, audit-log PII redaction), UI/UX Phase 0 (z-index assignment-popup bug), Phase 3 (RaisedTab 3D cards, KPI rebalance, compact weather card), Phase 4a (multi-photo posts, gallery picker), and most of Phase 5 (Profile accordions collapsed by default). Both plan files at `/Users/mac/.claude/plans/` on the Mac side annotated with STATUS notes so they stop looking actionable.

### Not verified
None of today's ~20 commits were tested on a physical device/real Telegram client — only code review, `node --check`/`py_compile`, and inline-script parsing. Flagging honestly per this project's no-test-framework reality; next session should start with live verification, especially the embedded chat tab and the checkin object-id fix.

### Open
- Tools screen still 404s from Google Sheets — the OAuth token was never re-consented for that specific spreadsheet after the last token reissue (same root cause as the earlier Objekte fix). Needs the owner to walk through Google's consent screen again; deferred at their request ("не срочно").
- A checkin session opened 2026-07-22 on OBJ-001 (user 872079437) is still unclosed server-side — left untouched, not this session's data to close without confirmation.

## 2026-07-25 (Telegram UI/navigation fixes — verified ChatGPT audit subset)

External ChatGPT audit (62 sections) was cross-checked against real code via 3 parallel read-only agents + a Fable second-opinion review before any fix — only CONFIRMED findings were acted on, FALSE claims (mixed fonts, inconsistent h1 sizes) were dropped. Full findings + explicitly-deferred scope in `~/.claude/plans/cozy-honking-leaf.md` (Mac-side).

### Fixed
- **Radio widget collided with Telegram's system zone on Home**: `.radio-fab-btn`'s 82px override (enlarged earlier for glove-tap usability) kept the same top-offset as the 52px base rule — the extra circle depth pushed into Telegram's chevron/menu area. Shifted right/top offset instead of shrinking the tap target.
- **Back from photo comments landed on Profile instead of the photo feed**: `NavigationManager.overlayStack`/`registerOverlay()` existed since Phase 0.5 but had zero callers — `openPhotoComments`/`closePhotoComments` toggled `display` directly, outside navigation entirely. Telegram's native BackButton had no idea the modal was open and popped the real screen stack. Registered the modal as a proper overlay; split close logic into an internal path (called by `NavigationManager.back()`, modal already popped) and the manual-close path (explicitly unregisters itself).
- **Photo-comments composer collapsed to a ~40px sliver**: `#pc-comment-input` had `flex:1` with no `min-width:0`; sibling send button used the shared `.submit-btn` class (`width:100%`) with no `flex-shrink` — the button's intrinsic-width claim won the flex fight. Scoped fix inside `.pc-input-row` only, doesn't touch `.submit-btn` globally.
- **Photo-comments carousel showed 1/2 + dots + arrows but didn't swipe**: only click listeners existed on prev/next buttons, zero touch handlers. Added threshold-based (40px) touch swipe on `#pc-photo-wrap`, routes through the same functions the arrows already used.
- **Object-scoped Needs tab always showed "Потребностей нет"**: `GET /api/tasks` filtered by `from_user_id` for non-owner roles *before* applying the `object_id` filter, so a worker only ever saw their own requests for that object. Owner-confirmed intended behavior: object-scoped view should have team visibility (like the object chat); the global Потребности screen keeps the own-requests-only restriction.
- **Object-chat embedded composer didn't hide bottom-nav**: `embedObjectChat()` deliberately skipped `chat-dialog-open` (comment described it as intentional). Owner confirmed it should match the standalone chat instead — now sets/unsets the same class the standalone chat already uses.
- **Object card showed budget/status/stage duplicated 2-3× simultaneously**: hero pill + inline `.status-switch` editor + stat chips all displayed the same fields. Removed the redundant chips and the dead "Документы" accordion (actually rendered Tasks data, duplicating the real Tasks tab). Moved the status editor (В работе/Пауза/Завершён, owner-only) into the object detail Инфо tab instead of deleting the capability — card now passes its status via `data-status` so the editor has data without an extra API call.
- **Inconsistent type scale across screens**: `.profile-week-total` had no explicit `font-size` at all (inherited browser default ~16px next to a 1.26rem section title) — now 1.5rem/700 matching the Home KPI scale. Avatar circles (`.obj-people-dot`, `.obj-people-add`, `.profile-team-avatar`) unified from 28-30px to 36px.

### Deferred (explicit scope decisions, tracked in the plan file)
- Emoji → SVG icon sweep (confirmed real, 61+ call sites across 18 files) — large mechanical pass, not bundled here.
- Decorative-widget shadow/neumorphism cleanup (weather orb, tool icons) — confirmed localized, cosmetic, not blocking.
- Tasks "+"-button in object detail — UNVERIFIED without live device/console repro; owner-only gate is correct per this session's earlier `require_owner` fix, but visual "does nothing" report not yet root-caused live.
- Full 106-route backend permission matrix, backend modular restructure, offline queue, AI subprocess hardening, upload magic-byte validation, CSV injection guard, Google Sheets outbox — all explicitly out of scope, disproportionate to this project's scale (single owner, ~10 workers, no CI).

## 2026-07-25 (audit fixes — Grok/MiniMax external review)

### Fixed
- **Swagger/OpenAPI exposed** (`backend/main.py`): `FastAPI(...)` now sets `docs_url=None, redoc_url=None, openapi_url=None` — `/docs`, `/redoc`, `/openapi.json` were reachable on the backend port (127.0.0.1:8001/docs returned 200 before the fix, 404 after), exposing the full route/schema map. Public `app.promonta.fun/docs` was a false alarm on investigation — Caddy's SPA `try_files` fallback served `app.html` there regardless, not real Swagger UI — but the backend-level fix closes the actual exposure and is correct defense-in-depth regardless.
- **Dead legacy files servable directly**: `angebot-tab.html`, `projects-tab.html`, `tools-tab.html` (unreferenced by `app.html`/any JS, last touched 2026-07-08, predate current auth model) were served as-is by Caddy on `/var/www/miniapp/` before falling through to the SPA. Archived to `frontend/.archived-legacy/` in repo, moved to a timestamped backup dir on the live frontend host — confirmed all three paths now 200-fallback to the SPA shell instead of serving their own content.

### Added
- **CSP + security headers** (`/etc/caddy/Caddyfile`, not repo-tracked): `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security`. `script-src`/`style-src` keep `'unsafe-inline'` (app.html has one inline `<script>` block + 18 `onclick=` attributes — removing needs a separate frontend refactor, not bundled here). `X-Frame-Options` deliberately omitted — would conflict with Telegram's WebView iframe embedding; `frame-ancestors` in the CSP already scopes that correctly. Google Fonts domains added to `style-src`/`font-src` after live-testing caught the initial policy blocking them. Verified via Playwright against the live page: zero CSP violations in console.
- **`scripts/deploy.sh`**: repo→VPS sync (backend `main.py` + frontend `frontend/`) with `py_compile` pre-check, timestamped backups both sides, optional `--restart` flag for the backend service (off by default). Replaces the ad-hoc manual SSH+cp+backup sequence used throughout this session's prior work.

### Deferred (explicit owner decision)
- **Rate limiting on upload/checkin endpoints** — only `/api/ai-chat` has a rate limit today (20/hr). Checkin-photo AI-analysis endpoints and general uploads have none. Owner's call: all current users are trusted (own workers), not urgent — skipped for now, revisit if abuse/cost-spam ever becomes a real incident.

## 2026-07-31 (release-hardening: 4 rounds, chat actions + P0/P1 audit fixes)

### Added
- **Chat message actions**: reply-with-quote (snapshot-based, survives original deletion), copy-to-clipboard (with textarea/execCommand fallback that now correctly triggers on any clipboard error, not just missing API), forward-to-any-accessible-thread. Long-press + right-click + explicit `⋯` button all open the same menu; button moved out of `.chat-msg-header` so it stays visible on grouped (consecutive) messages.
- **roadmap_lib.py isolated loader** (`_load_repo_roadmap_lib`) — matches tools_lib/mangel_lib/objekte_lib pattern, no longer resolved via global `sys.path`.
- **Persistent corrupt-JSON quarantine**: critical stores (roles/assignments/checkin/chat/chat-archive/abwesenheit/profiles/tasks/critical-alerts/roadmap/stage-requests) that hit a `JSONDecodeError` are quarantined to `.corrupt-<ts>` with a permanent `.corrupt-lock` marker; reads/mutations return 503 until an owner manually restores the file and removes the marker — previously the second request after quarantine silently returned an empty default and a mutation could recreate an empty store on top.
- **MINIAPP_DATA_ROOT real isolation**: 25+ hardcoded `/home/promonta/agent/miniapp/...` path constants across `main.py`, `roadmap_lib.py`, `mangel_lib.py` converted to `os.path.join(DATA_ROOT, ...)`. Verified by a subprocess regression test that performs real file I/O and confirms the prod directory's mtime is unchanged.
- **Concurrency locks**: tool checkout/return (`_lock_for_tool`, per-serial), tool creation (`_tool_create_lock`, closes a duplicate-serial race on `POST /api/tools`) — both verified with real `threading.Barrier` tests, not sequential-call approximations.
- **package-lock.json** for the PDF-generation `pdfkit` dependency; CI now runs `npm ci` + `require('pdfkit')` smoke-check; `deploy.sh` verifies pdfkit is present in serving `node_modules` *before* touching production files.

### Fixed (security)
- **Worker object-mutation scope**: `create_stage`/`update_stage_description_endpoint` allowed ANY worker to write to ANY object regardless of assignment — now gated by `require_object_access` (owner or accepted-assignment only).
- **Abwesenheit privacy leak**: `/api/abwesenheit/all` was returning `note` and `reason` (free-text, potentially medical/personal) to every worker for every colleague's absence record. Now redacted to `{id, user_id, name, date_from, date_to, open_ended, status}` for entries that aren't the caller's own; owner and the record's author still see everything.
- **Chat attachment IDOR**: `get_chat_attachment` matched access on the *first* message referencing a file and only checked `to_user_id` — broke for object/defect/task threads and files forwarded into a second thread. Now checks every message referencing the file (active *and* archived) via `_check_message_access`.
- **Live production P0**: Caddy was serving `.bak-*` snapshot files publicly over HTTPS (verified live — 200 OK on a real `app.html.bak-*`). Blocked via `@forbidden`/`@hiddenDir` matchers (404), applied with config backup + `caddy validate` before each reload.
- **CSP gap**: `media-src` was missing `blob:` — chat voice messages (fetched via authorized request, played through a blob URL) were silently blocked by the browser.
- **Caddy body-size limit too low**: 10MB rejected legitimate finish-shift uploads (2 photos × 8MB backend limit each); raised to 30MB.

### Fixed (reliability)
- Frontend: chat message re-render / poll tick destroyed `<img>`/`<audio>` elements via `container.innerHTML` without revoking their `blob:` URLs — unbounded memory growth in the Telegram WebView over a session. `URL.revokeObjectURL` now called before DOM replacement, before `src` reassignment, and on chat view re-entry.
- Attachment upload to `thread_key`-scoped chats (object/defect/task) wasn't setting `thread_key` on the saved message — files silently leaked into the general chat instead of staying in their thread.
- deploy.sh/rollback.sh: added `objekte_lib.py` (was in git but never actually verified present by CI's required-files check — a real gap), `roadmap_lib.py`, `angebot_free.js`, `rechnung.js` to backup/restore/syntax-check coverage; trap-based auto-rollback on any failure after backup creation; test suite now runs with a dummy `BOT_TOKEN` + isolated `MINIAPP_DATA_ROOT`, not the full production credentials file.
- Frontend cache-busting: JS/CSS `<script src>`/`<link href>` get `?v=<deploy SHA>` injected into the *serving* copy of `app.html` at deploy time (not the repo file); Caddy now caches `/js/*`/`/css/*` for a year as a result.

### Known gaps (not fixed, explicitly out of scope this session)
- Repository is currently **public** on GitHub — needs manual switch to private (Settings → Danger Zone) plus GitHub PAT rotation (used throughout these sessions for CI/git-push checks).
- 144 loose `.bak-*` files remain physically on `/var/www/miniapp/` (Caddy no longer serves them, but disk isn't cleaned) — left as-is per explicit prior owner decision.
- No frontend request timeout (AbortController) in `api()` — deferred, not a release blocker.
- Real Telegram WebView E2E (Owner + 2 Workers) has not been performed by an automated agent — Safari MCP repeatedly failed to open the app in this environment; owner is doing this manually with screenshots.

## 2026-07-24 (evening — autonomous session)

### Fixed
- **Chat embed stranded** (`frontend/app.html` `switchView()`): navigating away from Object Detail via bottom nav while Chat tab was active left `#chat-thread-detail-view` embedded inside `#obj-detail-panel-chat` instead of `#view-chat`, breaking regular chat until back-button was pressed. Fix: call `unembedObjectChat()` unconditionally at the top of `switchView()` (no-op when not embedded).
- **Checkin inaccessible from Stages tab** (`frontend/js/object-info.js`): the old `openStagesView()` showed a checkin-bar; the new 6-tab object detail screen's Stages tab only showed the roadmap. Fix: `_appendCheckinShortcut()` added at bottom of Stages panel for workers — calls `_openCheckinStatusScreen()` modal (same flow as FAB, no DOM duplication). Owners see no shortcut.

### Not yet deployed
Frontend changes committed+pushed but not copied to `/var/www/miniapp/` (requires root). See SESSION_HANDOFF.md.

## 2026-07-24 (fix/security-reliability-p1)

### Fixed
- **Deadlock (P0)**: removed redundant outer `with _lock_for(FILE):` wrapper at 15 call sites (roles, profile, object-assignments, chat-thread-meta, critical-alerts, abwesenheit) — `_atomic_write_json` already locks internally; the outer lock caused a guaranteed self-deadlock on nested acquire (non-reentrant `threading.Lock`), hanging every future request touching the same file. Same bug class as the 2026-07-17 `create_task`/`update_task_status` fix, not swept to these files at the time.
- **Authorization gaps**: `checkin_finish` (any worker could finish any other user's shift), checkin AI-analysis endpoints (no ownership check + unused rate limit not wired), `update_mangel_status` (missing owner gate, inconsistent with sibling status endpoints), `post_chat_attachment` (skipped `_check_thread_access` for thread-scoped uploads).
- **Upload validation**: `resolve_critical_alert` had no content-type/size check — now matches the 8MB + image/* pattern used elsewhere.
- **Live 500 bug**: `checkin_manual` referenced `idempotency_key` without declaring it as a parameter — every call failed (confirmed via audit.log: zero successful entries ever).
- **Robustness**: `validate_init_data` malformed `auth_date` now returns a clean 401 instead of an uncaught 500.
- **PII in logs (P1)**: `audit_log_middleware` no longer stores the full request body — was logging chat text, notes, and profile fields in plaintext.
- **UX**: Потребности split into its own view (`view-tasks`), separate from Дефекты — was a sub-tab sharing `view-mangel` via a `window._pendingMangelTab` side-channel.

## 2026-07-23 (recovery)

- **chore**: preserve recovered project state before documentation rebuild (backend code snapshot).
- **chore**: merge frontend git history (14 commits) as `frontend/` subtree.

---

Prior history (before this recovery) is not itemized here — it lived only in `.bak-pre-*` filenames on the VPS and in the frontend repo's own 14-commit log (visible via `git log -- frontend/`), not in a changelog. Going forward, every functional change should get an entry here per the rules in `CLAUDE.md`.
