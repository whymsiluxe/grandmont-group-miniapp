# Decisions

Architectural decision log. New decisions get a new entry; superseded ones are marked, never rewritten.

---

**Date**: 2026-07-23
**Status**: Accepted
**Decision**: Combine the backend (`/home/promonta/agent/miniapp/`) and frontend (`/var/www/miniapp/`, which already had its own 14-commit git history) into a single repo, `miniapp-repo`, with `backend/` and `frontend/` subdirectories, built directly on the VPS.
**Context**: A Claude Code session was lost mid-work. Investigation found the two halves of the app lived in different directories on the VPS, only the frontend had version control, and a stale local copy existed on the developer's Mac (dated 8-12 July, while the VPS frontend had commits through 22-23 July) that would have caused data loss if used as the basis for recovery.
**Problem**: No single source of truth existed; documentation didn't reflect the actual, more-advanced state of the code.
**Options considered**: (1) two separate repos (frontend/backend), (2) work from the stale Mac copy and reconcile later, (3) one combined repo built from the live VPS state.
**Chosen**: (3).
**Why**: A single repo with one README/doc-set is easier to keep in sync per the new documentation-governance rules (see `CLAUDE.md`), and building directly from the VPS avoids reconciling a diverged, stale local copy. `git subtree add` was used (not `git filter-repo`, not installed) to bring the frontend's existing history in under a `frontend/` prefix without rewriting it.
**Consequences**: The old frontend repo's remote (a local filesystem path, `/var/www/miniapp`) was removed after the subtree merge — GitHub is now the intended remote. The Mac-side stale copy at `~/Projects/promonta/miniapp/frontend/` was left untouched (not deleted) since it wasn't authorized for destructive cleanup in this pass — see TODO.md for reconciling or removing it.
**Risks**: If anyone continues editing `/var/www/miniapp` or `/home/promonta/agent/miniapp` directly on the VPS without syncing back to this repo, the repo will drift from production again — the exact problem this recovery fixes. The governance rules in `CLAUDE.md` exist specifically to prevent that regression.
**Affected files**: entire repo structure.

---

**Date**: 2026-07-23
**Status**: Accepted
**Decision**: Runtime JSON data stores (worker profiles, chat messages, GPS check-in logs, photos, generated PDFs) are excluded from git entirely via `.gitignore`, not just from public visibility.
**Context**: These files contain employee personal data (names, GPS locations, photos, birthdays, clothing sizes) and business documents (client quotes/invoices). The task's own safety rules prohibit committing "real personal data of employees."
**Problem**: A private repo still isn't an appropriate place for this data — it multiplies where sensitive data lives and creates a stale, unsynced copy the moment someone's shift GPS log updates.
**Chosen**: Never commit runtime data. Backend code only. Data stays on the VPS, protected by the existing `backup.sh` daily-tarball mechanism (see DATABASE.md), not by git.
**Consequences**: `docs/FEATURES.md` status claims can't be verified by reading committed fixtures — verification requires either live access to the VPS or a manually-sanitized fixture set, which doesn't exist yet (see TODO.md).
**Affected files**: `.gitignore`.

---

**Date**: 2026-07-28
**Status**: Accepted
**Decision**: Chat Hub keeps 5 category tabs (Общий/Личные/Объекты/Дефекты/Потребности), not the 4 the Phase 06 spec (ТЗ3) literally describes.
**Context**: `docs/plan-phases/06-chat-hub-rebuild.md`'s code audit found a 5th tab, "Потребности" (`task:ID` threads), already live in production and in active daily use — it predates this plan and isn't a Phase 06 addition. The spec text says "4 таба" and lists only Общий/Личные/Объекты/Дефекты.
**Problem**: Silently dropping the tab during the rebuild would delete team members' access to an existing, used feature (task-request chat threads) with no owner sign-off; silently keeping it would mean diverging from a written spec without recording why.
**Options considered**: (1) drop Потребности to match the spec literally, (2) keep it and treat the spec's "4 таба" as describing the four *new* dark-theme tab types being introduced, not a hard cap, (3) block the whole Phase 06 rebuild pending an explicit owner answer.
**Chosen**: (2) — keep 5 tabs.
**Why**: `app.html`'s Object Info screen has an explicit prior comment recording an owner requirement to keep Потребности as its own object-scoped surface ("Потребности остаётся отдельным object-scoped табом по явному требованию владельца"), which is direct evidence the owner treats task-requests as a first-class, separate concern from defects — not something to fold away. Removing a live, working, explicitly-requested feature to satisfy a tab-count in a planning doc is a worse failure mode than a documented, reversible deviation from that doc. Blocking the entire rebuild on this alone (option 3) wasn't warranted — it's a low-stakes, easily-revisited call, not one that needed to stall a multi-session project.
**Consequences**: All Chat Hub rebuild work (worker strip, tabs component, search-per-tab, empty states) targets 5 tabs. If the owner later says they actually meant 4 and wants Потребности folded into Объекты or removed from Chat Hub entirely, that's a small, isolated follow-up, not a rearchitecture.
**Affected files**: `frontend/app.html` (chat category tabs), `frontend/js/chat.js`, `docs/plan-phases/06-chat-hub-rebuild.md`.

