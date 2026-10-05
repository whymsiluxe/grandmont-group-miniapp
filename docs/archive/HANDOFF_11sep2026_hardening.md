> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# Handoff — Final Production Hardening Round, 11.09.2026

Governing documents: `docs/AUTONOMOUS_RULES_11sep2026_hardening.md` and
`docs/PLAN_11sep2026_hardening.md`.

START_SHA: `def7711` (plan baseline); actual starting HEAD: `9826f75`
(one commit ahead — docs commit, tree clean).

Baseline: 176 routes, 672 passed + 1 skipped (pre-round).

---

## Phase Status

| # | Phase | Status | Notes |
|---|-------|--------|-------|
| 0 | Baseline | ✅ done | Recorded below |
| 1 | Deploy/Rollback Artifact Integrity | ✅ done | commit below |
| 2 | Make CI Real | ⚠️ partial | CI file root-owned; code fixes done except ci.yml edit; see OPEN_QUESTIONS Q2 |
| 3 | Destructive Chat Delete Bug | 🔲 pending | |
| 4 | Assignment→DailyPlan→CheckIn Invariant | 🔲 pending | |
| 5 | JSON Data Integrity | 🔲 pending | |
| 6 | Cross-Process Storage Safety | 🔲 pending | |
| 7 | Critical Store Policy | 🔲 pending | |
| 8 | Audit Trail | 🔲 pending | |
| 9 | Business Date Consistency | 🔲 pending | |
| 10 | Dashboard Stable ID Logic | 🔲 pending | |
| 11 | Startup Two-Tier Bootstrap | 🔲 pending | |
| 12 | Frontend Request Reliability | 🔲 pending | |
| 13 | Chat keyboard | ⏭️ SKIP | Handled at def7711 per explicit owner instruction |
| 14 | Permission Matrix | 🔲 pending | |
| 15 | Security Hardening | 🔲 pending | |
| 16 | Retention/Privacy Operations | 🔲 pending | |
| 17 | Staging/Operations | 🔲 pending | |
| 18 | Backup/Restore Drill | 🔲 pending | |
| 19 | Owner↔Worker Full E2E | 🔲 pending | |
| 20 | Frontend Safety/XSS | 🔲 pending | |
| 21 | Documentation Truth | 🔲 pending | |

---

## Phase 0 — Baseline

- HEAD: `9826f75`, branch: main, tree: clean
- Routes: 176
- Tests: 672 passed + 1 skipped (baseline before any Phase 1 changes)
- Backend .py files: main.py + 8 libs (tools_lib, mangel_lib, objekte_lib,
  roadmap_lib, work_types, profile_skills, assignment_matching, daily_plan_lib)
  + core/ (constants.py, limits.py, paths.py, storage.py, telegram.py, time.py)
- Frontend JS: 27 files in frontend/js/ plus core/radio-controller.js and
  components/radio-player.js
- External scripts (outside repo): /home/promonta/agent/create_object.py (4311B),
  /home/promonta/agent/create_object_folder.py (2523B) — see OPEN_QUESTIONS Q1
- CI status: local pytest verified; GitHub Actions status UNKNOWN (gh not authenticated)
- Startup timing before Phase 11: not yet measured

---

## Phase 1 — Deploy/Rollback Artifact Integrity ✅

**Finding confirmed:** `daily_plan_lib.py` imported by `main.py` (lines 126+131 via
try/except relative+absolute import) was NOT in `deploy.sh`'s copy list. If
`daily_plan_lib.py` changed in the repo, the production version would silently stay
stale. Also: `backend/core/` was not backed up by `deploy.sh`'s backup step and not
restored by `rollback.sh` — a rollback after a core/ update would leave mismatched
`core/` in production.

**What changed:**
- `scripts/manifest.sh` (NEW): single source of truth for BACKEND_PY_LIBS,
  BACKEND_JS_FILES, BACKEND_CORE_DIR. Sourced by deploy.sh and rollback.sh.
- `scripts/deploy.sh`: sources manifest.sh; backup step now uses BACKEND_PY_LIBS loop
  (all libs get ABSENT-marker handling); copy step uses manifest loop; py_compile uses
  loop + core/*.py; step 4 syntax check now covers backend/core/*.py.
- `scripts/rollback.sh`: sources manifest.sh; syntax check and restore both use manifest
  loops; core/ is now backed up and restored.
- `tests/test_deploy_manifest.py` (NEW): 12 tests — manifest completeness, daily_plan_lib
  present, all listed files exist and compile, deploy/rollback round-trip hash test,
  ABSENT-marker test for new-module rollback.
- `tests/test_assignment_lifecycle.py`: updated `DeployRollbackNewModulesTests` to verify
  via manifest.sh instead of per-file string search (per the manifest refactor).
- `docs/OPEN_QUESTIONS_11sep2026.md` (NEW): Q1 (external create_object scripts), Q2
  (CI file root-owned + needed changes), Q3 (start-without-plan policy).

**CI note (Phase 2 overlap):** `.github/workflows/ci.yml` is root-owned (cannot write
without sudo). Needed changes documented in `docs/OPEN_QUESTIONS_11sep2026.md` Q2.
Manual owner action required: `sudo chown promonta .github/workflows/ci.yml` then apply
the 5 edits from Q2.

**Tests after:** 684 passed (+12 new), 1 skipped, 0 failed. Routes: 176.

---

## Phase 2 — Make CI Real ⚠️ partial

CI file root-owned — cannot apply directly. Required changes documented in
`docs/OPEN_QUESTIONS_11sep2026.md` Q2. All other CI-independent code changes from
Phase 1 are done (syntax check in deploy.sh now covers core/*.py).

Cannot verify GitHub Actions run status — `gh` CLI not authenticated on this box.

---

## FINAL REPORT

*Will be filled in after Phase 21.*

START_SHA: def7711 / 9826f75 (HEAD at round start)
FINAL_SHA: TBD

### Per-phase status
*(see Phase Status table above)*

### Test counts
- Before: 672 passed, 1 skipped
- After Phase 1: 684 passed (+12), 1 skipped

### Route count
- Before: 176
- After Phase 1: 176

### Startup timing (Phase 11)
- Before: TBD (not yet measured)
- After Phase 11: TBD

### BETTER (bug classes closed)
- Phase 1: `daily_plan_lib.py` now deployed on every `deploy.sh` run — silent stale-version bug closed
- Phase 1: `core/` now backed up and restored on rollback — silent mismatch after core/ update + rollback closed
- Phase 1: Manifest-driven deploy/rollback — adding future libs to manifest.sh automatically propagates to backup/copy/rollback/test

### WORSE (regressions, tradeoffs, complexity added)
*(none yet)*

### Remaining P2/deferred
- CI file root-owned (manual chown needed before applying Q2 fixes)
- External create_object*.py scripts need owner decision (Q1)
- Start-without-plan policy needs owner decision (Q3)
- Chat keyboard live-device pass: explicitly deferred per owner (separate round)
