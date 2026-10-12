"""Worker roster, profile, skills and avatar HTTP domain.

The module owns profile-facing business rules and persistence orchestration while
cross-domain inputs (roles, check-in state, Sheets object labels) are injected by
``backend.main``.  It intentionally has no import back to the monolith.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Collection, Mapping

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel


class SkillV2Body(BaseModel):
    skill_id: str
    level: str
    verified: bool = False


class ProfileUpdateBody(BaseModel):
    name: str | None = None
    skills: list[str] | None = None
    skills_v2: list[SkillV2Body] | None = None
    quiz_completed: bool | None = None
    onboarding_completed: bool | None = None
    onboarding_version: int | None = None
    pants_size: str | None = None
    shirt_size: str | None = None
    shoe_size: str | None = None
    birthday: str | None = None


class SkillVerificationBody(BaseModel):
    verified: bool


@dataclass(frozen=True)
class WorkforceProfilesDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    load_roles: Callable[[], dict]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    atomic_write_json: Callable[[str, Any], None]
    worker_profiles_file: Callable[[], str]
    work_types_catalog: Callable[[], Any]
    skill_options: Collection[str]
    normalize_profile_skills: Callable[[dict], tuple[list, bool]]
    legacy_skill_names_from_v2: Callable[[list], list]
    set_skill_verification: Callable[[list, str, bool], tuple[list, bool]]
    skill_levels: Collection[str]
    business_today: Callable[[], Any]
    business_today_str: Callable[[], str]
    last_seen_for: Callable[[str], float | None]
    online_threshold_seconds: int
    load_checkin_meta: Callable[[], list]
    is_active_photo_checkin_session: Callable[[dict], bool]
    cached_get_used_range: Callable[[str], list]
    avatar_dir: Callable[[], str]
    avatar_max_bytes: int
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]


@dataclass(frozen=True)
class WorkforceProfilesHandlers:
    save_worker_profiles: Callable[[dict], None]
    get_worker_profile: Callable[[Any], dict]
    get_worker_skills_v2: Callable[[Any], list]
    is_meaningful_name: Callable[[str | None, str], bool]
    resolve_current_display_name: Callable[[Any, str | None], str]
    validate_birthday: Callable[[str], str]
    profile_completion_status: Callable[[dict, dict, str], dict]
    list_workers: Callable[..., dict]
    get_work_types: Callable[..., Any]
    get_my_profile: Callable[..., dict]
    get_user_card: Callable[..., dict]
    update_my_profile: Callable[..., dict]
    verify_worker_skill: Callable[..., dict]
    upload_my_avatar: Callable[..., Any]
    get_avatar: Callable[..., Any]
    skill_options: list[str]


def create_workforce_profiles_router(
    deps: WorkforceProfilesDeps,
) -> tuple[APIRouter, WorkforceProfilesHandlers]:
    router = APIRouter()
    skill_options = list(deps.skill_options)
    os.makedirs(deps.avatar_dir(), exist_ok=True)

    def save_worker_profiles(profiles: dict):
        deps.atomic_write_json(deps.worker_profiles_file(), profiles)

    def get_worker_profile(user_id) -> dict:
        profiles = deps.load_worker_profiles()
        return profiles.get(str(user_id), {"skills": [], "quiz_completed": False})

    def get_worker_skills_v2(user_id) -> list:
        key = str(user_id)

        def _mutator(profiles):
            profile = profiles.get(key, {"skills": [], "quiz_completed": False})
            skills_v2, changed = deps.normalize_profile_skills(profile)
            if changed:
                profile['skills_v2'] = skills_v2
                profiles[key] = profile
            return skills_v2

        return deps.update_json_transaction(deps.worker_profiles_file(), {}, _mutator)

    def is_meaningful_name(raw: str | None, user_id: str) -> bool:
        cleaned = deps.sanitize_display_name(raw, '')
        if not cleaned:
            return False
        value = cleaned.strip()
        if len(value) < 2 or len(value) > 100:
            return False
        if value.isdigit() or value == str(user_id):
            return False
        return True

    def resolve_current_display_name(user_id, stored_name: str | None = None) -> str:
        uid = str(user_id)
        profile_name = deps.load_worker_profiles().get(uid, {}).get('name')
        if is_meaningful_name(profile_name, uid):
            return deps.sanitize_display_name(profile_name, uid)
        return deps.sanitize_display_name(stored_name, uid)

    def validate_birthday(raw: str) -> str:
        if raw is None or not str(raw).strip():
            raise HTTPException(400, "Укажите дату рождения")
        value = str(raw).strip()
        try:
            birthday = datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError as exc:
            raise HTTPException(400, "Некорректная дата рождения (формат ГГГГ-ММ-ДД)") from exc
        if birthday > deps.business_today():
            raise HTTPException(400, "Дата рождения не может быть в будущем")
        return birthday.isoformat()

    def profile_completion_status(user: dict, profile: dict, role: str) -> dict:
        uid = str(user['id'])
        name_ok = is_meaningful_name(profile.get('name') or user.get('first_name'), uid)
        birthday_ok = bool(profile.get('birthday'))
        return {
            'name_required': not name_ok,
            'birthday_required': role == 'worker' and not birthday_ok,
        }

    @router.get('/api/workers')
    def list_workers(user: dict = Depends(deps.get_current_user)):
        roles = deps.load_roles()
        profiles = deps.load_worker_profiles()
        all_ids = set(roles.keys() if isinstance(roles, dict) else []) | set(
            profiles.keys() if isinstance(profiles, dict) else []
        )
        workers = []
        for uid in all_ids:
            try:
                profile = profiles.get(uid) if isinstance(profiles, dict) else None
                if not isinstance(profile, dict):
                    profile = {}
                last_seen = deps.last_seen_for(uid)
                role = roles.get(uid) if isinstance(roles, dict) else None
                skills = profile.get('skills', [])
                workers.append({
                    'user_id': uid,
                    'role': role,
                    'access_granted': role is not None,
                    'name': deps.sanitize_display_name(profile.get('name'), uid),
                    'skills': skills if isinstance(skills, list) else [],
                    'has_avatar': bool(profile.get('avatar')),
                    'quiz_completed': bool(profile.get('quiz_completed', False)),
                    'online': bool(last_seen and (time.time() - last_seen) < deps.online_threshold_seconds),
                })
            except Exception:
                logging.getLogger('grandmont.workers').warning(
                    'workers: skipping malformed entry for uid=%s', uid
                )
        return {'workers': workers}

    @router.get('/api/work-types')
    def get_work_types(user: dict = Depends(deps.get_current_user)):
        return deps.work_types_catalog()

    @router.get('/api/profile/me')
    def get_my_profile(user: dict = Depends(deps.get_current_user)):
        profile = get_worker_profile(user['id'])
        healed_name = deps.sanitize_display_name(profile.get('name'), '') or None
        if not healed_name and user.get('first_name'):
            candidate = deps.sanitize_display_name(user.get('first_name'), '')
            if candidate:
                profiles = deps.load_worker_profiles()
                key = str(user['id'])
                profile = profiles.get(key, {"skills": [], "quiz_completed": False})
                profile['name'] = candidate
                profiles[key] = profile
                save_worker_profiles(profiles)
                healed_name = candidate
        if healed_name:
            profile = {**profile, 'name': healed_name}
        elif not deps.sanitize_display_name(profile.get('name'), ''):
            profile = {**profile, 'name': str(user['id'])}

        skills_v2 = get_worker_skills_v2(user['id'])
        role = deps.load_roles().get(str(user['id']), 'worker')
        return {
            'user_id': user['id'],
            'skill_options': skill_options,
            **profile,
            'role': role,
            'skills': deps.legacy_skill_names_from_v2(skills_v2),
            'skills_v2': skills_v2,
            'onboarding_completed': bool(profile.get('onboarding_completed') or profile.get('quiz_completed')),
            'onboarding_version': profile.get('onboarding_version', 1),
            'needs_completion': profile_completion_status(user, profile, role),
        }

    @router.get('/api/users/{target_id}/card')
    def get_user_card(
        target_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        roles = deps.load_roles()
        if target_id not in roles:
            raise HTTPException(404, 'Пользователь не найден')
        profile = get_worker_profile(target_id)
        skills_v2 = get_worker_skills_v2(target_id)
        card = {
            'user_id': target_id,
            'name': deps.sanitize_display_name(profile.get('name'), target_id),
            'role': roles[target_id],
            'skills': deps.legacy_skill_names_from_v2(skills_v2),
            'skills_v2': skills_v2,
            'has_avatar': bool(profile.get('avatar')),
        }
        if role == 'owner' and roles[target_id] != 'owner':
            today = deps.business_today_str()
            sessions = [
                session for session in deps.load_checkin_meta()
                if str(session.get('user_id')) == target_id and session.get('date') == today
            ]
            open_session = next(
                (session for session in sessions if deps.is_active_photo_checkin_session(session)),
                None,
            )
            rows = deps.cached_get_used_range('Объекты')
            object_names = {}
            if rows:
                header, data = rows[0], rows[1:]
                for row in data:
                    obj = dict(zip(header, row))
                    object_names[str(obj.get('ID объекта', ''))] = obj.get('Объект', '')
            if open_session:
                card['shift_status'] = 'working'
                card['object_name'] = object_names.get(
                    open_session['object_id'], open_session['object_id']
                )
                card['stage_name'] = open_session.get('stage_name') or ''
                card['start_at'] = open_session.get('start_at')
            else:
                card['shift_status'] = 'idle'
        return card

    @router.patch('/api/profile/me')
    def update_my_profile(
        body: ProfileUpdateBody,
        user: dict = Depends(deps.get_current_user),
    ):
        key = str(user['id'])
        role = deps.load_roles().get(key, 'worker')
        updates = body.model_dump(exclude_unset=True)
        if 'name' in updates:
            cleaned = deps.sanitize_display_name(updates['name'], '')
            if not cleaned:
                raise HTTPException(400, 'Имя не должно быть пустым')
            updates['name'] = cleaned[:100]
        if 'birthday' in updates and updates['birthday'] is not None:
            updates['birthday'] = validate_birthday(updates['birthday'])
        if 'skills_v2' in updates:
            for skill in updates['skills_v2']:
                if skill.get('level') not in deps.skill_levels:
                    raise HTTPException(400, f"Недопустимый уровень навыка: {skill.get('level')!r}")

        def _mutator(profiles):
            profile = profiles.get(key, {"skills": [], "quiz_completed": False})
            if 'skills_v2' in updates:
                existing_by_id = {
                    skill['skill_id']: skill
                    for skill in (profile.get('skills_v2') or [])
                    if 'skill_id' in skill
                }
                normalized = []
                for skill in updates['skills_v2']:
                    prior = existing_by_id.get(skill.get('skill_id'))
                    unchanged = prior is not None and prior.get('level') == skill.get('level')
                    normalized.append({
                        'skill_id': skill.get('skill_id'),
                        'level': skill.get('level'),
                        'verified': bool(prior.get('verified')) if unchanged else False,
                    })
                updates['skills_v2'] = normalized
            if updates.get('onboarding_completed'):
                merged_name = updates.get('name', profile.get('name'))
                merged_skills = updates.get('skills_v2', profile.get('skills_v2', []))
                merged_birthday = updates.get('birthday', profile.get('birthday'))
                if not deps.sanitize_display_name(merged_name, ''):
                    raise HTTPException(400, 'Укажите имя, чтобы завершить регистрацию')
                if role == 'worker' and not merged_birthday:
                    raise HTTPException(400, 'Укажите дату рождения, чтобы завершить регистрацию')
                if not merged_skills:
                    raise HTTPException(400, 'Выберите хотя бы один навык')
                for skill in merged_skills:
                    level = skill.get('level') if isinstance(skill, dict) else None
                    if level not in deps.skill_levels:
                        raise HTTPException(400, 'Укажите уровень для каждого выбранного навыка')
                updates['onboarding_completed_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
                updates['onboarding_version'] = 2
                updates['quiz_completed'] = True
            profile.update(updates)
            profiles[key] = profile
            return profile

        return deps.update_json_transaction(deps.worker_profiles_file(), {}, _mutator)

    @router.patch('/api/workers/{user_id}/skills/{skill_id}/verification')
    def verify_worker_skill(
        user_id: str,
        skill_id: str,
        body: SkillVerificationBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        key = str(user_id)

        def _mutator(profiles):
            profile = profiles.get(key)
            if not profile:
                raise HTTPException(404, 'Профиль не найден')
            skills_v2, _changed = deps.normalize_profile_skills(profile)
            skills_v2, found = deps.set_skill_verification(skills_v2, skill_id, body.verified)
            if not found:
                raise HTTPException(404, 'У работника нет такого навыка')
            profile['skills_v2'] = skills_v2
            profiles[key] = profile

        deps.update_json_transaction(deps.worker_profiles_file(), {}, _mutator)
        return {'skill_id': skill_id, 'verified': body.verified}

    @router.post('/api/profile/me/avatar')
    async def upload_my_avatar(
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
    ):
        raw = await file.read()
        if len(raw) > deps.avatar_max_bytes:
            raise HTTPException(400, 'Аватар слишком большой (макс. 4 МБ)')
        detected = deps.sniff_image(raw)
        if not detected:
            raise HTTPException(400, 'Файл должен быть изображением')
        ext = deps.allowed_image_mime_ext[detected]
        uid = str(user['id'])
        avatar_dir = deps.avatar_dir()
        os.makedirs(avatar_dir, exist_ok=True)
        for filename in os.listdir(avatar_dir):
            if filename.startswith(uid + '.'):
                os.remove(os.path.join(avatar_dir, filename))
        with open(os.path.join(avatar_dir, f'{uid}.{ext}'), 'wb') as handle:
            handle.write(raw)

        def _mutator(profiles):
            profile = profiles.get(uid, {"skills": [], "quiz_completed": False})
            profile['avatar'] = True
            if not profile.get('name'):
                profile['name'] = user.get('first_name', uid)
            profiles[uid] = profile

        deps.update_json_transaction(deps.worker_profiles_file(), {}, _mutator)
        return {'status': 'ok'}

    @router.get('/api/profile/{user_id}/avatar')
    def get_avatar(user_id: str, user: dict = Depends(deps.get_current_user)):
        if not user_id.isdigit():
            raise HTTPException(400, 'user_id должен быть числовым Telegram ID')
        for ext in ('jpg', 'png', 'webp'):
            path = os.path.join(deps.avatar_dir(), f'{user_id}.{ext}')
            if os.path.exists(path):
                return FileResponse(path)
        raise HTTPException(404, 'Аватар не найден')

    return router, WorkforceProfilesHandlers(
        save_worker_profiles=save_worker_profiles,
        get_worker_profile=get_worker_profile,
        get_worker_skills_v2=get_worker_skills_v2,
        is_meaningful_name=is_meaningful_name,
        resolve_current_display_name=resolve_current_display_name,
        validate_birthday=validate_birthday,
        profile_completion_status=profile_completion_status,
        list_workers=list_workers,
        get_work_types=get_work_types,
        get_my_profile=get_my_profile,
        get_user_card=get_user_card,
        update_my_profile=update_my_profile,
        verify_worker_skill=verify_worker_skill,
        upload_my_avatar=upload_my_avatar,
        get_avatar=get_avatar,
        skill_options=skill_options,
    )
