from pathlib import Path

path = Path('backend/main.py')
text = path.read_text(encoding='utf-8')
start_marker = '# ---------- Потребности (10.33) — worker → owner запросы (инструмент/материалы/защита) ----------'
end_marker = '# ---------- Mängelmanagement — Фаза 3 ----------'
start = text.index(start_marker)
end = text.index(end_marker, start)

replacement = '''# ---------- Потребности / Needs domain ----------
# Worker -> owner material/tool/PPE/access requests now live in routes/needs.py.
# main.py remains the composition root and keeps only migration aliases.
if __package__:
    from .routes.needs import (
        NeedsRouteDeps,
        TaskCreateBody,
        TaskStatusBody,
        create_needs_router,
    )
else:
    from routes.needs import (  # noqa: E402
        NeedsRouteDeps,
        TaskCreateBody,
        TaskStatusBody,
        create_needs_router,
    )

_needs_router, _needs_handlers = create_needs_router(
    NeedsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        tasks_file=lambda: TASKS_FILE,
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        task_priorities=TASK_PRIORITIES,
        task_categories=TASK_CATEGORIES,
        task_statuses=TASK_STATUSES,
        has_active_object_access=lambda user_id, object_id: has_active_object_access(user_id, object_id),
        load_roles=lambda: _load_roles(),
        get_worker_profile=lambda user_id: _get_worker_profile(user_id),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        send_telegram_message=lambda *args, **kwargs: send_telegram_message(*args, **kwargs),
        business_now=lambda: business_now(),
        load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
    )
)
app.include_router(_needs_router)

# Compatibility names for direct-call tests/internal callers during migration.
# These are aliases to the extracted implementation only.
_load_tasks = _needs_handlers.load_tasks
_save_tasks = _needs_handlers.save_tasks
_task_is_open = _needs_handlers.task_is_open
_normalize_task_due_at = _needs_handlers.normalize_task_due_at
_format_due_at = _needs_handlers.format_due_at
_overdue_task_alerts = _needs_handlers.overdue_task_alerts
list_tasks = _needs_handlers.list_tasks
# This is intentionally the final module-level create_task symbol. Historically
# it overwrote the earlier object-task function of the same name; Phase 4 keeps
# that exact public-module behavior and Phase 6 preserves it here.
create_task = _needs_handlers.create_task
update_task_status = _needs_handlers.update_task_status


'''

text = text[:start] + replacement + text[end:]
path.write_text(text, encoding='utf-8')
print('phase6 transform applied')
