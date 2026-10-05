# Session handoff — current

Short, current-only. Updated 2026-10-05. Do not append history here: when a session ends, replace the sections below with the new current state and move anything worth keeping into `docs/CHANGELOG.md` or `docs/archive/`.

Full historical handoff entries (2026-09 through 2026-10-04, about 900 lines) are in [archive/SESSION_HANDOFF_history_through_2026-10-04.md](archive/SESSION_HANDOFF_history_through_2026-10-04.md).

## Where to look first

- Deployed state: the live `/api/health` endpoint's `commit` field (this repo's `main` can be ahead of production by docs/CI-only commits).
- What is built and its status: `docs/PROJECT_STATE.md`, `docs/CURRENT_STATE.md`, `docs/FEATURES.md`.
- Open work: `docs/TODO.md`, `docs/BACKLOG.md`, `docs/OPEN_QUESTIONS.md`.
- Decisions: `docs/DECISIONS.md`. Ownership across Core/CRM/Miniapp: `docs/DOMAIN_OWNERSHIP.md`.

## In progress

Nothing recorded as in progress at this reset. The last archived entry (2026-09-25) was the Stages/Roadmap router extraction (`backend/routes/stages.py`, merged); verify with `git log` rather than trusting this line.

## Rules for the next session

- Start with `git status`, `git log --oneline -10`, and check the local clone is current with `origin/main`.
- Production deploys only with explicit owner approval, via `scripts/deploy.sh`.
- Keep this file under about 80 lines.
