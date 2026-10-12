# Grandmont Group Mini App

Telegram Mini App for Grandmont Group, a construction company operating in Chemnitz/Sachsen, Germany (legal name: Grandmont Group UG (haftungsbeschränkt) — official registration pending, not yet confirmed). Used internally by the owner and field workers to manage construction sites, worker assignments, shift check-in/out, absence requests, defect tickets, tool/equipment tracking, team chat, and a news/photo feed — all inside Telegram.

Live at: **https://app.promonta.fun/app.html** (Telegram WebApp, opens inside Telegram client). The GitHub repo is now `grandmont-group-miniapp`; the backend serving path and deployer have also migrated to the Grandmont naming (`/home/grandmont/...`). Some historical documentation may still mention pre-rebrand `promonta` paths and should not be treated as the current runtime source of truth.

## Status

Actively developed, in production. No formal versioning yet (pre-1.0). See [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md) for the current operational snapshot — that file is the first thing to read after any session loss.

## Roles

- **owner** — full access: manage objects, assign workers, approve absence requests, resolve critical alerts, see budgets/financials.
- **worker** — field access: see own assigned object, check in/out with GPS+photo, request absence, report defects/material needs, chat.

Role is looked up from `roles.json` by Telegram user ID; unknown IDs get a hard 403 (whitelist-only access since a "Фаза 10.1" hardening — not a silent worker default). See [docs/ROLES_AND_PERMISSIONS.md](docs/ROLES_AND_PERMISSIONS.md).

## Tech stack

- **Frontend**: vanilla HTML/CSS/JS, no build step, no framework, no bundler. Single-page shell (`app.html`) + per-feature JS modules loaded as plain `<script>` tags from `js/`. Telegram WebApp JS SDK for host integration (initData, haptics, theming, safe-area).
- **Backend**: Python 3.12 + FastAPI. `backend/main.py` is now the composition root (~2,985 lines on the monolith-reduction branch), while domain HTTP/business flows live under `backend/routes/` and shared runtime/storage helpers under `backend/core/`. Auth uses Telegram WebApp `initData` HMAC validation, plus a 12-hour backend session token (`POST /api/session`) issued after one initData check — no separate login system, no JWT library, no session cookies.
- **Data storage**: flat JSON files on disk (no database, no ORM, no migrations). See [docs/DATABASE.md](docs/DATABASE.md) for the full list of stores and known race-condition mitigations (atomic write + per-file locks). Critical stores (`checkin_meta.json`, `chat_messages.json`, `mangel_tickets.json`, and most others) write atomically (temp-file + `os.replace`) — see [docs/RELEASE_AUDIT.md](docs/RELEASE_AUDIT.md) for the full audit.
- **Shared modules** (`objekte_lib.py`, `mangel_lib.py`, `tools_lib.py`, `roadmap_lib.py`): historically these lived only on the VPS outside git. All four are now tracked in `backend/` and loaded via an isolated `importlib`-based loader (not a plain `import`) so `main.py` always resolves the repo-tracked version, not a possibly-stale copy on disk — a clean `git clone` + deploy reproduces working code for all of them (see RELEASE_AUDIT.md, marked historical, for the original untracked-module findings).
- **AI features**: Claude (via `CLAUDE_BIN` CLI bridge) and GLM as fallback, for chat assistant and voice-note transcript extraction (`faster-whisper` for STT).
- **PDF generation**: `pypdf`, for Angebot (quotes) and Rechnung (invoices).

Full breakdown: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository layout

```
miniapp-repo/
├── backend/                 FastAPI backend and runtime helpers
│   ├── main.py              composition root / remaining legacy orchestration
│   ├── routes/              extracted domain routers and domain orchestration
│   ├── core/                shared storage/runtime/config helpers
│   └── docs-source/         historical handoff docs kept for context, not current state
├── frontend/                Telegram Mini App frontend
│   ├── app.html             app shell, routing, role-based bottom nav
│   ├── angebot-tab.html, projects-tab.html, tools-tab.html   legacy standalone tab pages (status: see FEATURES.md)
│   └── js/                  one module per feature area
├── scripts/                 deploy/rollback/runtime manifest tooling
└── docs/                    project documentation
```

Runtime data (JSON stores, uploaded photos, generated PDFs, `.venv`, `node_modules`) lives on the VPS under `/home/grandmont/agent/miniapp/` and adjacent upload directories (`object_photos/`, `chat_attachments/`, `checkin_photos/`, etc.) — outside this repo, never committed, see `.gitignore`. It contains personal data of employees and must not leave the server without authorization. Full inventory: [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md).

## Local development

**There is no fully packaged local dev environment yet.** Development has historically happened against the production-oriented runtime, so deploy/test isolation is treated as a safety requirement. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/TODO.md](docs/TODO.md).