---

*(No earlier decisions are recorded — prior sessions did not maintain this log. Everything before 2026-07-23 is undocumented architectural history; where it matters, it's referenced inline in other docs from session memory rather than reconstructed here as a formal decision.)*

---

**Date**: 2026-08-04
**Status**: Accepted
**Decision**: Closed needs (`Потребности`, `tasks.json`) are now RETAINED in the working JSON with `status:закрыто`+`closed_at` instead of being deleted on close.
**Context**: Раунд 3 задача 5.2 requires the Потребности screen to show a "Выполнены N" counter and a "Выполненные" filter/archive. The prior behaviour archived the closed task to a Google Sheet and then removed it from `tasks.json`, so the frontend had nothing to count or list for completed items.
**Options considered**: (1) fetch completed items from Google Sheets on the frontend; (2) keep closed tasks in JSON, filter to "Активные" by default in the UI.
**Chosen**: (2).
**Why**: The frontend has no Sheets access path and Sheets is explicitly a view-only mirror, not an app data source. Keeping closed tasks in JSON is a one-line retention change; the UI already defaults to the "Активные" filter so completed items don't clutter the main view. Sheets archive still happens best-effort, but only on the first close (`prev_status != 'закрыто'`) to avoid duplicate rows on re-close.
**Consequences**: `tasks.json` grows over time with closed items (acceptable at current scale — same flat-JSON tradeoff documented in DATABASE.md). Any consumer of `GET /api/tasks` that must exclude completed items filters `status != 'закрыто'` (Dashboard badge already does).
**Affected files**: `backend/main.py` (`update_task_status`), `frontend/js/tasks.js`, `frontend/js/object-info.js`, `tests/test_needs_workflow.py`.

---

**Date**: 2026-09-22
**Status**: Accepted
**Decision**: Stage picker ("Какой этап сегодня?") now auto-selects and starts the shift immediately when exactly one stage exists on the object, instead of always prompting.
**Context**: `_openStagePickerThenStart()` was written on 28.07 with an explicit owner request to show the picker ALWAYS — including with only one stage — so a worker could also add the *first* stage from a clean object. Re-reviewing the same flow during the 22.09 iPhone screenshot audit, the owner explicitly reversed that for the single-stage case, citing the app's own "Worker UX V2 doesn't ask what context already knows" principle: one existing stage is unambiguous, so asking is a pointless extra screen before every shift start.
**Options considered**: (1) keep always-prompt behaviour (28.07 status quo), (2) auto-select only for exactly one stage, still prompting for 0 (need the add-first-stage UI) and 2+ (real ambiguity), (3) remove the picker's prompting entirely and always take the most-recently-used stage.
**Chosen**: (2).
**Why**: This isn't a reversal for its own sake — it's a narrower, correct rule replacing an overly broad one. The 28.07 requirement's actual goal (let workers add a first stage from an empty object) is fully preserved by still showing the picker at 0 stages. Option 3 would silently guess in the 2+ case, which is exactly the "auto-select when ambiguous" failure mode the owner explicitly warned against in the same message.
**Consequences**: One fewer screen before shift start for every object that has settled into a single active stage (the common case once a project is underway). Objects with 0 or 2+ stages are unaffected.
**Affected files**: `frontend/js/worker-checkin-fab.js` (`_openStagePickerThenStart`), `tests/test_checkin_frontend_contract.py`.

---

**Date**: 2026-09-22
**Status**: Accepted (interim — full resolution deferred to Object Detail V2 step 2/3)
**Decision**: `renderObjectStagesTab()` keeps its single hardcoded DOM mount (`#obj-detail-panel-stages`) for now; the new `#obj-detail-panel-work` container added in Object Detail V2 step 1 stays inert/unused rather than becoming a second live mount for the same stage markup.
**Context**: Reviewing the approved Object Detail V2 migration plan (`docs/OBJECT_DETAIL_V2_IMPLEMENTATION_PLAN.md`), the owner found a real sequencing gap: deleting the legacy `#stages-view` (an earlier step in that plan) does **not**, by itself, resolve a conflict between `#obj-detail-panel-stages` (the live 3-tab production panel) and `#obj-detail-panel-work` (the new 4-zone shell's placeholder) once "Работа" is ever activated — both would want to render the same `#obj-stages-roadmap`/`#obj-stages-tab-add-trigger` markup, and `renderObjectStagesTab()` can only target one `getElementById()` call.
**Options considered**: (1) parameterize `renderObjectStagesTab()` and every ID it creates to accept a target panel, so both `-stages` and `-work` could render the same content into either mount; (2) do nothing structural now, just guard the invariant with a regression test, and decide the real canonical mount (rename `-stages` to `-work`, or point `-work`'s future activation at the existing `-stages` panel) at the actual cutover step; (3) delete `#obj-detail-panel-work` now since nothing renders into it yet.
**Chosen**: (2).
**Why**: The owner was explicit: parameterizing `renderObjectStagesTab()` now would "резко увеличить площадь рефакторинга именно в рабочем stage-flow" for a change that has no user-visible effect yet (the 4-zone tab bar isn't live). Option 3 would just delete work that step 1 already completed and re-add it later for no benefit. A regression test enforcing "exactly one stage DOM mount exists" costs nothing today and fails loudly the moment a future step tries to wire `-work` up without first making the real mount decision — which is exactly the failure mode this decision exists to prevent.
**Consequences**: Object Detail V2 step 2/3 (whenever "Работа" is actually activated in the visible tab bar) MUST make the canonical-mount decision before that step ships — either rename `obj-detail-panel-stages` to `obj-detail-panel-work` (simplest, since only 2 references exist in the whole codebase) or have the future zone-routing layer point `work` at the existing `-stages` panel id. Until then, `#obj-detail-panel-work` remains a harmless unused `display:none` div.
**Affected files**: `frontend/js/object-info.js` (`renderObjectStagesTab`, unchanged), `tests/test_object_detail_v2_zone_shell.py` (`test_stage_dom_ids_are_never_duplicated_across_panels`), `docs/OBJECT_DETAIL_V2_IMPLEMENTATION_PLAN.md`.

---

**Date**: 2026-09-26
**Status**: Accepted
**Decision**: Grandmont Group rebrand of code/display/storage/env names uses brand-neutral JS identifiers (`appConfirm`, `appOutbox*`) and keeps a one-time read-fallback for every renamed persisted key and env var.
**Context**: Brand changed Promonta -> Grandmont Group. Several `promonta*` names are persisted on user devices (sessionStorage session token, IndexedDB offline outbox holding unsent shift evidence, today-plan cache, object-order preference) or set in deployed service config (`PROMONTA_*` env vars). A plain rename would log out every open session and could orphan unsent check-in/finish records on deploy.
**Options considered**: (1) plain rename everywhere; (2) keep old persisted names, rename only code; (3) rename + one-time legacy fallback/migration.
**Chosen**: (3). JS function names are brand-neutral (`app*`) because they carry no persisted state and shouldn't need touching on any future rename; persisted names use the `grandmont-group`/`grandmont_group` slug.
**Why**: (1) causes a mass logout and possible evidence loss; (2) leaves the old brand in live storage indefinitely. (3) costs a few small, test-covered fallbacks that can be removed after one release cycle.
**Consequences**: Legacy fallbacks (`LEGACY_SESSION_TOKEN_KEY`, `LEGACY_APP_OUTBOX_DB`, `_TP_LEGACY_DB_NAME`, `LEGACY_ORDER_KEY`, `main._env_compat` legacy names, runner `PROMONTA_*` fallbacks) should be removed in a later cleanup once no pre-rebrand client/config can remain. Domain, VPS user/paths, repo name are NOT part of this decision.
**Affected files**: `frontend/js/shared.js`, `frontend/js/today-plan.js`, `frontend/js/objects.js`, `backend/main.py`, `scripts/autonomous_codex_runner.sh`, `tests/test_rebrand_storage_compat.py`, `tests/test_data_root_isolation.py`.

---

**Date**: 2026-09-27
**Status**: Accepted
**Decision**: Implemented the real HTTP transport in the existing `backend/core/grandmont_core_client.py` seam and added a thin `backend/core/workforce_shadow.py` orchestration module, wired as best-effort, exception-swallowing side effects into two existing worker-facing routes (`GET /api/my-assignments`, `POST /api/abwesenheit`). No new client, no new routes, no change to either route's response shape.
**Context**: Task scope was Mini App <-> Core workforce shadow integration for worker identity, assignments, and absences only (explicitly not DailyPlan/checkin/execution/CRM). The seam module already existed as an inert adapter (`GrandmontCoreClient.command()`), disabled by default, with an explicit design principle of not inventing Core's contract. The authoritative Core HTTP contract (base URL via `CORE_BASE_URL`, `X-Core-Service-Token` auth, `GET /worker-identity/resolve`, `GET /assignments`, `GET /absences`, `POST /commands/absence/create`, and the 4xx/timeout/5xx -> REJECTED/TIMEOUT/UNAVAILABLE error mapping) was supplied directly by the owner/task, not invented.
**Options considered**: (1) put the shadow orchestration logic directly inline in `main.py`/`routes/objects.py`; (2) add a second Core client; (3) a small dedicated `workforce_shadow.py` module reusing the one existing `GrandmontCoreClient`, injected into route handlers via the existing dependency-injection pattern (`ObjectsRouteDeps`) or a direct best-effort call wrapped in `try/except`.
**Chosen**: (3).
**Why**: (1) would scatter Core-specific logging/error-swallowing logic across unrelated route handlers; (2) was explicitly disallowed by the task ("do not create a second client"). A single small orchestration module keeps the "never raises to the caller, no-ops when disabled" invariant in one place, testable in isolation from the routes that call it.
**Consequences**: `CoreIntegrationConfig.validate()` now also requires a non-blank `CORE_SERVICE_CREDENTIAL` when enabled (previously only `CORE_BASE_URL`/`timeout_seconds` were checked) — existing seam tests were updated to supply a credential in their enabled-mode fixtures, not weakened. `CoreIntegrationError` gained an optional `status_code` field (used only to disambiguate a 404 identity-bridge response as a controlled "unmapped worker" state from a generic REJECTED, without adding a new `CoreErrorKind`). Assignment writes (`POST /commands/assignments/*`) and absence approve/reject/cancel were deliberately not implemented — out of this task's scope per the owner's explicit instruction not to expand it. `CORE_INTEGRATION_ENABLED` stays unset (disabled) in production; this PR does not flip it.
**Affected files**: `backend/core/grandmont_core_client.py`, `backend/core/workforce_shadow.py` (new), `backend/main.py` (`create_abwesenheit`, import wiring), `backend/routes/objects.py` (`my_assignments`, `ObjectsRouteDeps.shadow_compare_assignments`), `tests/test_core_integration_seam.py`, `tests/test_core_http_transport.py` (new), `tests/test_core_workforce_shadow.py` (new), `requirements-test.txt`, `docs/ENVIRONMENT.md`.

---

**Date**: 2026-10-04
**Status**: Accepted
**Decision**: Migrate the Mini App's Telegram bot from `@promonta_bot` to `@GrandMont_bot` via a temporary dual-token verification window (`BOT_TOKEN_NEW` optional env var), rather than a hard cutover.
**Context**: Owner created a new bot and wants to move the Mini App to it without breaking any existing worker's login/session or losing identity continuity. `telegram_user_id` is the canonical Mini App identity (resolved to a Core Worker UUID via `source_system=miniapp, legacy_id=<telegram_user_id>`) — this must not change as a side effect of a bot swap.
**Problem**: `validate_init_data()` verifies Telegram WebApp initData's HMAC signature against a single `BOT_TOKEN`. A straight token swap would instantly invalidate every in-flight session signed by the old bot and give no window to verify the new bot actually works in production before committing.
**Options considered**: (1) swap `BOT_TOKEN` directly and accept a hard cutover with no fallback; (2) run two separate backend processes, one per bot, routed by some reverse-proxy rule; (3) accept initData signed by either token for a bounded migration window, keep outbound sends and session-token signing on the old token throughout, remove the fallback once the new bot is confirmed working.
**Chosen**: (3).
**Why**: (1) has no rollback if the new bot's Mini App launch, initData signing, or Menu Button config turns out subtly wrong in production — found out from a real user's failed login, not a test. (2) adds real infrastructure complexity (two processes, routing logic) to solve a problem that's purely about which token signs a hash, not about running two different app instances. (3) is the smallest change that gives a real rollback window: the old bot keeps working for existing users throughout, the new bot can be verified end-to-end (Menu Button → Mini App open → initData → same Core Worker UUID → DailyPlan/shifts/absence) before the fallback is ever removed, and removing it later is a one-line env change, not a code change.
**Consequences**: `validate_init_data()` now checks up to two candidate tokens instead of one (negligible perf cost, two HMAC computations worst case). `_secret_key()`'s signature changed from zero-arg to an optional `bot_token` parameter defaulting to the old `BOT_TOKEN` — existing zero-arg callers (`main.py`'s re-export, `tests/test_access_control.py`) are unaffected. Outbound sends and session-token signing deliberately did NOT move to dual-token — see `docs/SECURITY.md`'s migration-window note for why, and don't "simplify" this by making everything dual-token in a later change without re-reading that reasoning. CRM's invite-link generation needed no change — confirmed it already targets the plain HTTPS Mini App URL, not a `t.me/<bot>` link.
**Affected files**: `backend/core/telegram.py` (`BOT_TOKEN_NEW`), `backend/core/permissions.py` (`_secret_key`, `validate_init_data`), `tests/test_bot_token_migration.py` (new), `docs/SECURITY.md`, `docs/CHANGELOG.md`.
