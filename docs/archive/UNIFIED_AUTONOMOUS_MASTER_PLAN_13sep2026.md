> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

AUTONOMOUS_STATUS: IN_PROGRESS

# Unified Autonomous Master Plan - 2026-09-13

This is the single source of truth for the next Grandmont Group miniapp work. It combines:

- the premium UI screenshot audit from 2026-09-13;
- the previous 3 large autonomous stages;
- the owner-pasted product gap backlog;
- verification, deploy, and handoff rules.

Preparation note: the autonomous runner is prepared, but must stay disabled until the owner explicitly asks to start it. If this file is later executed by the enabled systemd runner, treat that invocation as authorization to continue autonomously.

## Hard Rules

- Work in `/home/promonta/agent/miniapp-repo` on the VPS, or the matching local repo when preparing commits locally.
- Do not start or enable `autonomous-miniapp.timer` unless the owner explicitly asks.
- Before changing code, inspect the exact existing implementation and nearby tests.
- Keep commits small and logical. Do not mix unrelated refactors with product work.
- Never print secrets, tokens, auth files, SSH keys, `.env` values, or Telegram/OpenAI credentials.
- Do not run two agents against the same dirty worktree. When this plan is executed by `scripts/autonomous_codex_runner.sh`, `/home/promonta/agent/.codex-autonomous-miniapp.lock` and the parent runner/Codex processes are expected and belong to the current run; do not stop merely because they exist. Stop only if you find a second independent Codex/autonomous process or unrelated dirty worktree changes.
- Prefer existing architecture and helper APIs. Avoid large rewrites unless the local code already points that way.
- Use `rg` for search and patch-style edits for manual changes.
- After each meaningful code change, run focused tests first, then broader tests when practical.
- Before production deploy: commit and push.
- Deploy only from the server repo with `bash scripts/deploy.sh`.
- After deploy, verify `curl -fsS https://app.promonta.fun/api/health`.
- Update `/home/promonta/agent/FILESYSTEM_MAP.md`, `docs/CHANGELOG.md`, and `docs/SESSION_HANDOFF.md` after deployed structural changes.
- When all required stages are genuinely done, change the first line to `AUTONOMOUS_STATUS: DONE`, commit, push, deploy, update the server map, then stop.

## Current Baseline

Latest known deployed SHA before the autonomous runner was installed: `4f23082`.

Runner infrastructure commit: `6bdcc19` (`chore: add resumable codex autonomous runner`). The runner is installed on the VPS but disabled/inactive at the time this plan was created.

Recent completed work already includes:

- unified photo/news comment bottom sheet behavior;
- Instagram-like dark comment composer and reply/delete ownership basics;
- feed save hardening and saved filters per tab;
- AI composer visibility;
- objects FAB/card polish;
- Kontrol day filter swipe guard.

Audit those areas before touching them again. Fix only remaining defects.

## A. Premium Visual Direction

Owner feedback from the screenshot batch: colors differ everywhere, fonts look inconsistent, and the app feels raw instead of premium.

Root cause: the app currently mixes several visual systems:

- cinematic black/gold splash;
- beige "Old Money" cards;
- heavy dark glass bottom nav;
- Instagram-like dark comments;
- green chat bubbles;
- dark calendar;
- emoji weather/media accents;
- huge bold dashboard typography;
- unrelated pill, tab, chip, card, and button styles.

Decision: move the whole app toward one premium iPhone-style operational UI. Keep Instagram-like patterns only where they are natural: feed media, comments, reactions, saved/share actions, and chat-adjacent action sheets.

Explicit chat style rule:

- Feed photo cards and all feed comments stay Instagram-like.
- Comments stay as a dark Instagram-style bottom sheet with emoji row, reply/delete/actions, and media-first context.
- Chat list becomes premium iOS/Apple Messages-like: compact rows, search, avatars, last message, unread/status, and no oversized empty top space.
- Chat threads become premium iOS/Telegram messenger style, not full Instagram comments style: clean bubbles, current user on the right, other users on the left, light working background, stable iOS composer with attach/mic/location/send.
- Chat actions, delete/reply sheets, quick reactions, and visual polish should reuse the same quality and behavior language as the Instagram-style comments.
- Object chat should be a calm light work messenger. Do not make object chat a dark Instagram comment sheet.

### Global Design Rules

- One app language across all tabs: iOS-like surfaces, consistent typography, consistent icons, consistent sheets.
- Use Apple/SF system font first, Manrope fallback.
- Keep a restrained palette:
  - background: warm neutral light;
  - primary surface: white;
  - grouped surface: light warm gray;
  - primary text: near black, not pure black;
  - secondary text: muted gray/taupe;
  - accent: Grandmont Group forest green;
  - brass/gold: only small brand/status accents.
