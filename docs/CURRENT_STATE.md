# Current State

**Last updated**: 2026-09-22 (iPhone screenshot audit session). `fix-shift-start`
(the section below) has since been **merged to `main` as PR #1** (`c28ff97`)
and deployed to production. Two more merges landed after it: PR #2
(Object Detail V2 plan doc fix, `b8e9404`) and PR #3 (Object Detail V2 step 1
internal zone shell, `8b01f23`). Current work lives on branch
`screenshot-audit-2026-09-22` (6 commits ahead of `main`'s `8b01f23`) — **not
merged, not deployed**, awaiting explicit owner approval. Full writeup:
`docs/IPHONE_SCREENSHOT_AUDIT_2026-09-22.md`. Tests on that branch:
1071 passed, 1 skipped, 0 failed.

Prior baseline was 2026-09-18. This is the single place to check first — if
it contradicts anything elsewhere in `docs/`, this file wins for "what's
true right now." Everything else in `docs/` with a date in its filename
(`*_09sep2026.md`, `*_11sep2026.md`, `*_13sep2026.md`, `HANDOFF*.md`,
`PLAN_*.md`, `EXECUTION_PLAN.md`, `MASTER_ARCHITECTURE_PLAN.md`,
`ROUND1_TASK.md`, `AUTONOMOUS_*.md`, `SESSION_HANDOFF.md`,
`PRODUCTION_CONTROL_PLAN.md`, `RELEASE_AUDIT.md`, `RELEASE_CANDIDATE_REPORT.md`,
`FOUNDATION_COMPLETION_ADDENDUM.md`) is a **historical snapshot of one
finished (or abandoned) work round** — kept for git-blame-style context, not
maintained, not a source of truth. Their content was read in full and folded
into this file and `BACKLOG.md`/`OPEN_QUESTIONS.md` on 2026-09-18; each now
carries a one-line `SUPERSEDED` pointer at the top. The "Repo / branch /
deploy" and "Scale" sections immediately below describe the `fix-shift-start`
session as it was on 2026-09-21, before the merges above — kept for
git-blame context, superseded by this paragraph for current branch/deploy
status.

## Repo / branch / deploy

- **Repo**: https://github.com/whymsiluxe/promonta-miniapp — still **PUBLIC**
  (owner has declined to make it private twice; not an oversight, a standing
  decision — see OPEN_QUESTIONS.md).
- **Branch**: `fix-shift-start`, **NOT pushed to origin, NOT merged to
  `main`** — exists only in the local repo clone as of this writing. Contains
  the Worker UX V2 work below (Этапы 0.5-8 of `UNIFIED_STEP_BY_STEP_PLAN_v1.1.md`).
  `main` last commit: `7f4a9a7`.
- **Deployed = HEAD**: NOT verified this session, and cannot be true —
  the VPS can only be running `main` or an earlier pushed branch, since
  `fix-shift-start` was never pushed. Verify `GET /api/health` `commit`
  field against `git rev-parse HEAD` before assuming any of this work is
  live; it is not, until pushed/merged/deployed.
- **CI**: green on `main` as of this writing (`gh run list`, last 3 runs all
  `success`) — but `fix-shift-start` has never run CI (never pushed), so
  none of this session's commits have been verified by CI yet.

## Stack (unchanged, still accurate)

Vanilla HTML/JS frontend (no build step, no framework) + FastAPI/Python
backend (`backend/main.py`) + flat JSON file storage under
`MINIAPP_DATA_ROOT` (no database) + Google Sheets for object/tool data via
`objekte_lib.py`/`tools_lib.py`. Full detail: [ARCHITECTURE.md](ARCHITECTURE.md).

## Scale, as of 2026-09-21 (on `fix-shift-start`, not `main`)

- Tests: **950 passed, 1 skipped, 1 pre-existing unrelated failure**
  (`test_assignment_lifecycle.py::ProductionPackageImportTests::
  test_production_layout_resolves_module_identities_correctly` — hardcodes
  an expected route count of 185, actual is 186; predates this session,
  not caused by it, not fixed here since the test's own expected-value
  assumption is what's stale, not the code).
- Routes: **186** (`backend/main.py`, measured this session).
- `backend/main.py`: **10092 lines** — still one file, no router split.
- `frontend/app.html`: **10521 lines** — still one file, most feature logic
  already lives in `frontend/js/*.js` modules, but a meaningful amount
  (styles, view containers, some inline handlers) remains inline.
- `frontend/js/home.js`: 1782 lines, `frontend/js/chat.js`: 2225 lines —
  the two largest JS modules.

## What's working (high-level, see FEATURES.md for the full trace)

App is live, in active daily use by the owner and workers. Recent rounds
(2026-09-13 through 2026-09-18) added, on top of the existing worker
checkin/chat/tasks/defects/tools/abwesenheit feature set:

- **DailyPlan system**: publish/accept/amendment flow, per-worker
  acknowledgment, carryover tracking, blocker reporting, contract-deadline
  risk coloring (RED/GREEN via `predicted_finish_date` vs
  `contract_finish_date`), tomorrow-preview tab (accept a plan a day early
  without it affecting today's shift-start linkage), and two owner alerts —
  an 18:00 "tomorrow's plan not published yet" reminder and a 06:30
  "today's plan is overdue" alert (`backend/daily_plan_cutoff_check.py`,
  two systemd timers). Starting a shift without a plan stays freely allowed
  either way — this is a lagging-indicator alert, not a start-time gate.
- **Shift Flow**: all entry points (Home CTA, bottom FAB, Object Detail,
  DailyPlan) converge on one shared object-picker → stage-picker → start
  flow; the FAB now opens the same real `#active-shift-panel` (timer/GPS/
  pause/quick-actions) as Object Detail instead of a separate simplified
  modal. GPS status shows the real saved accuracy in meters instead of a
  boolean "geolocation API exists" check.
- **Object Info**: budget dashboard, task Kanban (To Do/In Progress/Done),
  document gallery with object filters, team drag-assignment.
- **Calendar (Abwesenheit)**: week + month views (year view + drag-to-move
  deliberately not built — needs its own design pass, see BACKLOG.md),
  `PATCH /api/abwesenheit/{id}` for moving dates.
- **Offline resilience**: outbox pattern for checkin/finish with a
  dead-letter state after 5 failed attempts (`shared.js`
  `appOutboxRecordFailure`), `api()` has request timeout + safe-read
  retry.
- **Contract ingestion pipeline exists but is deliberately OFF**:
  `backend/contract_ingest.py`/`scripts/plan_sync.py` (Drive polling → AI
  fact extraction → draft DailyPlan → owner approve/reject) is fully built
  and tested but gated behind `CONTRACTS_DRIVE_FOLDER_ID`, unset in
  production. Owner has explicitly said not to enable it yet (2026-09-18) —
  this is a live decision, not an oversight; see BACKLOG.md.

## Worker UX V2 (2026-09-21 session, `fix-shift-start` branch)

Following `UNIFIED_STEP_BY_STEP_PLAN_v1.1.md`. Status per stage:

- **Этап 0.5 Hardening**: done. Fixed a real active-shift timer desync gap
  (checkin.js free-runs client-side with no resync on tab/app visibility
  return); Sheets formula sanitization and legacy abwesenheit-id migration
  were already implemented pre-session, only verified.
- **Этапы 1-3 (Shift State/Contextual Start/Safe Finish)**: were already
  closed before this session (see `FACT_CHECK_REPORT.md` in the Codex
  outputs dir) — one real gap found and fixed (NavigationManager
  registration for object/stage pickers).
- **Этап 4 (navigation)**: done. Worker bottom-nav: 5 tabs
  (Лента/Главная/Чат/Объекты/Профиль) -> 4 (Сегодня/Объекты/Чат/Ещё). Owner
  nav untouched. New `view-more` utility hub re-links to existing
  views/sub-tabs, no duplicated screens. `data-view="home"` id kept, only
  its label changed to "Сегодня" — NavigationManager's root fallback
  needed zero changes.
- **Этап 5 ("Сегодня" operational panel)**: done. DailyPlan compact preview
  card (reads `window._todayPlanState`, opens the existing canonical
  overlay — does not duplicate today-plan.js's fetch/polling/offline-cache/
  acceptance logic) + a unified Problems card (aggregates existing
  `/api/alerts` + `/api/tasks` + `/api/mangel/counts`, ranked
  critical>important>needs>defects, no new store) replacing 3 separate
  legacy tiles. Persistent DailyPlan bar suppressed only while on Home
  (compact card already shows the same status there).
- **Этап 6 (contextual quick actions)**: done. New
  `frontend/js/worker-quick-actions.js`: Фото/Потребность/Дефект/Чат, with
  context resolution in the plan's exact priority order (active shift ->
  Object Detail -> single eligible assignment -> picker). Every action
  reuses an existing form/endpoint; `_uploadFeedPhoto()` gained an optional
  `objectId` param (backend already accepted the field, client never sent
  it before).
- **Этап 7 (object-detail 4 zones)**: **docs only, no code**. A mapping
  audit found the plan's own premise wrong before any refactor started —
  there are 3 tabs today (Чат/Инфо/План работ), not 8; what looked like "8
  tabs" is 10+ sections stacked in one "Инфо" tab, with owner/worker
  rendering interleaved in the same functions (not two render trees), a
  chat panel that physically moves DOM nodes between parents (2
  already-documented subtle bugs in its history), and near-zero test
  coverage on ~3000 lines of interlinked code. See
  `docs/OBJECT_DETAIL_V2.md` for the exact target section->zone mapping
  and a risk-ordered migration plan for whoever picks this up next.
- **Этап 8 (Finish Wizard simplification)**: **done**. Removed the
  standalone blocking "Геолокация" step (AUTO-CAPTURE PRINCIPLE — capture
  now starts in the background on wizard open, status folds into the
  existing Сводка/review screen with inline retry). Merged summary+plan-fact
  into one screen and extra-works+needs/defects+tomorrow-prep into another
  ("Проблемы и завтра"), each sub-block keeping its original markup/
  validation, only the screen containers and single next-button handler
  merged. Wizard is now exactly 4 screens (Фото/Что сделано-план-факт/
  Проблемы и завтра/Сводка) regardless of whether a DailyPlan exists —
  matches the plan's 3-4 screen target, down from 6/8.
- **Этап 9 (object timeline direction)**: docs only, as the plan specifies.
  See `docs/OBJECT_TIMELINE_DIRECTION.md` — documents the 8 existing
  `_append_object_history` event kinds and a real gap found (checkin
  start/pause, task creation, daily-plan create/accept don't log to object
  history at all, while checkin finish and defect creation do).
- **Этап 10 (this update)**: in progress — see this section plus
  BACKLOG.md/OPEN_QUESTIONS.md updates alongside it.

Not done: manual iPhone Telegram checklist (back button, active-nav
highlighting, safe-area, keyboard behavior on real hardware) — cannot be
automated from this environment, explicitly flagged, not silently skipped.
Feature freeze has NOT been declared — Этап 7 code and the remaining Finish
Wizard screen merges are still open.

## Known blockers / standing decisions (not bugs — explicit choices)

- **Repo stays public** — owner declined private twice. Do not silently
  change this.
- **GitHub PAT not rotated** — discussed, no go-ahead yet.
- **No staging environment** — production-only, by scale not by oversight.
- **No live automated Telegram E2E** — covered by unit/contract tests +
  manual owner verification via screenshots, not a scripted WebView flow.

## Technical debt (see BACKLOG.md for the actionable list)

- `backend/main.py` and `frontend/app.html`/`home.js`/`chat.js` remain
  monolithic — no domain/router split has started.
- Flat JSON storage, no database — fine at current scale (see DATABASE.md),
  revisit if concurrency/reporting needs grow.
- No CRM layer (clients/leads/deals) — out of scope so far, P3 idea only.

## 2026-09-27 Live load-failure bugfix + profile UX (merged to `main` as PR #20)

Fixed real owner-reported outages: `GET /api/objects` 500 on any malformed
assignment record (shared root cause behind Objects, Documents gallery, and
Start Shift's object picker all failing together), `GET /api/workers` 500 on
any malformed profile entry (independent cause), Documents KeyError crashes
on legacy records missing `file`/`id`, the critical-alert popup's
same-poll-response duplicate-id gap, and a real swipe-gesture bug that made
bottom sheets (skills-add sheet included) look like they closed from a stray
tap/scroll. Also redesigns the skills selector (no more separate "frequently
used" block, tri-state section select-all, per-device usage-based ordering
frozen per open) and replaces free-text clothing-size inputs with native
pickers. See `docs/CHANGELOG.md` (27.09 entry) for full detail. Full suite at
merge time: `1268 passed, 1 skipped`, plus 1 pre-existing unrelated
`rsync`-tooling failure. Not deployed.

## 2026-09-27 Core workforce shadow integration (branch `claude/admiring-fermi-wrjyrm`, rebased onto the above)

Implements the real HTTP transport in `backend/core/grandmont_core_client.py`
(previously an inert seam, disabled by default) against the authoritative
Grandmont Core API contract, plus a new `backend/core/workforce_shadow.py`
wired as best-effort shadow reads/writes into `GET /api/my-assignments` and
`POST /api/abwesenheit`. `CORE_INTEGRATION_ENABLED` stays unset — no
production behavior change, nothing deployed. Scope: worker identity
resolution (Telegram id -> Core Worker UUID), worker-facing assignment
shadow reads, worker-created absence shadow create. DailyPlan, checkin,
execution, and CRM are untouched. Originally branched off `main`@`fd30a52`,
merged forward onto `main`@`5aa8b3f` (the live-load-failure bugfix above) to
pick up its resilience fixes to the same files (`backend/main.py`,
`backend/routes/objects.py`) before this PR merges. See `docs/DECISIONS.md`
(27.09 entry) and `docs/CHANGELOG.md` (27.09 entry) for full detail.

## Where to look next

- Active/actionable work: [BACKLOG.md](BACKLOG.md).
- Things genuinely waiting on an owner decision: [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).
- Chronological detail of every change: [CHANGELOG.md](CHANGELOG.md).
- Feature-by-feature trace of what's actually wired end-to-end:
  [FEATURES.md](FEATURES.md) (last verified 2026-09-13 per its own header —
  worth a re-pass given how much has shipped since, not done in this cleanup).
