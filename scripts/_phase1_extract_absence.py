#!/usr/bin/env python3
"""One-shot branch-local transform for Phase 1 absence extraction."""
from pathlib import Path

path = Path("backend/main.py")
source = path.read_text(encoding="utf-8")
start_marker = "\n# ---------- Abwesenheit — Фаза 5 (календарь отсутствий работников) ----------"
end_marker = "\n# ── DailyPlan routes (Round 1 — Production Control) ────────────────────────"

start = source.find(start_marker)
if start < 0:
    raise SystemExit("absence block start marker not found")
end = source.find(end_marker, start)
if end < 0:
    raise SystemExit("absence block end marker not found")

replacement = r'''
# ---------- Abwesenheit domain ----------
# Persistence, validation, notifications and HTTP handlers live in
# routes/absence.py. Runtime dependencies are injected here so that the domain
# module never imports main.py and compatibility aliases contain no implementation.
if __package__:
    from .routes.absence import (
        AbsenceRouteDeps,
        AbwesenheitBody,
        AbwesenheitMoveBody,
        AbwesenheitStatusBody,
        create_absence_router,
    )
else:
    from routes.absence import (  # noqa: E402
        AbsenceRouteDeps,
        AbwesenheitBody,
        AbwesenheitMoveBody,
        AbwesenheitStatusBody,
        create_absence_router,
    )

_absence_router, _absence_handlers = create_absence_router(
    AbsenceRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        # Data paths and collaborators are deliberately resolved dynamically:
        # tests isolate the JSON file and runtime code may replace integrations.
        absence_file=lambda: ABWESENHEIT_FILE,
        reasons=ABWESENHEIT_REASONS,
        public_fields=ABWESENHEIT_PUBLIC_FIELDS,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(
            path, default, mutator
        ),
        load_roles=lambda: _load_roles(),
        send_telegram_message=lambda *args, **kwargs: send_telegram_message(*args, **kwargs),
        business_today_str=lambda: business_today_str(),
        shadow_create_absence=lambda *args, **kwargs: shadow_create_absence(*args, **kwargs),
        create_critical_alert=lambda *args, **kwargs: _create_critical_alert(*args, **kwargs),
        resolve_current_display_name=lambda user_id, fallback: _resolve_current_display_name(
            user_id, fallback
        ),
    )
)
app.include_router(_absence_router)

# Compatibility names for direct-call tests/internal callers during migration.
# These are aliases only; no absence implementation remains in main.py.
_load_abwesenheit = _absence_handlers.load_abwesenheit
_save_abwesenheit = _absence_handlers.save_abwesenheit
_migrate_abwesenheit_legacy_ids = _absence_handlers.migrate_abwesenheit_legacy_ids
_notify_owner_abwesenheit_pending = _absence_handlers.notify_owner_abwesenheit_pending
_notify_worker_abwesenheit_decision = _absence_handlers.notify_worker_abwesenheit_decision
_validate_date_str = _absence_handlers.validate_date_str
_month_end = _absence_handlers.month_end
create_abwesenheit = _absence_handlers.create_abwesenheit
close_abwesenheit = _absence_handlers.close_abwesenheit
update_abwesenheit_status = _absence_handlers.update_abwesenheit_status
update_abwesenheit_dates = _absence_handlers.update_abwesenheit_dates
_auto_close_expired_open_ended_abwesenheit = _absence_handlers.auto_close_expired_open_ended_abwesenheit
list_my_abwesenheit = _absence_handlers.list_my_abwesenheit
list_all_abwesenheit = _absence_handlers.list_all_abwesenheit
delete_abwesenheit = _absence_handlers.delete_abwesenheit

'''

updated = source[:start] + "\n" + replacement + source[end:]
if updated == source:
    raise SystemExit("transform produced no change")
path.write_text(updated, encoding="utf-8")
print(f"backend/main.py: removed {source.count(chr(10)) - updated.count(chr(10))} lines net")
