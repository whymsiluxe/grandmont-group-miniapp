> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# PROMONTA MINIAPP — FINAL PRODUCTION HARDENING ROUND

GOAL:
This is the LAST global functional/architecture hardening round before
visual design freeze and production rollout.

Do NOT redesign the UI.
Do NOT migrate to PostgreSQL.
Do NOT rewrite to React.
Do NOT split all FastAPI routes.
Do NOT invent new features.
Do NOT touch chat keyboard geometry (v11d) — that bug is handled in a
SEPARATE round later. Leave app.html's chat keyboard state machine alone.

Baseline at round start: HEAD d122b30, 176 routes, clean tree.

IMPORTANT:
Do not trust old handoff/docs/test-count claims from this plan text itself.
This plan was authored by an external reviewer (ChatGPT) reading a source
snapshot that may be stale or slightly wrong. Before fixing ANY finding
below: re-read the actual current code at the referenced file/function
first. If a finding doesn't match current reality (already fixed, doesn't
exist, wrong file), note it as "finding did not reproduce" in the final
report instead of forcing a fix. Do not blindly apply a diff described in
prose without re-deriving it from the real code.

Owner explicitly authorized: deploy immediately after each individually
verified fix (same as the previous autonomous round) — commit, push, deploy
per logical fix, do not batch. Owner also wants a FINAL COMPARISON REPORT:
what got better, what got worse (if anything), concretely — not just a list
of commits.

============================================================
PHASE 0 — BASELINE
============================================================

