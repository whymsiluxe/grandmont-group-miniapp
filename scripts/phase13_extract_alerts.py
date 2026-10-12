from pathlib import Path

path = Path('backend/main.py')
source = path.read_text(encoding='utf-8')
start_marker = '# ---------- Alerts inbox — role-aware агрегация (Фаза 2g, восстановлено после инцидента Фазы 3) ----------\n'
end_marker = '# ---------- Tools domain ----------\n'
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit('alerts markers mismatch')
start = source.index(start_marker)
end = source.index(end_marker, start)
old_lines = source[start:end].count('\n')
replacement = r'''# ---------- Alerts domain ----------
try:
    from .routes.alerts import AlertDismissBody, AlertsDeps, create_alerts_router
except ImportError:
    from routes.alerts import AlertDismissBody, AlertsDeps, create_alerts_router  # noqa: E402

_alerts_router, _alerts_handlers = create_alerts_router(
    AlertsDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
        load_repo_tools_lib=lambda: _load_repo_tools_lib(),
        load_assignments=lambda: _load_assignments(),
        load_abwesenheit=lambda: _load_abwesenheit(),
        assignment_confirmation_alerts=lambda: _assignment_confirmation_alerts(),
        overdue_task_alerts=lambda: _overdue_task_alerts(),
        load_critical_alerts=lambda: _load_critical_alerts(),
        load_activity_alerts=lambda: _load_activity_alerts(),
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        alert_dismissals_file=lambda: ALERT_DISMISSALS_FILE,
        alert_dismiss_ttl=ALERT_DISMISS_TTL,
    )
)
app.include_router(_alerts_router)

_load_alert_dismissals = _alerts_handlers.load_alert_dismissals
_save_alert_dismissals = _alerts_handlers.save_alert_dismissals
get_alerts = _alerts_handlers.get_alerts
dismiss_alerts = _alerts_handlers.dismiss_alerts


'''
source = source[:start] + replacement + source[end:]
path.write_text(source, encoding='utf-8')
print(f'alerts block replaced: {old_lines} old lines -> {replacement.count(chr(10))} wiring lines')
