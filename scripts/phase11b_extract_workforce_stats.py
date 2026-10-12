from pathlib import Path

path = Path('backend/main.py')
source = path.read_text(encoding='utf-8')
start_marker = '@app.get("/api/profile/stats")\n'
end_marker = '# ---------- Назначения работников на объекты (Фаза 2c, восстановлено после инцидента Фазы 3) ----------\n'
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit('workforce stats markers mismatch')
start = source.index(start_marker)
end = source.index(end_marker, start)
old_lines = source[start:end].count('\n')
replacement = r'''# ---------- Workforce statistics domain ----------
try:
    from .routes.workforce_stats import WorkforceStatsDeps, create_workforce_stats_router
except ImportError:
    from routes.workforce_stats import WorkforceStatsDeps, create_workforce_stats_router  # noqa: E402

_workforce_stats_router, _workforce_stats_handlers = create_workforce_stats_router(
    WorkforceStatsDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_checkin_meta=lambda: _load_checkin_meta(),
        load_roles=lambda: _load_roles(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        hours_from_session=lambda session: _hours_from_session(session),
        session_hours_live=lambda session: _session_hours_live(session),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        business_today=lambda: business_today(),
        load_assignments=lambda: _load_assignments(),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        load_abwesenheit=lambda: _load_abwesenheit(),
        get_worker_profile=lambda uid: _get_worker_profile(uid),
        get_worker_skills_v2=lambda uid: _get_worker_skills_v2(uid),
        legacy_skill_names_from_v2=pskills.legacy_skill_names_from_v2,
    )
)
app.include_router(_workforce_stats_router)
profile_stats = _workforce_stats_handlers.profile_stats
get_dashboard_team_hours = _workforce_stats_handlers.get_dashboard_team_hours


'''
source = source[:start] + replacement + source[end:]
path.write_text(source, encoding='utf-8')
print(f'workforce stats block replaced: {old_lines} old lines -> {replacement.count(chr(10))} wiring lines')
