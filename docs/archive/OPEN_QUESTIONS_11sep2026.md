> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for current state.

# Open Questions — Production Hardening Round 11.09.2026

Items requiring an owner decision before implementation. Kept separate from
`docs/HANDOFF_11sep2026_hardening.md` (which tracks status) to avoid mixing
product/policy decisions with engineering status.

---

## Q1 — External create_object scripts (Phase 1)

**Finding:** `backend/main.py` (lines ~3557 and ~3575) shells out to:
- `/home/promonta/agent/create_object.py`
- `/home/promonta/agent/create_object_folder.py`

Both files exist at `/home/promonta/agent/` (outside this git repo, owned by root,
file sizes 4311 and 2523 bytes). They are called by the `POST /api/objects` endpoint
when creating a new object.

**Problem:** A fresh clone of this repo + clean VPS deployment would fail silently
on object creation because these scripts don't exist in the repo. There's also no
deploy/rollback tracking for them.

**Options:**
1. Move both files into `scripts/` or `backend/` in this repo, add to manifest.
2. Confirm they are vestigial (object creation works without them, some fallback
   exists, or the endpoint is never actually called in practice) and add a comment.
3. Keep them external but document the manual copy step in `docs/DEPLOYMENT.md`.

**Pending:** Owner decision on which option to take. No code changed here.

---

## Q2 — CI workflow file root-owned (Phase 2)

**Finding:** `.github/workflows/ci.yml` is owned by root (not promonta), so it cannot
be edited by the promonta user during this autonomous round. Several needed CI fixes
were identified in Phase 2:

**Required CI changes (apply manually after `sudo chown promonta .github/workflows/ci.yml`):**

1. **Python syntax check** — must cover `backend/core/*.py` (currently only `backend/*.py`):
   ```yaml
   - name: Python syntax check (backend/*.py, backend/core/*.py, tests/*.py)
     run: |
       python -m py_compile backend/*.py backend/core/*.py tests/*.py
   ```

2. **Package import smoke test** — must copy `backend/core/` into the temp package dir
   (currently omitted; `from .core.paths import ...` would fail at CI import time):
   ```yaml
   rm -rf /tmp/ci-pkgtest && mkdir -p /tmp/ci-pkgtest/miniapp
   cp backend/*.py /tmp/ci-pkgtest/miniapp/
   cp -r backend/core /tmp/ci-pkgtest/miniapp/    # ← ADD THIS LINE
   ```

3. **Required files check** — add `daily_plan_lib.py` and `scripts/manifest.sh`:
   ```yaml
   test -f backend/daily_plan_lib.py || (echo 'backend/daily_plan_lib.py missing' && exit 1)
   test -f scripts/manifest.sh || (echo 'scripts/manifest.sh missing' && exit 1)
   ```

4. **Shell syntax check** — add manifest.sh:
   ```yaml
   bash -n scripts/manifest.sh
   ```

5. **JS syntax check** — add subdirs (Phase 2 completeness):
   ```yaml
   for f in frontend/js/core/*.js frontend/js/components/*.js; do
     [[ -f "$f" ]] && node --check "$f" || true
   done
   ```

**Also note:** GitHub Actions run status could not be verified — `gh` CLI is not
authenticated on this box. Cannot confirm whether recent CI runs are actually passing.
Label in final report as "local pytest only, GitHub Actions status unknown."

---

## Q3 — "Start without plan" behavior (Phase 4)

**Finding:** The existing codebase has a defined policy for checkin Start when no
DailyPlan exists for the worker+object+date: the check-in proceeds without a plan
(no blocking). Whether this is intentional product behavior or a gap to close is
a product decision.

**Current behavior:** Verified in `daily_plan_lib.py` — if no plan is found, start
proceeds without daily_plan validation. This is documented in the existing codebase.

**Owner needs to decide:** Should a worker be able to start work without a published
DailyPlan for that day, or should it be blocked with a reason field? Current behavior
(allow start without plan) is documented but not explicitly enforced as policy.

See Phase 4 notes in `docs/HANDOFF_11sep2026_hardening.md` for implementation context.

---

## Q1 RESOLVED (2026-09-17, commit befc962)

Both scripts moved into `backend/`, tracked in `scripts/manifest.sh`
(`BACKEND_SUBPROCESS_SCRIPTS`), wired through `scripts/runtime_manifest.sh`
so backup/deploy/rollback/syntax-check all cover them. `sys.path.insert`
changed from the hardcoded `/home/promonta/agent` to a repo-relative path.
Side-effect finding: the external `objekte_lib.py` these scripts imported had
diverged from the repo copy (stale budget-percent column name, real crash
bug) — synced and `promonta-bot.service` restarted to pick up the fix for
the 6 other external scripts that also import that module.