- Red/yellow/green are semantic only.
- Avoid decorative gradients, emoji controls, novelty effects, and one-off visual patches.
- Cards should usually be 8-14px radius. Do not put cards inside cards.
- Motion must be subtle and respect reduced motion.

### Typography

- Page title: 34-38px, 800, only top-level screens.
- Screen/subtitle title: 24-28px, 750.
- Card title: 17-20px, 700.
- Body: 16-17px, 400-500.
- Meta: 13-14px, 500.
- Button label: 16-17px, 650.
- Avoid large all-caps Russian labels and remove letter spacing from normal Russian text.

### Shared Components To Build First

Create/reuse these component classes before broad screen restyling:

- `.ios-page`
- `.ios-section`
- `.ios-card`
- `.ios-list`
- `.ios-list-row`
- `.ios-segmented`
- `.ios-chip`
- `.ios-action-button`
- `.ios-icon-button`
- `.ios-bottom-sheet`
- `.ios-empty-state`
- `.ios-status-pill`
- `.ios-stat-tile`

Acceptance: feed tabs, object tabs, profile tabs, team tabs, saved filters, and date filters look like variants of one segmented/chip system.

## B. Stage 1 - Premium UI And Interaction Polish

Goal: the app should feel like a polished iPhone Telegram miniapp, with stable sheets, no flicker, no stale screen flash, and consistent Instagram-like action patterns where appropriate.

### App Shell

- Keep Telegram safe-area handling stable.
- Header should feel native iOS and not fight Telegram Close/Menu buttons.
- Bottom nav can remain floating, but must be lighter: less shadow, cleaner active state, consistent icon stroke, and enough bottom spacing so it does not cover content actions.
- Use one keyboard/sheet strategy for comments, chat, AI, object creation, and finish wizard.

### Splash

- Current splash is polished alone but disconnected from the app.
- Either simplify it into the same light/green premium system, or bring only a subtle version of its brand warmth into the app.
- Do not keep a luxury black/gold poster followed by unrelated beige admin screens.

### Dashboard/Home

- Replace the heavy dashboard feeling with an owner "Today" cockpit:
  - compact KPI row;
  - active shifts;
  - workers who have not started;
  - unconfirmed/overdue tasks;
  - material/delay/budget risks;
  - short "everything else ok" summary.
- Radio widget is visually from another app. Lighten it or remove it from the core operational dashboard.
- Rows like Messages/Calendar/Tools/Documents/AI/Contracts should become compact iOS list rows with useful secondary text, not oversized identical cards.

### Feed

- Photo tab: media-first Instagram card, clean header, consistent like/comment/share/save icons, fixed aspect ratio, visible action/caption area above nav spacing.
- News tab: Apple News/Notes-like cards: smaller source/category row, strong but not oversized title, lighter summary, same action row as photos.
- Weather tab: keep as `Погода`, use premium row/icon styling instead of emoji-heavy cards, keep saved weather separate from saved photos/news.
- Fresh counters: small iOS badges only when non-zero, no layout shift.

### Comments

- Comments are always a bottom sheet/popup, not a separate route.
- One shared visual system for photo, news, weather, and future object/feed comments.
- Backdrop tap closes reliably.
- Input composer must not jump, flicker, or create gray artifacts after send.
- Newly sent comments must remain visible and scroll into view.
- Quick emoji row must have visible feedback and no dead taps.
- `...` actions open a small dark action sheet above comments.
- Worker can delete only own comments. Owner/admin can moderate only according to backend role rules.

### Chat

- Chat list should become Apple Messages-like: compact list, inline search, no giant empty top area.
- Object/general chat should share the polished composer/action-sheet behavior from comments, but keep a light iOS/Telegram messenger surface.
- Switching from one chat to another must not show the previous chat while the new chat loads.
- Add or polish swipe-to-reply after the core stale-chat/flicker issue is fixed.
- Chat location send button should be available when Stage 2 location work is wired.

### Objects

- Object detail should become an object cockpit:
  - photo/header;
  - status, stage, risk summary;
  - team/shift state;
  - next actions;
  - tabs below;
  - long editable admin sections collapsed or moved deeper.
- Plan Work should be a clean stage timeline/list with status pill, due/risk info, and compact problem action.
- Budget dashboard belongs inside object cockpit, not as noisy global clutter.

### Profile, Tools, Calendar, Team, Needs, Alerts

- Profile: iOS Settings-style list groups; app/system info as small rows, not developer-looking cards.
- Tools: iOS search/list pattern, clear owner add action, no clipped search placeholder.
- Calendar: prefer light calendar to match app; avoid half-dark/half-light split state.
- Team/Control/Needs: use the same manager cockpit summary-card and list-row system.
- Alerts sheet: keep the useful bottom sheet, replace emoji/severity circles with shared icons and status colors.

