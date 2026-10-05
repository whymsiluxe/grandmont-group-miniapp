---
paths: ["backend/**"]
---

# Miniapp backend rules

- `telegram_user_id` is the canonical Miniapp-facing identity key — never introduce a second identity field or resolve identity by name-matching (see Dev OS `adr/001-core-canonical-identity-owner.md`).
- Telegram auth lives in `backend/core/telegram.py` and `backend/core/permissions.py` (`validate_init_data` / `create_session_token` / `verify_session_token`). Read `docs/DECISIONS.md`'s 2026-10-04 bot migration entry before touching either: the dual-bot verify fallback there is deliberate, not legacy cruft.
- This service is the source of truth for shifts, DailyPlan, check-in, chat and tasks. Absence and worker identity are canonical in Core (this service calls Core via `backend/core/grandmont_core_client.py`). CRM reads shift/DailyPlan state through the bridge and forwards plan/amendment acceptance to `backend/routes/service_bridge.py`, which is guarded by a service credential (constant-time compare, disabled when unconfigured). Do not add a "sync back from CRM" path without updating Dev OS `registry/domains.yaml` first.
- `production_path` on the VPS is NOT a git repo (code is copied there by `scripts/deploy.sh`) — always verify deployed state via the live `/api/health` endpoint's `commit` field, never by inspecting a local clone.
