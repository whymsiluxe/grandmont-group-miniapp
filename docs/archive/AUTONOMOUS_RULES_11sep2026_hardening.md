> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# Autonomous Execution Rules — Final Production Hardening Round, 11.09.2026

Read `docs/PLAN_11sep2026_hardening.md` in full first. It is a 21-phase
production-hardening plan authored by an external reviewer (ChatGPT) reading
a source snapshot that may be stale. VERIFY each finding against the actual
current code before fixing it — do not blindly apply prose diffs.

This is SEPARATE from `docs/HANDOFF.md`/`docs/EXECUTION_STATE.txt`/
`docs/EXECUTION_PLAN.md` (older paused DailyPlan/Phase-A round,
STOPPED_FOR_REVIEW — do not touch) and separate from any chat-keyboard work
(that bug already got a targeted fix this session at commit def7711 — flex
padding-bottom WebKit scrollHeight bug fixed via ::after spacer; do NOT
touch chat keyboard geometry/state machine/composer transform again in this
round regardless of what Phase 13 in the plan says — Phase 13 explicitly
says skip it).

## Core mandate

Work through the 21 phases in order. Owner explicitly authorized: deploy
immediately after each individually verified fix (commit, push, deploy via
scripts/deploy.sh, verify its health-check output) — one logical fix per
commit, do not batch unrelated fixes into one deploy.

## Mandatory before starting

1. `git log --oneline -5` and `git status` in
   `/home/promonta/agent/miniapp-repo` — confirm HEAD is `def7711` (or
   later) and tree is clean.
2. Read `docs/PLAN_11sep2026_hardening.md` fully.
3. Test/route-count baseline:
   `PROMONTA_ENV=test /home/promonta/agent/miniapp/.venv/bin/python3 -m pytest tests/ -q`
   (672 passed, 1 skipped is the known-good baseline going in).
   Route count: `grep -oE '@(app|router)\.(get|post|put|delete|patch)' backend/main.py | wc -l`
   must stay 176 unless a phase deliberately and explicitly adds a route.

## Verify-before-fix discipline

Every phase in the plan starts with "VERIFY FIRST" or similar language for a
reason — the plan's file:line references and code snippets are from an
external read of the repo, not guaranteed current or accurate. For each
finding: read the actual current code at the claimed location. If it
matches, fix it. If it doesn't match (already fixed, wrong location, doesn't
exist, described inaccurately), write "finding did not reproduce: <why>" in
the final report for that phase and move on — do not force a fix onto code
that doesn't have the claimed problem.

## Real decision points

Genuine product/business decisions (not code questions) go to
`docs/OPEN_QUESTIONS_11sep2026.md` with full context. Keep working on
everything else that doesn't depend on the answer. Do not invent business
policy (e.g. Phase 4's "start without plan" behavior if no existing policy
is found).

## Quality bar

- Full test suite before and after each change. Never leave the repo broken
  between commits.
- Real commit messages: root cause found (or "finding verified as described"),
  what changed, test/route-count results.
- No --no-verify, no force-push, no amending existing commits.
- After each deploy: verify scripts/deploy.sh's own health-check passed
  (`GET https://app.promonta.fun/api/health -> 200` and SHA confirmed). If
  either is missing/failed, STOP and investigate before continuing.
- Do not touch chat keyboard code (Phase 13 = skip, already handled this
  session at def7711).
- Do not provision real staging infrastructure (Phase 17) or change GitHub
  repo visibility (Phase 15) — write these up as plans/action-items instead,
  per the phase text.

## Continuity — HANDOFF_11sep2026_hardening.md

Create and continuously update `docs/HANDOFF_11sep2026_hardening.md` — what's
done, in progress, left, and why for any non-obvious call.

## FINAL REPORT — owner explicitly wants before/after comparison

When all 21 phases are done (fixed / finding-did-not-reproduce / deferred /
skipped-with-reason), write the final comparison report as specified in the
plan's own "FINAL REPORT" section at the bottom of
`docs/PLAN_11sep2026_hardening.md` — this is not optional narrative, the
owner asked specifically for a concrete "what got better, what got worse"
comparison, not just a commit list. Put it in
`docs/HANDOFF_11sep2026_hardening.md` under a clear final heading. Include
START_SHA (def7711), FINAL_SHA, per-phase status, test/route counts
before/after, startup timing before/after (Phase 11), an explicit "WORSE"
section (regressions, tradeoffs, complexity added) and "BETTER" section
(bug classes closed), and remaining P2/deferred items.

## Stopping condition

After Phase 21 and the final report are both written, STOP. Do not start a
22nd phase, do not revisit earlier phases speculatively, do not start the
chat-keyboard live-device pass (separate round, owner's explicit call) or
the 2-tier-splash-adjacent visual redesign.

## No scope creep

Do not refactor unrelated code noticed while working. Do not touch files
belonging to the other paused DailyPlan/Phase-A round.