Expected Stage 1 tests:

- Existing frontend contracts for feed photo, comments, save filters, chat actions, AI composer, objects, finish wizard, and swipe guards.
- Add focused regression tests for any fixed flicker/stale data/delete permission bug.

## C. Stage 2 - Worker Start/Finish, Geo, Photos, Voice, Offline

Goal: workers should start and finish shifts with evidence and almost no typing friction.

Known current state to verify before changing:

- Finish wizard already has multiple photo, summary, geolocation, and voice button work in the codebase.
- Start shift already has geolocation paths.
- Do not duplicate these systems; inspect and complete/polish them.

Checklist:

- Start shift captures geolocation at shift start: lat/lon/accuracy/timestamp when available.
- Finish shift captures geolocation at finish.
- Finishing without photos is impossible.
- Finish requires at least 2 photos from different angles; allow more than 2.
- Finish flow is sequential: photos first, then additional-work text, then per-item/summary/confirmation based on current data model.
- Additional-work text step supports voice input/transcription where possible. Use existing audio/transcription/AI endpoints if present; otherwise create a clean placeholder contract.
- Add ability to send/share current geolocation inside chat.
- Shared frontend API layer needs timeout/retry/abort states. Avoid duplicate writes unless idempotency exists.
- Add offline finish/check-in outbox:
  - worker can take photos/write report with bad internet;
  - data is stored locally and retried later;
  - idempotency keys prevent duplicate finish submissions.
- Backend must validate `daily_plan_id` at finish: submitted plan belongs to worker and accepted context.
- Add per-worker DailyPlan acknowledgment state.

Product decision default: use the hybrid shift-start model. Worker may start without a plan only if no plan was published by cutoff, or if worker explicitly chooses "start without plan" with a reason. Owner cockpit surfaces it as a warning.

Expected Stage 2 tests:

- Backend tests for start location payload, finish location payload, minimum photos, DailyPlan validation, outbox/idempotency, chat location message, overdue alerts.
- Frontend contract tests for finish wizard steps, disabled finish until requirements are met, voice input affordance, retry/offline states, and chat location button.

## D. Stage 3 - Manager Cockpit, AI Voice Command, Broadcast

Goal: the owner/manager should have one fast operational cockpit.

Checklist:

- Manager daily summary:
  - greeting by time of day;
  - object count;
  - worker count;
  - workers who have not started;
  - missing materials/delay/budget risks;
  - short "others ok" summary when clean.
- Task confirmation:
  - manager assigns task;
  - worker can confirm/accept;
  - after 2h without reaction, show yellow warning;
  - after 4h without reaction, show red/unconfirmed alert.
- Add overdue task alerts into the same operational surface.
- Add central voice/action entry point without visual clutter.
- Russian voice/text management command parser should handle:
  `Поставь Ивану завтра задачу закончить потолок у Мюллера и скажи ему взять лазер.`
- Parser extracts object, worker, date, task, and comment.
- Prefer safe draft/confirmation before automatic assignment.
- Add quick broadcast controls: announcement to everyone on an object or the whole company.
- Add human-readable object history: status changed, worker assigned, stage completed, document uploaded, defect created, finish submitted.

Expected Stage 3 tests:

- Parser/unit tests for Russian management command.
- Backend tests for task confirmation escalation at 2h/4h.
- Frontend contract tests for manager summary, central action/voice button, broadcast controls, and object history.

## E. Product Gap Backlog

P1 - daily-use operational gaps:

- Offline finish/check-in outbox.
- Shared frontend API timeout/retry/abort layer.
- Server-side DailyPlan validation at finish.
- Per-worker DailyPlan acknowledgment.
- Owner daily cockpit.
- Human-readable object history.

P2 - high-value product UI:

- Drag/drop worker to object, opening assignment sheet with from/to/task/work type.
- Object budget dashboard with budget/spent/remaining/risk.
- Object task Kanban: To Do -> In Progress -> Done.
- Document gallery/preview grid with object filters and quick actions.
- Extended calendar: week/month/year, drag/drop event moves, day bottom sheet.
- Interactive Stundenzettel graphs.
- Dashboard mini charts/sparklines.
- Tool booking by availability calendar.
- News category/tag filtering and optional mute/unmute.
- Swipe-to-reply in chat.

P3 - platform and architecture:

- CRM block: customers, leads, contacts, deals/objects, communication history, documents.
- Google Sheets write-back for DailyPlan after durable sync exists.
- Durable external sync queue.
- Contract RED risk from contract finish date and actual progress.
- Crew productivity accuracy for multi-worker objects.
- Real staging environment.
- Full automated E2E: owner assigns -> worker accepts -> starts -> completes plan -> photos -> finishes -> owner sees result.
- Backend `main.py` domain split.
- Frontend thin-shell split for `app.html`, `home.js`, `chat.js`.
- PostgreSQL migration when flat JSON becomes limiting.
- Self-contained server install for external object scripts.
- CI final hardening.

