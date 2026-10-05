> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# TASK — Master Plan Round 0 + Round 1 (Foundation Bug Fixes)

Execute ONLY Round 0 (baseline/discovery/freeze) and Round 1 (foundation bug fixes)
from docs/MASTER_ARCHITECTURE_PLAN.md, which is the full text of the owner's master
technical plan. Read that file first, in full.

STOP after Round 1 — do NOT begin Round 2 (main.py extraction) automatically. Round 2
requires separate owner review and approval first.

## Already done before you start (do not redo)

Two P0 items from Round 1 §1 (multi-worker DailyPlan acceptance) are ALREADY FIXED and
pushed to origin/main as of commit 1628201:
- HTTP endpoint no longer duplicates the status gate (accept_plan() library function
  is the single source of truth for which statuses allow acceptance).
- Frontend _shouldShowPlanScreen() allows published/amendment_pending/accepted/in_progress,
  gated on the worker's own acceptance record, not shared plan status.
- get_today_plan_for_worker() STATUS_PRIORITY now includes in_progress.
- Regression tests added for both the "second worker after first accepted" and
  "second worker after first finished (in_progress)" scenarios.

Verify this is still true (git log, read the actual current code) before assuming
§1 is fully closed — do not blindly trust this note over the real code state. If you
find §1 is NOT actually fully closed by the current code, fix what's missing.

Round 1 items §2 through §14 are NOT yet done — execute them for real, per the full
plan text in docs/MASTER_ARCHITECTURE_PLAN.md.

## Cost-consciousness — read this before doing anything else — MAXIMUM TOKEN ECONOMY

The owner has explicitly asked to run this "as economically as possible" — a previous
audit session burned significant account usage running in a tight retry loop with
near-zero output. Owner has repeated the economy demand again — treat token spend as
the primary constraint, correctness second only to that. Apply these constraints:

0. **Test tiering is mandatory, not optional**: per-item, run ONLY the focused/
   targeted test(s) for that item (`pytest tests/test_round1_foundation_fixes.py -k
   item_N` or the specific relevant existing test file) plus `py_compile`/`node
   --check` on changed files. Run the FULL suite ONLY after item 6, after item 10,
   after item 14, and at the final gate — 4 full runs total, not 14. Do not re-read
   files already covered by the evidence table in docs/MASTER_ARCHITECTURE_PLAN.md —
   that table already has the exact file:line locations, go straight there.

1. Work efficiently: read only the files/functions the plan's Round 1 items actually
   name, not the whole codebase. Use targeted grep/read, not exhaustive re-reading of
   files already covered by recent sessions (main.py, daily_plan_lib.py structure is
   already well understood from recent work — verify specific claims, don't re-survey
   everything from scratch).
2. Do not loop on a single failing test or blocked item more than twice. If stuck,
   write it to docs/HANDOFF.md under a BLOCKED heading with what you tried, and move
   to the next Round 1 item instead of burning further attempts.
3. If you hit the session/usage limit mid-work: stop cleanly, leave docs/HANDOFF.md
   accurate about exact progress (which Round 1 items done/in-progress/not-started),
   and end the process. Do NOT rely on an aggressive short-interval watchdog to retry
   every few minutes — that pattern already wasted a full day of usage with zero
   output in a prior session. A human will check back and manually resume.

## Master safety rules (already stated in the plan, restated for emphasis)

- Full existing test suite (currently 603 passing, 1 skipped — do not hardcode this
  exact number, just require the full suite green) must pass after every change.
- Add regression tests for every fixed bug.
- Do NOT combine architecture extraction + business logic rewrite + database
  migration in one commit — Round 1 is bug fixes only, no main.py splitting.
- No production deploy. No modification of live production Google Sheets without
  explicit owner approval. No systemd config changes without explicit owner approval.
- Record START_SHA before starting, report FINAL_SHA at the end.
- One item = one commit (per the plan's "Fix plan per item" section — items 2-14,
  14 items, 14 commits, strict granularity even for logically-coupled items like
  7+8+9+10).
- **`git push origin main` immediately after EVERY single commit — not batched, not
  at the end. Push right after each item's commit lands, before moving to the next
  item.** This is cheap (no API cost) and means work already done survives even if
  you get cut off mid-round.
- Do NOT deploy (deploy.sh is separate from push — push to git only).
- STOP FOR REVIEW at the end with the report format specified in the plan
  ("REPORT FORMAT AFTER EACH ROUND" section).

Write continuous progress to docs/HANDOFF.md as you go (existing pattern in this repo).
When Round 1 is fully done (or you've made a good-faith pass through all 14 items,
with any genuinely blocked ones clearly documented), write the final round report to
docs/HANDOFF.md and stop the process — do not self-restart, do not begin Round 2.
