---
paths: ["backend/**"]
---

# Miniapp backend rules

- `telegram_user_id` is the canonical Miniapp-facing identity key — never introduce a second identity field or resolve identity by name-matching (see `~/engineering-os/adr/001-core-canonical-identity.md`).
- Bot token auth lives in `backend/core/telegram.py` (`BOT_TOKEN`/`BOT_TOKEN_OLD`) and `backend/core/permissions.py` (`validate_init_data`/`create_session_token`/`verify_session_token`) — read `docs/DECISIONS.md`'s 2026-10-04 bot migration entry before touching either, the dual-token verify-fallback pattern there is deliberate, not legacy cruft to clean up.
- This service IS the source of truth for shifts/DailyPlan/absence/chat/tasks — CRM reads it read-only. Don't add a "sync back from CRM" path without updating `~/engineering-os/registry/domains.yaml` first.
- `production_path` on the VPS is NOT a git repo (code is copied there by `scripts/deploy.sh`) — always verify deployed state via the live `/api/health` endpoint's `commit` field, never by inspecting a local clone.