To run the backend locally:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# requires BOT_TOKEN and other vars from .env.example
# always point runtime data at an isolated directory
export MINIAPP_DATA_ROOT=/tmp/grandmont-miniapp-dev
uvicorn main:app --reload --port 8001
```

Frontend has no build step — open `frontend/app.html` directly, but it expects Telegram WebApp context (`window.Telegram.WebApp`) and will not fully function in a plain browser tab without stubbing that out.

## Checks

- **Automated tests**: `tests/*.py`, runnable through `pytest`. Current monolith-reduction validation: **1401 passed, 1 skipped**. The suite is offline: CI uses a dummy `BOT_TOKEN` and isolated `MINIAPP_DATA_ROOT`, with external integrations mocked/stubbed as appropriate. The one current skip is reported explicitly by CI (`test_owner_kt_requirements.py` needs a real assignment fixture).
- **CI**: `.github/workflows/ci.yml` runs Python syntax validation for `backend/*.py`, `backend/core/*.py`, `backend/routes/*.py` and tests, direct and package-style backend imports, the full pytest suite, JS syntax checks, required-file/merge-marker/secret guards, `npm ci`, and a pdfkit smoke check on every push/PR.
- **No full lint/typecheck gate yet** — Python has no repo-wide `ruff`/`mypy` enforcement, JS has no `eslint`/TypeScript gate. Syntax validation is handled by `py_compile`/`node --check`.
- Manual pre-deploy checklist: [docs/TESTING.md](docs/TESTING.md), release checklist: [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

## Git workflow

- `main` — stable, deployed state.
- `docs/...`, `fix/...`, `feat/...`, `chore/...` — work branches. See [docs/RELEASE_PROCESS.md](docs/RELEASE_PROCESS.md).
- Every functional commit should touch `docs/CHANGELOG.md`, `docs/PROJECT_STATE.md`, `docs/SESSION_HANDOFF.md`, and `docs/FEATURES.md` if applicable — see governance rules in [CLAUDE.md](CLAUDE.md).
- No force-push, no history rewriting without explicit owner approval.

## Recovering from a lost session

1. Read [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md) and [docs/SESSION_HANDOFF.md](docs/SESSION_HANDOFF.md) first.
2. Run `git log --oneline -20` and `git status` in this repo.
3. Compare against the live VPS state — the production frontend is served from `/var/www/miniapp/app.html` and backend from `/home/grandmont/agent/miniapp/`; this repo is the deploy source of truth. If live state has been changed manually, reconcile it before the next deploy.
4. Check `docs/TODO.md` for the next planned step.

## Health / diagnostics

- `GET /api/health` — unauthenticated liveness probe. Returns `status`, `service`, `version`, `commit`, `time`. No secrets, no internal paths. `version`/`commit` come from a `VERSION` file written by `scripts/deploy.sh` next to `main.py` on deploy (not a `git` subprocess on every request — the serving path isn't even a git repo).
- `GET /api/health/ready` — owner-only readiness check for internal monitoring. Cheap filesystem checks only (storage dirs writable, `tools_lib.py`/`mangel_lib.py` present, `roles.json` present) — never a live Google Sheets call on every poll.

## Deploy / rollback

- `scripts/deploy.sh` — full pre-flight (clean git status, `main` branch, full test suite, Python/JS syntax) → timestamped backup → manifest-driven backend deployment → frontend sync → `systemctl restart` → health checks. Backend runtime files are defined by `scripts/manifest.sh`; imported `core/` and `routes/` directories are deployed as units, so adding an extracted module does not rely on a manual copy step.
- `scripts/rollback.sh <backup-dir>` — validates and restores a backup created by `deploy.sh`, restarts the service, re-checks health. Never deletes the backup it used.
- `scripts/cleanup_rollback_backups.sh [keep_count]` — retention for `/tmp/rollback_backup_*` only (default: keep last 5). Does not touch user data — see [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md) for the actual data backup.

## Required environment variables

- `BOT_TOKEN` — Telegram bot token, used for `initData` HMAC validation. **Never commit a real value.**
- `MINIAPP_DATA_ROOT` — runtime data root. CI/dev must point this at an isolated directory; production uses the Grandmont serving data root.
- Full list, including optional AI-related vars: [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).

## Known limitations

- JSON/file locks are process-local; production must remain single-worker/single-instance until storage coordination is moved to a shared mechanism/database.
- No automated visual/UI testing — UI changes are verified by code review and manual Telegram testing rather than screenshot automation.
- Full data-deletion-on-request ("right to be forgotten") is not implemented — see [docs/DATA_PROTECTION.md](docs/DATA_PROTECTION.md).

## Secrets warning

This repo must never contain `BOT_TOKEN`, `GLM_KEY`, `WEBHOOK_SECRET`, real employee personal data, or any file under `backend/*.json` (all runtime data, gitignored). See [docs/SECURITY.md](docs/SECURITY.md) and [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).
