from pathlib import Path

main_path = Path('backend/main.py')
module_path = Path('backend/routes/workforce_profiles.py')

# First tighten the new module to preserve the legacy atomic profile-write path.
module = module_path.read_text(encoding='utf-8')
old = """    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]\n    worker_profiles_file: Callable[[], str]\n"""
new = """    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]\n    atomic_write_json: Callable[[str, Any], None]\n    worker_profiles_file: Callable[[], str]\n"""
if module.count(old) != 1:
    raise SystemExit('workforce deps marker mismatch')
module = module.replace(old, new)

start = module.index('    def save_worker_profiles(profiles: dict):\n')
end = module.index('\n    def get_worker_profile', start)
module = module[:start] + """    def save_worker_profiles(profiles: dict):\n        deps.atomic_write_json(deps.worker_profiles_file(), profiles)\n\n""" + module[end + 1:]

old_heal = """                key = str(user['id'])\n\n                def _heal(profiles):\n                    current = profiles.get(key, {\"skills\": [], \"quiz_completed\": False})\n                    current['name'] = candidate\n                    profiles[key] = current\n                    return current\n\n                profile = deps.update_json_transaction(deps.worker_profiles_file(), {}, _heal)\n                healed_name = candidate\n"""
new_heal = """                profiles = deps.load_worker_profiles()\n                key = str(user['id'])\n                profile = profiles.get(key, {\"skills\": [], \"quiz_completed\": False})\n                profile['name'] = candidate\n                profiles[key] = profile\n                save_worker_profiles(profiles)\n                healed_name = candidate\n"""
if module.count(old_heal) != 1:
    raise SystemExit('profile self-heal marker mismatch')
module = module.replace(old_heal, new_heal)
module_path.write_text(module, encoding='utf-8')

source = main_path.read_text(encoding='utf-8')
start_marker = '# ---------- Workers list (для bubble-assign, Фаза 2 → доделано в Фазе 3) ----------\n'
end_marker = '\ndef _photo_pause_seconds(s: dict) -> int:\n'
if source.count(start_marker) != 1 or source.count(end_marker) != 1:
    raise SystemExit('main workforce markers mismatch')
start = source.index(start_marker)
end = source.index(end_marker, start)
old_lines = source[start:end].count('\n')

replacement = r'''# ---------- Workforce profiles domain ----------
# Roster, profile/onboarding, skills verification and avatar HTTP flow live in
# routes/workforce_profiles.py. Cross-domain check-in/Sheets state is injected.
try:
    from .core.profiles import _load_worker_profiles, _sanitize_display_name
    from .routes.workforce_profiles import (
        ProfileUpdateBody,
        SkillV2Body,
        SkillVerificationBody,
        WorkforceProfilesDeps,
        create_workforce_profiles_router,
    )
except ImportError:
    from core.profiles import _load_worker_profiles, _sanitize_display_name  # noqa: E402
    from routes.workforce_profiles import (  # noqa: E402
        ProfileUpdateBody,
        SkillV2Body,
        SkillVerificationBody,
        WorkforceProfilesDeps,
        create_workforce_profiles_router,
    )

_workforce_profiles_router, _workforce_profiles_handlers = create_workforce_profiles_router(
    WorkforceProfilesDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        load_roles=lambda: _load_roles(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda raw, fallback: _sanitize_display_name(raw, fallback),
        update_json_transaction=update_json_transaction,
        atomic_write_json=lambda path, value: _atomic_write_json(path, value),
        worker_profiles_file=lambda: WORKER_PROFILES_FILE,
        work_types_catalog=wt.work_types_catalog,
        skill_options=[w['name'] for w in wt.WORK_TYPES if w['active']],
        normalize_profile_skills=pskills.normalize_profile_skills,
        legacy_skill_names_from_v2=pskills.legacy_skill_names_from_v2,
        set_skill_verification=pskills.set_skill_verification,
        skill_levels=pskills.SKILL_LEVELS,
        business_today=lambda: business_today(),
        business_today_str=lambda: business_today_str(),
        last_seen_for=lambda uid: _last_seen.get(str(uid)),
        online_threshold_seconds=ONLINE_THRESHOLD_SECONDS,
        load_checkin_meta=lambda: _load_checkin_meta(),
        is_active_photo_checkin_session=lambda session: _is_active_photo_checkin_session(session),
        cached_get_used_range=lambda tab: _cached_get_used_range(tab),
        avatar_dir=lambda: AVATAR_DIR,
        avatar_max_bytes=AVATAR_MAX_BYTES,
        sniff_image=sniff_image,
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
    )
)
app.include_router(_workforce_profiles_router)

SKILL_OPTIONS = _workforce_profiles_handlers.skill_options
_save_worker_profiles = _workforce_profiles_handlers.save_worker_profiles
_get_worker_profile = _workforce_profiles_handlers.get_worker_profile
_get_worker_skills_v2 = _workforce_profiles_handlers.get_worker_skills_v2
_is_meaningful_name = _workforce_profiles_handlers.is_meaningful_name
_resolve_current_display_name = _workforce_profiles_handlers.resolve_current_display_name
_validate_birthday = _workforce_profiles_handlers.validate_birthday
_profile_completion_status = _workforce_profiles_handlers.profile_completion_status
list_workers = _workforce_profiles_handlers.list_workers
get_work_types = _workforce_profiles_handlers.get_work_types
get_my_profile = _workforce_profiles_handlers.get_my_profile
get_user_card = _workforce_profiles_handlers.get_user_card
update_my_profile = _workforce_profiles_handlers.update_my_profile
verify_worker_skill = _workforce_profiles_handlers.verify_worker_skill
upload_my_avatar = _workforce_profiles_handlers.upload_my_avatar
get_avatar = _workforce_profiles_handlers.get_avatar

# Check-in-specific GPS heuristic stays in the composition module until the
# remaining check-in support helpers are consolidated separately.
def _gps_suspect(lat: str, lon: str) -> bool:
    try:
        f_lat, f_lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return True
    if f_lat == 0 and f_lon == 0:
        return True
    return not (47 <= f_lat <= 55.5 and 5 <= f_lon <= 16)

'''
source = source[:start] + replacement + source[end + 1:]
main_path.write_text(source, encoding='utf-8')
print(f'workforce profile block replaced: {old_lines} old lines -> {replacement.count(chr(10))} wiring lines')
