> **SUPERSEDED 2026-09-18** — historical snapshot, kept for context/git-blame only, not maintained. See [BACKLOG.md](BACKLOG.md) for current state.

# HANDOFF — Autonomous continuation, Phases 05-10

Read this first. Then read every file in `docs/plan-phases/05-*.md` through `10-*.md` in order — each is a self-contained phase brief with the owner's exact requirements. Phases 01-04 are already done (see their files for what was built, don't redo).

## Where you are

Repo: `/home/promonta/agent/miniapp-repo` (git, branch `main`).
Prod paths: backend `/home/promonta/agent/miniapp/main.py`, frontend `/var/www/miniapp/` (app.html + js/).
Service: `promonta-miniapp.service` (systemd) — backend. Caddy serves frontend directly, no restart needed for frontend-only changes.

**Workflow that was used for phases 01-04, keep using it:**
1. Read the relevant phase file in `docs/plan-phases/`.
2. Verify current code state by reading it — don't trust the plan text alone, it was written before implementation started and is sometimes wrong (e.g. it claimed Needs was "just a stub" when a full CRUD already existed).
3. Make small, focused changes. One logical change per commit.
4. After every backend change: `python3 -m py_compile backend/main.py` (in the repo) before committing.
5. After every frontend JS change: `node --check <file>`.
6. Commit to the git repo (`main`) with a clear message explaining what changed and why, using `git commit -F <message-file>` (heredocs with embedded quotes break over SSH — always write the message to a file first).
7. **Also deploy to prod** — this session (unlike earlier ones) has been deploying continuously so the owner can verify fixes live on his phone. Copy the changed file(s) to their prod path, restart `promonta-miniapp.service` only for backend changes, and back up the file being overwritten first (`cp target target.bak-pre-<label>-$(date +%Y%m%d-%H%M%S)`).

   **Important permission note (found 28.07.2026, see memory `project_miniapp_server_permissions`)**: the `promonta` user (which is what you're running as in this autonomous mode) cannot write to `/var/www/miniapp/` directly and cannot restart the systemd service directly -- both require root. A narrow passwordless sudo rule was set up specifically for this:
   ```
   sudo -n /bin/cp <source> /var/www/miniapp/<dest>
   sudo -n /bin/chown root:root /var/www/miniapp/<dest>
   sudo -n /bin/systemctl restart promonta-miniapp.service
   ```
   These three sudo invocations are the ONLY root actions available to you -- don't attempt `sudo -u promonta` wrapping (you're already promonta) or try other sudo commands, they will hang waiting for a password that will never come (`-n` makes it fail fast instead if the rule doesn't cover it -- if a cp/chown/restart call errors with sudo asking for a password, the path or command didn't match the sudoers rule exactly, check for typos before assuming something else is wrong).
   For the backend main.py: after `python3 -m py_compile`, `cp backend/main.py /home/promonta/agent/miniapp/main.py` (this path IS writable by promonta directly, no sudo needed) then `sudo -n /bin/systemctl restart promonta-miniapp.service`.
   For frontend files: `sudo -n /bin/cp frontend/app.html /var/www/miniapp/app.html` (or the relevant js/ file) then `sudo -n /bin/chown root:root /var/www/miniapp/<file>` -- no service restart needed for frontend-only changes, Caddy serves the static file directly.
8. After finishing a phase's checklist items, edit that phase's file in `docs/plan-phases/` to mark each item's status (FIXED / CONFIRMED-not-a-gap / deferred-with-reason), matching the style already used in files 01-04. Commit that doc update separately from the code commit.

## Critical rules (violating these has caused real damage this session already)

