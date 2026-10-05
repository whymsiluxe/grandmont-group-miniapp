# Domain Ownership — Grandmont Group Miniapp

| Domain | Canonical owner | Miniapp's role |
|---|---|---|
| Worker identity (`telegram_user_id` ↔ Worker UUID) | **Core** | Read-only consumer. Never creates/mutates identity locally; `telegram_user_id` stays the canonical Miniapp-facing key (see 2026-10-04 bot migration — identity never changed across the token cutover). |
| Shifts, DailyPlan, check-in/out, absence, chat, tasks | **Miniapp** | Source of truth. CRM reads this through `miniapp_client.py`, never writes to it directly. |
| Clients, leads, commercial workflow, documents | **CRM** | Miniapp has no opinion on this; does not store or expose it. |
| Organizations, projects, stages | **Core** | Miniapp has no opinion on this. |

Full cross-project map: `~/grandmont-dev-os/registry/capabilities.yaml`, `~/grandmont-dev-os/adr/001-core-canonical-identity.md`, `~/grandmont-dev-os/adr/002-miniapp-shift-source-of-truth.md`.

## Note on docs/ organization

This repo's `docs/` historically accumulated many dated point-in-time files (PLAN_*, HANDOFF_*, OPEN_QUESTIONS_*, etc.). As of 2026-10-04, the clearly-superseded dated snapshots were moved to `docs/archive/` (Engineering OS plan item 27) — `BACKLOG.md` is the living backlog (already does this consolidation job, see its own header), `CURRENT_STATE.md`/`SECURITY.md`/`ARCHITECTURE.md`/`TESTING.md`/`TODO.md` remain the living docs at the top level.
