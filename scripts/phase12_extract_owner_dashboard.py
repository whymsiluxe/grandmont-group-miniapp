from pathlib import Path

path = Path('backend/main.py')
source = path.read_text(encoding='utf-8')

# Remove the legacy assignment-status body but keep the escalation constants and
# _utcnow_iso in main because the latter is shared by object-history/CRUD paths.
status_start = 'def _assignment_status(a: dict) -> str:\n'
status_end = '\n\nASSIGNMENT_CONFIRM_WARNING_SECONDS = 2 * 60 * 60\n'
if source.count(status_start) != 1 or source.count(status_end) != 1:
    raise SystemExit('assignment status markers mismatch')
s1 = source.index(status_start)
e1 = source.index(status_end, s1)
source = source[:s1] + source[e1 + 2:]

start_marker = 'def _parse_assignment_timestamp(value) -> int | None:\n'
end_marker = '# ---------- Alerts inbox — role-aware агрегация (Фаза 2g, восстановлено после инцидента Фазы 3) ----------\n'
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit('owner dashboard markers mismatch')
start = source.index(start_marker)
end = source.index(end_marker, start)
old_lines = source[start:end].count('\n')

replacement = r'''# ---------- Owner dashboard domain ----------
try:
    from .routes.owner_dashboard import OwnerDashboardDeps, create_owner_dashboard_router
except ImportError:
    from routes.owner_dashboard import OwnerDashboardDeps, create_owner_dashboard_router  # noqa: E402

_owner_dashboard_router, _owner_dashboard_handlers = create_owner_dashboard_router(
    OwnerDashboardDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        business_today_str=lambda: business_today_str(),
        business_today=lambda: business_today(),
        load_checkin_meta=lambda: _load_checkin_meta(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        hours_from_session=lambda session: _hours_from_session(session),
        load_assignments=lambda: _load_assignments(),
        load_abwesenheit=lambda: _load_abwesenheit(),
        load_roles=lambda: _load_roles(),
        load_repo_objekte_lib=lambda: _load_repo_objekte_lib(),
        load_roadmap_store=lambda: _safe_load_json(rl.ROADMAP_FILE, rl._default_store()),
        skill_display_name=pskills.skill_display_name,
        confirm_warning_seconds=ASSIGNMENT_CONFIRM_WARNING_SECONDS,
        confirm_danger_seconds=ASSIGNMENT_CONFIRM_DANGER_SECONDS,
    )
)
app.include_router(_owner_dashboard_router)

_assignment_status = _owner_dashboard_handlers.assignment_status
_parse_assignment_timestamp = _owner_dashboard_handlers.parse_assignment_timestamp
_assignment_pending_escalation = _owner_dashboard_handlers.assignment_pending_escalation
get_dashboard_shifts_today = _owner_dashboard_handlers.get_dashboard_shifts_today
get_active_blockers = _owner_dashboard_handlers.get_active_blockers
get_team_plan = _owner_dashboard_handlers.get_team_plan
_format_assignment_wait = _owner_dashboard_handlers.format_assignment_wait
_assignment_confirmation_alerts = _owner_dashboard_handlers.assignment_confirmation_alerts


'''
source = source[:start] + replacement + source[end:]
path.write_text(source, encoding='utf-8')
print(f'owner dashboard block replaced: {old_lines} old lines -> {replacement.count(chr(10))} wiring lines')