## F. Verification, Deploy, Definition Of Done

For each completed slice:

- Run focused tests.
- Run broader backend/frontend contract tests when practical.
- Update docs only where behavior or structure actually changed.
- Commit and push.

Before production deploy:

- Confirm clean git state except intended changes.
- Deploy from VPS with `bash scripts/deploy.sh`.
- Verify health endpoint.
- Update `/home/promonta/agent/FILESYSTEM_MAP.md` with changed app structure and latest deployed SHA.

Done means:

- Stage 1, Stage 2, and Stage 3 are implemented or have explicit tiny blocker notes.
- P1 backlog items that overlap the stages are handled or explicitly scheduled with blocker notes.
- Tests pass on server.
- Production is deployed and health check passes.
- Changelog, session handoff, and server map are updated.
- First line is changed to `AUTONOMOUS_STATUS: DONE`.

## G. Completion Audit - 2026-09-15

Status: DONE after the final server deploy and health verification for this plan.

Implemented coverage:

- Stage 1 premium UI: shared `.ios-*` primitives, light splash, Home `Сегодня`
  cockpit, Feed/comment action polish, Chat list/stale-load cleanup, Profile,
  Tools, Calendar, Team/Control, Needs, Alerts, and bottom-nav/sheet alignment
  now have focused frontend contract coverage.
- Stage 2 worker evidence/offline flow: start/finish geo metadata, required
  finish photos, finish wizard sequencing/voice affordance, chat location
  sending, shared API timeout/retry/abort behavior, durable check-in/finish
  outbox, DailyPlan finish validation, and DailyPlan acknowledgment are covered
  by backend and frontend contracts.
- Stage 3 manager cockpit/command/broadcast/history: Home daily summary,
  assignment confirmation escalation, overdue/confirmation alerts, safe Russian
  command parser, central AI command entry with voice dictation, quick broadcast
  controls, and human-readable object history are implemented and tested.
- P1 overlap: offline finish/check-in outbox, shared API retry layer,
  DailyPlan validation/acknowledgment, owner daily cockpit, and object history
  are handled by the shipped slices above.

Tiny explicit note:

- `frontend/js/home.js` remains a large/root-owned legacy surface in this VPS
  checkout, so deeper Home label micro-polish is intentionally deferred. The
  required owner cockpit data, escalation signals, clean-state behavior, and
  contract coverage are shipped; this is cosmetic debt, not a functional blocker
  for the unified autonomous plan.

## H. Continuation - 2026-09-15 (owner-authorized)

Section G's completion audit covered Stage 1/2/3. Section E's P1/P2/P3
backlog below Section D was NOT covered by that audit and remains open.
Owner has explicitly authorized continuing this same plan to work through
the P1 backlog next (P2/P3 after, in order), one commit per item, test+deploy
per item exactly per the Hard Rules and Section F above. Re-verify each P1
item against current code first (some may already be partially covered by
Stage 2/3 work — check before implementing, do not duplicate).

Priority order for this continuation: Section E's P1 list top to bottom,
skipping anything already verifiably shipped. Then P2. Do not start P3
(architecture-scale items: CRM, PostgreSQL, main.py split, app.html split)
without a separate explicit owner go-ahead — flag P3 as still deferred in
the next completion audit instead.

When P1 and P2 are genuinely done (or have explicit tiny blocker notes same
as Section G's format), write a new dated Completion Audit subsection below
this one, set the first line back to AUTONOMOUS_STATUS: DONE, and stop. Do
not re-enable autonomous-miniapp.timer — it was deliberately disabled by the
owner on 2026-09-15; this continuation is a manual one-off run.

### Continuation Progress - 2026-09-17

- P1 re-verification: all P1 overlap items are already covered by shipped
  Stage 2/3 work and tests, so no duplicate implementation was added.
- P2 item 1 in progress: Team drag/drop worker-to-object assignment now opens
  the existing Assignment Sheet with worker and object preselected, while still
  collecting work type, from/to period, optional task note, and confirmation.
- P2 item 2 in progress: Object Info now includes an owner-only budget dashboard
  with budget, spent, remaining/overrun, and green/yellow/red risk state.
- P2 item 3 in progress: Object Info now includes an object-scoped task Kanban
  using existing `/api/tasks` statuses (`Нужно` / `В работе` / `Готово`) with
  owner actions, drag/drop lane moves, and task chat access.
- P2 item 4 in progress: Documents view now includes an object-document gallery
  with per-object filters, authenticated preview grid, open-object action, and
  owner-only delete quick action while preserving Angebot/Rechnung generation.