Record:
START_SHA, git status, route count, isolated pytest count, frontend JS
inventory, backend runtime dependency inventory (what backend/*.py + core/*.py
actually exist and are imported by main.py).

Use PROMONTA_ENV=test, temporary MINIAPP_DATA_ROOT, dummy BOT_TOKEN.
NEVER run tests against production data root. Do not modify production data
during audit.

============================================================
PHASE 1 — DEPLOY / ROLLBACK ARTIFACT INTEGRITY [P0]
============================================================

Claimed finding: main.py imports daily_plan_lib, but scripts/deploy.sh and
rollback.sh do not include daily_plan_lib.py in their copy list. Also: deploy
replaces backend/core/ as a directory, but rollback does not restore
backend/core/.

VERIFY FIRST by reading scripts/deploy.sh and scripts/rollback.sh (or
equivalent) as they exist right now. If daily_plan_lib.py truly is missing
from the deploy manifest, or rollback truly doesn't restore core/, this is a
real P0 — fix architecturally:

Create ONE backend runtime artifact manifest/source of truth used by backup,
deploy, rollback, syntax checks, and production-layout import smoke. Must
include all runtime tracked backend code (main.py + every backend/*.py lib
actually imported + backend/core/** + any non-python runtime file main.py
shells out to or requires, e.g. angebot_free.js/rechnung.js if those are
real). Do not manually maintain divergent lists again.

Add automated test: old artifact -> deploy candidate -> rollback -> hashes
equal old artifact tree (can be simulated in a temp dir, no need to touch
real prod paths during the test).

Also check: does the create-object flow shell out to
/home/promonta/agent/create_object.py or create_object_folder.py OUTSIDE the
git repo? If so and these files are real and in use, either bring them into
the tracked repo (scripts/ or backend/) so a clean clone reproduces full
functionality, or confirm they're vestigial and note it — do not silently
leave an undocumented external dependency if it's load-bearing.

============================================================
PHASE 2 — MAKE CI REAL [P0]
============================================================

Check .github/workflows/ci.yml as it currently exists. Check for a
production-package-layout smoke test — does it copy backend/*.py without
backend/core/, while main.py imports .core.*? If so, fix it to build the
SAME manifest as Phase 1's artifact list, then: import miniapp.main exactly
like systemd/uvicorn production would, assert route count, start ASGI, hit
health.

Make syntax checks recursive — must cover backend/core/**, frontend/js/core/**,
frontend/js/components/** if those directories exist (check first; don't
assume the exact tree from the plan text, verify with find/ls).

Confirm whether GitHub Actions has any actual recorded runs for recent
commits (gh api or gh run list if gh CLI is authenticated on this box; if
not available, note "could not verify Actions run status — gh not
authenticated" rather than guessing). Do not claim "CI green" from local
pytest alone in the final report — label clearly which result is local vs.
actual Actions run.

============================================================
PHASE 3 — DESTRUCTIVE CHAT DELETE BUG [P0]
============================================================

Claimed bug: DELETE /api/chat/threads?with_=X selects messages by
"sender == X OR recipient == X" instead of the exact thread pair, so owner
deleting DM with Ivan could also delete Ivan<->Viktor, Ivan<->Peter threads.

VERIFY FIRST: find the actual current implementation of this endpoint in
main.py. Read exactly how it filters messages. If the claim is accurate, fix
by exact thread identity: build target_thread_id from (current_user_id,
with_) using whatever thread-key helper already exists in the codebase
(check for something like _chat_thread_id or similar — do not invent a new
one if one exists), and only touch messages whose own (user_id, to_user_id)
pair resolves to that same thread id.

Add test: owner<->A, A<->B, B<->C all exist; delete owner<->A; assert only
owner<->A affected, A<->B and B<->C untouched, reactions on untouched
messages untouched.

Also check (only if trivial from the same code path, don't scope-creep): can
a new DM be opened to an arbitrary/non-existent/revoked user id? If yes and
it's a quick guard, add validation that to_user_id is an active authorized
user and not self. If it requires deeper surgery, skip and note in report.

============================================================
PHASE 4 — ASSIGNMENT -> DAILYPLAN -> CHECKIN INVARIANT [P0]
============================================================

Read the actual current DailyPlan create/publish endpoint and the checkin
Start endpoint in main.py/daily_plan_lib.py before assuming the claimed gaps
exist.

Claimed gap 1: POST /api/daily-plan (or equivalent) creates a plan without
verifying the assigned worker actually has an active, accepted assignment
covering that object+date, correct role=worker, no absence conflict, valid
stage/work_type. If real, add server-side validation at create/publish time
so a worker can never be handed a plan they are not legally eligible to
start.

Claimed gap 2: Start/checkin accepts a client-supplied daily_plan_id without
requiring daily_plan_acceptance_id, and trusts a client-supplied
daily_plan_version. If real: require a valid acceptance record tied to THIS
worker+plan before Start proceeds when a daily_plan_id is given; derive
plan_version and accepted_context_snapshot server-side, never from the
client body.

Decide and implement explicitly for "no plan published" Start: if this
codebase already has a deliberate policy here (check daily_plan_lib.py /
checkin code / existing tests for it first), keep it and just document it in
the report. If there is truly no policy and a plan exists for today for this
worker/object, block silent bypass — require an explicit "start without
plan" path with a reason field, not a quiet fallthrough. If implementing this
is a genuine product-policy question rather than a bug, write it to
docs/OPEN_QUESTIONS_11sep2026.md instead of guessing, and move on.

Add E2E-style test at the level the existing test suite already tests these
flows (direct route-handler calls, same pattern as
tests/test_assignment_lifecycle.py etc.): assign -> accept -> publish plan ->
accept plan -> start -> finish, plus a rejection case (start without
required acceptance -> blocked).

============================================================
PHASE 5 — JSON DATA INTEGRITY [P1]
============================================================

core/storage.py documents that load -> mutate -> _atomic_write_json is not
atomic read-modify-write, and that update_json_transaction() exists for real
atomicity. Grep the codebase for load_X()...save_X() pairs used in mutation
endpoints. Confirm the claimed candidate (OBJECT_INFO_FILE: description,
info items, documents) is real. Convert genuinely concurrent-risk,
business-critical mutation flows to update_json_transaction() — do not
convert read-only call sites, do not touch every single store speculatively.
Prioritize by actual risk (multi-user simultaneous edit likelihood), not by
exhaustively converting everything — this is P1, time-boxed, not a full
rewrite.

============================================================
PHASE 6 — CROSS-PROCESS STORAGE SAFETY [P1]
============================================================

Check whether uvicorn/systemd on this VPS actually runs more than one worker
process (check the systemd unit file / uvicorn invocation for --workers).
If it's already hard-pinned to a single worker, this whole phase is
low-risk — just add a startup assertion/comment confirming the single-worker
invariant instead of building new locking. If it's NOT pinned to one worker,
either pin it (simplest, safest, matches current real usage — ~10 users) or
add cross-process flock to core storage's write path, matching the pattern
daily_plan_lib.py already uses. Prefer pinning single-worker + assertion
over new locking code unless there's a concrete reason multiple workers are
needed (check for one; there almost certainly isn't at this scale).

============================================================
PHASE 7 — CRITICAL STORE POLICY [P1]
============================================================

Read core/constants.py's CRITICAL_JSON_PATHS (or equivalent) as it exists
now. Evaluate whether FINISH_OUTBOX_FILE, OBJECT_INFO_FILE, OBJECT_IMAGES_FILE,
WORK_CALENDAR_FILE are already critical or not. Add whichever are missing
and whose silent-corruption-to-{} would cause real business harm (outbox
definitely qualifies — it's the finish-projection reliability guarantee).

Check whether daily_plan_lib.py's own JSON loader actually goes through
core storage's critical-path/quarantine logic or has its own separate
error handling. If it bypasses it entirely (as claimed), unify: on
JSONDecodeError, quarantine + persistent corrupt-lock + controlled
unavailable response, matching the behavior core storage already gives
critical stores — reuse existing quarantine helper functions, don't
duplicate the logic.

============================================================
PHASE 8 — AUDIT TRAIL [P1]
============================================================

Read the current audit_log_middleware (or equivalent) and the current auth
flow (Bearer session token path vs X-Telegram-Init-Data path). Confirm
whether successful Bearer-authenticated mutations really do log
user_id:null as claimed. If so: during successful auth (wherever the
session token is validated), set request.state with normalized actor
identity; have the audit middleware read that after call_next() instead of
re-parsing headers itself. Do not duplicate auth-parsing logic in the
middleware. Never log token/initData/GPS/chat body/uploaded content/secrets
in the audit line — confirm current logging doesn't already leak any of
these while touching this code.

============================================================
PHASE 9 — BUSINESS DATE CONSISTENCY [P1]
============================================================

Grep for datetime.utcnow() / date.today() used specifically to derive a
BUSINESS DATE (not a timestamp) in Abwesenheit-related code
(close_abwesenheit / auto-close-expired-open-ended logic or equivalent
current function names — names may differ from the plan text, search for
the actual functions). Replace business-date derivations with
business_today_str() (already used elsewhere in the codebase). Leave real
timestamps (created_at, audit log times, etc.) as UTC — only business-date
fields change. Add a boundary test around a Europe/Berlin midnight case if
the existing test file for Abwesenheit has a pattern to extend.

============================================================
PHASE 10 — DASHBOARD STABLE ID LOGIC [P1]
============================================================

Check home.js's absence-matching logic as it currently exists (function name
may have changed since the plan was written — search for it). If it
currently matches by worker display name instead of user_id, fix to match
by String(user_id) === String(user_id), keeping name only for display. Add a
regression case: two workers with the same display name, correct one stays
marked absent.

============================================================
PHASE 11 — STARTUP TWO-TIER BOOTSTRAP [P0 UX]
============================================================

Read app.html's current AppBootstrap task registration and splash-hide logic
exactly as it exists now (already read once this session while working chat
keyboard fixes — re-check nothing changed). If it still does
Promise.allSettled(ALL tasks) before hideSplash as claimed, implement:

TIER 1 (blocks splash): auth/session, role, minimum Home data, worker
Today/checkin critical state if worker.
TIER 2 (background after splash hides): feed/news/weather, photos/media,
chat history, tools, stages, secondary dashboard widgets, other tabs.

Each Tier 2 view needs its own loading/empty/error/retry — no second
fullscreen splash, local skeletons instead. Dedupe any duplicate concurrent
requests for the same resource (shared Promise/cache) found while doing
this — don't go hunting for unrelated dedup opportunities beyond what's
touched here.

Instrument and MEASURE: APP_START, AUTH_DONE, TIER1_DONE,
FIRST_USABLE_SCREEN, SPLASH_HIDDEN, TIER2_DONE — log these (console.log
with timestamps is fine, this is a diagnostic instrumentation, not a
permanent feature) for both owner and worker cold start, and report raw ms
+ request count BEFORE vs AFTER in the final report. This is the single
largest change in this round — take real care not to break any existing
view's data loading; if something can't be safely deferred to Tier 2
without breaking a currently-working flow, keep it in Tier 1 and note why.

============================================================
PHASE 12 — FRONTEND REQUEST RELIABILITY [P1]
============================================================

Check shared.js's api() function's current signature. If it truly has no
timeout, add a central AbortController-based timeout: normal GET/mutation a
finite timeout (~10-15s), upload/transcribe/AI endpoints an explicitly
longer one. Must correctly combine with any caller-supplied AbortSignal
(don't break existing cancellation). Normalize the resulting error into
distinguishable cases (timeout / offline / HTTP error / auth expired) so
callers can show a real message instead of hanging forever. Audit that
every async action button used with api() restores its enabled/loading
state in a finally block — fix any confirmed missing ones found while doing
this, don't do a speculative sweep beyond what surfaces naturally.

============================================================
PHASE 13 — CHAT — DO NOT TOUCH
============================================================

Skip entirely. Chat keyboard geometry (v11d, SHA b635704) is being handled
in a separate, later round per explicit owner instruction. Do not modify
app.html's chat keyboard state machine, chat.js, ai.js composer code, or
shared.js's _bindTouchSafeSend in this round, even if you notice something
that looks improvable. Leave it exactly as-is.

============================================================
PHASE 14 — PERMISSION MATRIX [SECURITY CONTRACT]
============================================================

Create docs/PERMISSION_MATRIX.md by reading actual current backend
authorization checks (not by copying the plan's guessed table) for:
objects, budget, object info, documents, stages, roadmap, blockers,
defects, needs/tasks, tools, checkins/GPS/photos, worker profile, absence,
chat, DailyPlan, productivity, contracts, AI, roles/access. Columns: Owner /
Assigned Worker / Other Worker / Revoked-Profile-only User. Document
existing intentional broad-read policies (e.g. if workers can see other
objects' stages/defects) as-is — flag, do not silently change intentional
product policy.

One targeted fix if confirmed real: blocker-clear currently allowed for
any worker with object access — if so, restrict to owner OR the blocker's
own creator. Verify this is actually the current behavior before changing
it.

============================================================
PHASE 15 — SECURITY HARDENING [P1]
============================================================

Add a simple in-memory per-user rate limiter (document that it's
single-instance-only, matching Phase 6's single-worker reality) to
expensive endpoints that don't already have one: transcribe, chat voice,
chat attachments, feed uploads, mangel uploads, documents, checkin photos,
AI vision, PDF generation, object creation. Reuse the existing AI rate
limiter's pattern if one already exists in the code rather than inventing a
new mechanism.

Run pip-audit and npm audit (or confirm Dependabot is configured) — report
findings, do not necessarily upgrade every flagged dependency blindly if it
risks breaking something; use judgment, note anything left unaddressed and
why.

Confirm OWNER_AI_ENABLED is false and leave it false.

Flag repo visibility (public/private) in the final report as a manual
action item for the owner — do not attempt to change GitHub repo visibility
yourself via API/CLI without explicit separate confirmation, since this is
an account-level, easily-reversible-but-should-be-owner's-call setting.

============================================================
PHASE 16 — RETENTION / PRIVACY OPERATIONS [P1]
============================================================

Inventory stored personal/business data (GPS, checkin photos, voice/
transcription audio, chat+archive, attachments, avatars, absence, audit
log, backups). Write a retention policy doc
(docs/DATA_RETENTION_POLICY.md) with concrete proposed periods per category
(reasonable defaults, e.g. audit log 1 year, transcribed audio 90 days —
use judgment, this is a first draft for owner review, not a legal
document). Implement a simple, safe cleanup job ONLY if it's low-risk and
clearly won't delete anything still referenced by live/archive metadata —
otherwise leave implementation as a follow-up and just ship the policy doc
plus an OPEN_QUESTIONS note.

============================================================
PHASE 17 — STAGING / OPERATIONS [P1]
============================================================

This phase requires provisioning a genuinely separate service/URL/data
root on the VPS. Do NOT provision new staging infrastructure automatically
in this autonomous round — that's an infra decision with cost/complexity
implications beyond a code-hardening pass. Instead: write up exactly what
staging would require (systemd unit, data root, URL/subdomain, test bot)
into docs/STAGING_SETUP_PLAN.md as a followup, and skip actual provisioning.

DO implement the smaller, safe part: an internal readiness check (distinct
from the existing liveness /api/health) that verifies runtime artifact
completeness, critical stores readable, data root writable, DailyPlan store
readable, outbox readable, no corrupt-lock present. Keep it non-public or
owner-authenticated only — no secret-bearing public endpoint.

============================================================
PHASE 18 — BACKUP / RESTORE DRILL
============================================================

Perform one real restore drill using a TEMP directory only, never touching
real production data: seed temp data, run backup, mutate, restore, verify
hashes match, start the app against the restored temp copy, hit health.
Report pass/fail concretely. If Phase 1's manifest fix changes what backup
covers, this drill should exercise the NEW manifest.

============================================================
PHASE 19 — OWNER <-> WORKER FULL E2E
============================================================

Add/extend automated tests (same route-handler-call style as the existing
suite) covering as much of the full flow as is practical at this level:
grant access -> assign -> publish plan -> worker accepts -> start -> pause/
resume -> finish -> owner sees result; revoke access -> worker loses
protected access but historical records remain; same worker multiple work
types on one object; overlap blocked across objects but allowed on
different dates. This does not need to be literal browser E2E — extend the
existing backend test style, that's what this codebase already uses for
this purpose.

============================================================
PHASE 20 — FRONTEND SAFETY / UX CONSISTENCY
============================================================

Do NOT redesign visually. Do a focused XSS audit: grep for innerHTML
assignments in frontend/js that interpolate user-generated values (names,
object names, addresses, chat/comments, defects, tasks, notes, captions)
without escaping. Fix confirmed unsafe call sites only — check if an
existing escape helper is already used elsewhere in the codebase (there
almost certainly is one, given this app already has XSS-conscious code per
earlier session history) and reuse it, don't invent a new one.

============================================================
PHASE 21 — DOCUMENTATION TRUTH
============================================================

After all fixes, write ONE canonical release-state doc (or update the
existing PROJECT_STATE-style doc if one already exists — check first rather
than creating a duplicate) with: GitHub SHA, production SHA, route count,
verified test count, open blockers, known intentional permission rules,
deploy/rollback/backup commands, this round's real-device-required items
still outstanding (chat keyboard pass is explicitly one, deferred by owner
instruction).

============================================================
FINAL REPORT — BEFORE VS AFTER COMPARISON (owner explicitly requested this)
============================================================

Owner wants a clear "did this make things better or worse" comparison, not
just a commit list. The final docs/HANDOFF_11sep2026_hardening.md must
include:

- START_SHA, FINAL_SHA, commit list by phase (one line each: sha + what +
  why).
- Test count and route count BEFORE vs AFTER.
- For each phase: STATUS (fixed / finding-did-not-reproduce / deferred-to-
  OPEN_QUESTIONS / skipped-with-reason).
- Startup timing BEFORE vs AFTER (Phase 11) — concrete numbers.
- Any regression discovered during this round's own testing (be honest —
  if a fix broke something and had to be reverted, say so, including the
  revert commit).
- Explicit "WORSE" section — anything that got slower, more complex, or
  riskier as a tradeoff of a fix (e.g. new rate limiter could
  false-positive-block a legitimate power user; single-worker pin removes
  a scaling option that wasn't being used anyway — call these out plainly).
- Explicit "BETTER" section — concrete bug classes closed.
- Remaining P2 items, explicitly not touched, and why that's fine for now.
- Real-device tests still required (chat keyboard — deferred, separate
  round) before production rollout.

Deploy is AUTHORIZED after each individually verified fix in this round,
same as the previous autonomous round — commit, push, deploy per logical
fix via scripts/deploy.sh, verify its own health-check output before moving
to the next item. Do not deploy on top of a broken deploy — stop and
investigate first.

After Phase 21 and the final report are both written, STOP. Do not start a
22nd phase or revisit earlier phases speculatively.