- **Never deploy without verifying compile/syntax first.** A magic-bytes fix once made the whole backend service crash-loop because `python-magic` wasn't installed in the actual venv the systemd service uses (`/home/promonta/agent/miniapp/.venv/bin/pip`, NOT the system python3). If you add a new Python dependency, install it into that exact venv and add it to `backend/requirements.txt`.
- **Some shared modules live outside the git repo** — e.g. `/home/promonta/agent/mangel_lib.py` is imported by `main.py` via `sys.path.insert(0, '/home/promonta/agent')` but is not version-controlled in `miniapp-repo`. If you need to touch a file like this, back it up first (`cp x x.bak-pre-<label>-<timestamp>`) and treat it as a direct prod edit — no git history for it, be extra careful.
- **CSP lives in `/etc/caddy/Caddyfile`**, not in the repo. If you add any new external resource (fonts, media, scripts, API calls to third-party domains), you likely need to update the CSP there too, or it will silently fail in the browser with no error the user can see except "doesn't work." Validate with `caddy validate --config /etc/caddy/Caddyfile` before `systemctl reload caddy`.
- **Bubble Assignment must be preserved**, not replaced with a plain table — this was explicit owner instruction from the original master ТЗ.
- **DSGVO/legal compliance is out of scope** — don't build consent flows, data retention policies, etc.
- Don't migrate to React, a new database, or microservices without asking first — this is deliberately staying a vanilla-JS + FastAPI + flat-JSON-file monolith per the owner's explicit constraints in the original brief.
- The owner communicates in Russian, often in caveman-mode-style terse fragments, sometimes frustrated/using profanity when something breaks visually on his real device — that's not directed at code quality, it's real-time UI feedback from testing on his phone. Take it as signal, fix the concrete thing, don't get thrown off tone.
- **Never claim something is "done" or "fixed" without verifying against actual code** — the phase files already document several places where the original owner spec was flat wrong about current state (e.g. Needs, min-2-photos on finish shift). Always check reality first.

## What's already done (phases 01-04, don't redo)

- Phase 01: Full security pass — object-level permissions, geo-required on shift start/finish, XSS escaping across all 20 files with innerHTML, magic-byte upload validation, JSON storage hardening, path traversal fixes, CSV injection fix, last-owner protection, AI subprocess concurrency guard. All committed, all deployed.
- Phase 02: Start-shift stage picker, Active Shift panel (timer/GPS/quick actions), full 6-step finish-shift wizard with voice input, owner dashboard "who's working/who hasn't started" blocks, Object Info tab Team+Shifts section. All committed, all deployed.
- Phase 03: Needs got category field + expanded statuses, Mangel got expanded statuses (this one was a direct prod edit to `/home/promonta/agent/mangel_lib.py`, see above), a real navigation bug fixed (`loadTasks` name collision between objects.js and tasks.js), chat re-render logic hardened against a stale-count bug. All committed, all deployed.
- Phase 04 (in progress, not finished): Telegram safe-area horizontal compensation added (`--tg-safe-right`/`--tg-safe-left`, +10px margin), header title now truly centered via CSS Grid (was flex-based and got pushed off-center by asymmetric siblings), stale `margin-left:-10px` removed from back buttons (was pushing them under Telegram's Close button), root-route back arrows removed from Chat and Calendar screens (they're root tabs, should never have had one), a CSP bug found and fixed (radio player broke because `media-src` was missing from the CSP header in Caddyfile, blocking the external audio stream — this was a real regression from the 25.07 radio refactor, not a code bug in the JS itself).

