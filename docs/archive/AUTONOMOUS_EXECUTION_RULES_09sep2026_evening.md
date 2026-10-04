> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# Autonomous Execution Rules — UI Fix Round continuation, 09.09.2026 evening

Owner is going to sleep, asked to finish remaining items from today's Functional UI
Fix Round autonomously. Read `docs/PLAN_09sep2026_evening.md` in full first — it is
the complete list of what's left, in priority order, with file:line pointers and
exactly what's already been tried and failed (so you don't repeat blind guesses).

This is a SEPARATE, UNRELATED effort from `docs/HANDOFF.md`/`docs/EXECUTION_STATE.txt`/
`docs/EXECUTION_PLAN.md` (those belong to an older, paused DailyPlan/Phase-A
architecture round — currently STOPPED_FOR_REVIEW, do not touch, do not resume it,
do not read its state machine as if it applies here). This file and
`docs/PLAN_09sep2026_evening.md` are the only two files governing tonight's work.

## Core mandate

Work through `docs/PLAN_09sep2026_evening.md` in priority order (1 → 2 → 3). For each
item: diagnose for real (live device inspection is not available while owner sleeps —
read the code carefully, reproduce the described symptom in reasoning from the actual
code path, do not guess-and-deploy blind on a bug this session already failed to fix
blindly more than once — those items are flagged in the plan). Fix. Test. Commit
(one logical fix per commit, real commit message explaining root cause found, not just
"fix bug"). Deploy after each individually-verified fix — **owner explicitly said
"довести до конца и задеплой и запуш" — deploy IS authorized for this round**, unlike
the other paused round's file above. Do not batch multiple unrelated fixes into one
deploy if they were committed separately.

## Mandatory before touching anything

1. `git log --oneline -10` and `git status` in `/home/promonta/agent/miniapp-repo` —
   confirm HEAD is `7d0d258` (or later, if this is a resumed run) and tree is clean.
2. Read `docs/PLAN_09sep2026_evening.md` fully.
3. Before any pytest run: `PROMONTA_ENV=test python3 -m pytest tests/ -q` (matches
   how every fix this session was verified — see git log messages for the pattern).
   Route count check: `grep -oE '@(app|router)\.(get|post|put|delete|patch)'
   backend/main.py | wc -l` must stay 176 unless you deliberately add a new endpoint
   (only plan item 5 might need one — check first if an existing status-change
   endpoint can be reused before adding a new one).

## Diagnosis without live device access

Several items in the plan (composer lag, team-add popup delay, task-note textarea lag)
were previously "fixed" based on code reading alone and turned out wrong or incomplete
on live device retest — this is documented in the plan and in git log (commits 285d925,
d613088, 46fc7eb, 74b9e6f as a cautionary sequence). Tonight you have NO live device to
verify against. For items where the plan flags "requires live diagnosis" and no owner
is available:
- Prefer the more conservative, well-understood fix over a clever one you can't verify
  (e.g. for composer: if in doubt, a plain instant snap with no animation is safer than
  another animation attempt — it was explicitly named as the fallback in the plan).
- If a fix cannot be verified with reasonable confidence from code alone, do NOT deploy
  it speculatively — commit and push, note in HANDOFF why it wasn't deployed, and move
  to the next item. Owner can verify and request deploy in the morning.
- Never repeat the exact same category of blind fix a fourth time on the same bug
  (composer keyboard timing already had 4 attempts this session) without new evidence.

## Real decision points

If something requires an actual product/business decision (not a code question) —
write it to `docs/OPEN_QUESTIONS_09sep2026.md` with full context, keep working on
everything else that doesn't depend on the answer. Do not invent business rules
(e.g. what exactly counts as "duplicate" in the worker profile screens, item 6 in
the plan) — that one explicitly says "уточнить у owner", don't guess and ship it.

## Quality bar

- Full test suite before and after each change. Never leave the repo broken between
  commits.
- Real commit messages: root cause found, what was tried before and why it failed
  (if applicable), what changed, test/route-count results — matching every commit
  from today's session (see `git log` for the exact style/depth expected).
- No `--no-verify`, no force-push, no amending existing commits.
- After each deploy: verify `scripts/deploy.sh`'s own health-check passed (it prints
  `GET https://app.promonta.fun/api/health -> 200` and `deployed SHA подтверждён` —
  if either is missing/failed, STOP and investigate before continuing to the next item,
  do not deploy on top of a broken deploy).

## Continuity — HANDOFF_09sep2026_evening.md

Create and continuously update `docs/HANDOFF_09sep2026_evening.md` (a NEW file, do not
touch the other round's `docs/HANDOFF.md`) — what's done, what's in progress, what's
left, and why for any non-obvious call. This is what the owner or a resumed session
reads in the morning.

## Stopping condition

When all of `docs/PLAN_09sep2026_evening.md`'s items are either done+deployed, or
explicitly deferred to `docs/OPEN_QUESTIONS_09sep2026.md` with a clear reason, write a
final summary to `docs/HANDOFF_09sep2026_evening.md` titled "READY FOR OWNER REVIEW —
09.09.2026 evening autonomous run" listing: what shipped (commit SHAs + one-line
description each), what's deployed vs. committed-only, what's still open and why,
current production SHA. Then stop — no state-machine file to flip, this round doesn't
use one; the presence of that final HANDOFF section IS the stop signal for whoever
reads it next.

## No scope creep

Do not start the 2-tier splash rewrite or any other item explicitly marked "не
трогать без owner" in the plan. Do not touch the other (DailyPlan/Phase A) round's
files. Do not refactor unrelated code you happen to notice while working.