**Still open in Phase 04** (the owner sent a second, more detailed navigation/FAB brief mid-session — read `docs/plan-phases/04-telegram-ui-navigation.md` for the original C1-C10 items, but the LATEST owner message, which should take priority, asked for):
1. Move the Objects screen's "+" add-object button from the top-right header (where it currently overlaps the Telegram menu button) down to a proper Floating Action Button (FAB) above the bottom navigation — forest-green squircle, ivory plus icon, positioned using real measured heights (`--app-bottom-nav-height`, safe-bottom, radio-mini-player height if visible), not magic pixel offsets.
2. A `TelegramTitleHeader`-style unified approach so every root screen's title sits centered between Telegram's system buttons (mostly done via the grid fix, but verify against the owner's exact examples in his message: Home/Чат/Объекты/Календарь/Профиль).
3. Root vs nested route classification made explicit (root = Home/Chat/Objects/Calendar/Profile with no custom back ever; nested = Object Detail/Personal Chat/Object Chat/Defect Chat/Photo Viewer/etc, using Telegram's native BackButton, never both at once).
4. A proper fallback back-button component for when the app runs outside Telegram (browser/dev preview) — SVG chevron-left, not the old `←` text glyph, soft squircle not a heavy circle.
5. The object-creation flow should open in a managed bottom sheet when the FAB is tapped, not a form appearing inline in a random spot.
6. Tests (unit + Playwright) covering the navigation/FAB behavior described above — this codebase currently has zero test infrastructure, so this would be new (see phase 10 for the fuller test-infrastructure plan; a minimal smoke test here is enough, don't build a whole framework just for this).

Do NOT re-read the owner's full pasted brief from the conversation history — it's long and mostly redundant with the summary above. The summary captures everything that matters. If you need exact pixel/animation values, they were: FAB size 58-62px, squircle border-radius 20-22px, forest-green background, ivory plus icon 24-28px, soft shadow, press scale 0.92, appear animation opacity+translateY+scale over 200-260ms, no bounce.

## Phase order (05 through 10)

Finish Phase 04's remaining items first (the FAB + route classification work above), mark it done in `docs/plan-phases/04-telegram-ui-navigation.md`, then proceed through:

- **05**: `docs/plan-phases/05-design-system.md` — design tokens, typography, Home/Profile/Calendar/Bubble Assignment polish.
- **06**: `docs/plan-phases/06-chat-hub-rebuild.md` — full Chat Hub rebuild (dark theme, expandable search, worker strip, 4 tabs, direct threads, reactions). This is the single largest remaining item in the whole plan — treat it as its own multi-session project within your autonomous run, don't rush it.
- **07**: `docs/plan-phases/07-object-card-rebuild.md` — Object Card rebuild matching the ski-resort reference composition (hero photo, weather island, avatar overlap, status pill, stages strip).
- **08**: `docs/plan-phases/08-radio-player-rebuild.md` — mostly already done in a prior session (HomeRadioPlayer/RadioMiniPlayer exist and now work after the CSP fix), read the file to see what's left, likely just polish/tests.
- **09**: `docs/plan-phases/09-architecture-split.md` — backend/frontend modular split, API client, offline queue. This is invasive (touches main.py structure broadly) — be extra careful with small commits and compile checks at every step, this file monolith (4000+ lines) has been stable, don't break it.
- **10**: `docs/plan-phases/10-tests-docs-final.md` — test infrastructure, E2E flows, docs updates, endpoint audit table.

## How to keep going across context/session limits

You are being run in a loop (see the systemd timer/script that invoked you). Each invocation:
1. Re-read this file and the current phase file's status.
2. Continue from wherever the status markers show you left off — the phase files ARE the resumable state, written in the same style you've seen in 01-04 (status: FIXED/CONFIRMED/deferred, with commit hashes).
3. Before ending your turn (whether you hit a natural stopping point or you're running low on context), make sure whatever you were mid-way through is either: (a) committed in a working, compiling state, or (b) explicitly noted as in-progress in the phase file with enough detail that the next invocation can pick it up cleanly. Never leave uncommitted work that would be lost.
4. Do not wait for user confirmation on routine small fixes — you have been granted autonomous authority for this specific handoff (phases 05-10, following the established patterns from 01-04). Still avoid genuinely destructive/irreversible actions (force-push, dropping data, deleting without a backup) without stopping to flag it in the phase file as a blocker instead of proceeding.
5. If you get stuck on something that genuinely needs the owner's judgment (a design decision with no clear answer in the phase files, a security tradeoff, something that would change the DEPLOY state in a risky way you're not confident about), write it clearly into the phase file under a "BLOCKED — needs owner input" heading and move on to the next actionable item rather than stalling.
