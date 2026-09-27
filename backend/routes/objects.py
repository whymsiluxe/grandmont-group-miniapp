"""Objects-domain HTTP routes.

This module deliberately does not import backend.main. main.py wires the
runtime dependencies through ObjectsRouteDeps, then includes the returned
router and re-exports the handlers for legacy direct-call tests.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

try:
    from .. import assignment_matching as amatch
    from .. import profile_skills as pskills
    from .. import work_types as wt
    from ..core.constants import VALID_OBJECT_STATUSES
except ImportError:
    import assignment_matching as amatch  # noqa: E402
    import profile_skills as pskills  # noqa: E402
    import work_types as wt  # noqa: E402
    from core.constants import VALID_OBJECT_STATUSES  # noqa: E402


class AssignBody(BaseModel):
    user_id: str
    stage_id: str = ''
    work_type_id: str = ''
    date_from: str = ''
    date_to: str = ''
    task_note: str = ''


class AssignmentUpdateBody(BaseModel):
    work_type_id: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    task_note: str | None = None


class AssignmentRespondBody(BaseModel):
    accept: bool
    decline_reason: str = ''
    assignment_id: str = ''


class BatchAssignBody(BaseModel):
    user_ids: list[str]
    work_type_ids: list[str]
    date_from: str
    date_to: str
    task_note: str = ''


class ObjectDescriptionBody(BaseModel):
    description: str


class InfoItemBody(BaseModel):
    text: str
    qty: str = ''


class NewObjectBody(BaseModel):
    name: str
    adresse: str
    budget: str
    start: str = ''
    end: str = ''


class StatusBody(BaseModel):
    status: str


@dataclass(frozen=True)
class ObjectsRouteDeps:
    get_current_user: Callable
    get_role: Callable
    require_owner: Callable
    require_object_access: Callable
    cached_get_used_range: Callable
    load_assignments: Callable
    load_worker_profiles: Callable
    load_object_images: Callable
    load_repo_objekte_lib: Callable
    serialize_object_for_worker: Callable
    assignment_status: Callable
    business_today_str: Callable
    safe_load_json: Callable
    update_json_transaction: Callable
    object_assignments_file: Callable
    object_history_file: Callable
    object_info_file: Callable
    load_abwesenheit: Callable
    load_roles: Callable
    load_checkin_meta: Callable
    validate_date_str: Callable
    dates_overlap: Callable
    assignment_periods_overlap: Callable
    utcnow_iso: Callable
    sanitize_display_name: Callable
    object_history_worker_name: Callable
    append_object_history_best_effort: Callable
    object_info_entry: Callable
    ensure_object_info_entry: Callable
    require_server_script: Callable
    create_object_script: Callable
    create_object_folder_script: Callable


_logger = logging.getLogger('grandmont.objects')


def _valid_assignments(assignments, oid: str) -> list[dict]:
    """One malformed legacy assignment record (not a dict, or missing the
    user_id every downstream consumer indexes by) must not take down the
    whole /api/objects response -- skip just that record, log enough to find
    it (object id + index, never the record's own content, which could carry
    a worker's task_note/decline_reason free text)."""
    raw = assignments.get(oid) if isinstance(assignments, dict) else None
    if not isinstance(raw, list):
        return []
    valid = []
    for idx, a in enumerate(raw):
        if not isinstance(a, dict) or not a.get('user_id'):
            _logger.warning("objects: skipping malformed assignment object_id=%s index=%s", oid, idx)
            continue
        valid.append(a)
    return valid


def create_objects_router(deps: ObjectsRouteDeps):
    router = APIRouter()

    @router.get("/api/objects")
    def list_objects(user: dict = Depends(deps.get_current_user), role: str = Depends(deps.get_role)):
        rows = deps.cached_get_used_range('Объекты')
        if not rows:
            return {"objects": []}
        header, data = rows[0], rows[1:]
        assignments = deps.load_assignments()
        profiles = deps.load_worker_profiles()
        images = deps.load_object_images()
        o = deps.load_repo_objekte_lib()
        stages_by_object = o.all_stages_grouped()

        def _user_info(uid: str, assignment: dict | None = None) -> dict:
            p = profiles.get(str(uid)) if isinstance(profiles, dict) else None
            if not isinstance(p, dict):
                p = {}
            info = {
                "user_id": str(uid),
                "name": deps.sanitize_display_name(p.get('name'), str(uid)),
                "has_avatar": bool(p.get('avatar')),
            }
            if assignment is not None:
                info["assignment_status"] = deps.assignment_status(assignment)
                info["decline_reason"] = assignment.get('decline_reason', '')
                info["task_note"] = assignment.get('task_note', '')
                info["assignment_id"] = assignment.get('id', '')
                info["date_from"] = assignment.get('date_from', '')
                info["date_to"] = assignment.get('date_to', '')
                work_type_id = assignment.get('work_type_id', '')
                info["work_type_id"] = work_type_id
                info["work_type_name"] = pskills.skill_display_name(work_type_id) if work_type_id else ''
                info["stage_id"] = assignment.get('stage_id', '')
            return info

        def _stage_summary(oid: str) -> dict | None:
            stages = stages_by_object.get(oid.upper())
            if not isinstance(stages, list) or not stages:
                return None
            stages = [s for s in stages if isinstance(s, dict)]
            if not stages:
                return None
            completed = [s.get('Название этапа', '') for s in stages if s.get('Статус') == 'готово']
            current = next((s for s in stages if s.get('Статус') == 'в процессе'), None)
            current_idx = stages.index(current) if current else -1
            if current:
                next_stage = stages[current_idx + 1] if current_idx + 1 < len(stages) else None
            else:
                next_stage = next((s for s in stages if s.get('Статус') != 'готово'), None)
            return {
                "completed": completed,
                "completed_count": len(completed),
                "current": current.get('Название этапа', '') if current else None,
                "next": next_stage.get('Название этапа', '') if next_stage else None,
                "total": len(stages),
            }

        objects = []
        for r in data:
            obj = None
            try:
                obj = dict(zip(header, r))
                oid = str(obj.get('ID объекта', ''))
                obj_assignments = _valid_assignments(assignments, oid)
                if role == 'owner':
                    today_str = deps.business_today_str()
                    seen_uids = set()
                    deduped_users = []
                    detail_users = []
                    for a in obj_assignments:
                        if deps.assignment_status(a) == 'declined':
                            continue
                        a_from = str(a.get('date_from') or '')
                        a_to = str(a.get('date_to') or '')
                        is_dated = bool(a_from and a_to)
                        if is_dated and not (a_from <= today_str <= a_to):
                            continue
                        uid = str(a.get('user_id'))
                        detail_users.append(_user_info(uid, a))
                        if uid in seen_uids:
                            continue
                        seen_uids.add(uid)
                        deduped_users.append(_user_info(uid, a))
                    obj['assigned_users'] = deduped_users
                    obj['assigned_users_detail'] = detail_users
                    obj['photo_count'] = len(images.get(oid) or [])
                    obj['stage_summary'] = _stage_summary(oid)
                else:
                    obj = deps.serialize_object_for_worker(
                        obj, str(user['id']), obj_assignments, _user_info, _stage_summary, images
                    )
            except Exception:
                bad_oid = obj.get('ID объекта') if isinstance(obj, dict) else '?'
                _logger.exception("objects: skipping malformed object row ID=%r", bad_oid)
                continue
            objects.append(obj)
        return {"objects": objects}

    @router.get("/api/my-assignments")
    def my_assignments(user: dict = Depends(deps.get_current_user)):
        assignments = deps.load_assignments()
        uid = str(user['id'])
        rows = deps.cached_get_used_range('Объекты')
        names = {}
        if rows:
            header, data = rows[0], rows[1:]
            for r in data:
                obj = dict(zip(header, r))
                oid_key = str(obj.get('ID объекта', ''))
                names[oid_key] = obj.get('Объект') or obj.get('Название') or obj.get('Адрес') or oid_key

        result = []
        for oid, lst in (assignments.items() if isinstance(assignments, dict) else []):
            for a in (lst if isinstance(lst, list) else []):
                if not isinstance(a, dict) or a.get('user_id') != uid:
                    continue
                work_type_id = a.get('work_type_id', '')
                result.append({
                    "id": a.get('id', ''),
                    "object_id": oid,
                    "object_name": names.get(oid, oid),
                    "stage_id": a.get('stage_id', ''),
                    "work_type_id": work_type_id,
                    "work_type_name": pskills.skill_display_name(work_type_id) if work_type_id else '',
                    "date_from": a.get('date_from', ''),
                    "date_to": a.get('date_to', ''),
                    "assigned_at": a.get('assigned_at', ''),
                    "status": deps.assignment_status(a),
                    "decline_reason": a.get('decline_reason', ''),
                    "task_note": a.get('task_note', ''),
                })
        result.sort(key=lambda r: r['date_from'] or '', reverse=True)
        return {"assignments": result}

    @router.get("/api/objects/{object_id}/history")
    def get_object_history(object_id: str, limit: int = Query(50, ge=1, le=200),
                           user: dict = Depends(deps.get_current_user),
                           _: None = Depends(deps.require_object_access)):
        items = deps.safe_load_json(deps.object_history_file(), [])
        if not isinstance(items, list):
            items = []
        filtered = [e for e in items if str(e.get('object_id')) == str(object_id)]
        filtered.sort(key=lambda e: str(e.get('at', '')), reverse=True)
        return {"history": filtered[:limit]}

    @router.post("/api/objects/{object_id}/assign")
    def assign_user(object_id: str, body: AssignBody, user: dict = Depends(deps.get_current_user),
                    _: None = Depends(deps.require_owner)):
        key = str(object_id)
        result_holder = {}

        def _mutator(assignments):
            if key not in assignments:
                assignments[key] = []
            already = any(
                a['user_id'] == str(body.user_id) and a.get('stage_id', '') == body.stage_id
                and deps.assignment_status(a) != 'declined'
                for a in assignments[key]
            )
            if already:
                raise HTTPException(409, "Это назначение уже существует")
            for e in deps.load_abwesenheit():
                if str(e.get('user_id')) != str(body.user_id) or e.get('status') != 'approved':
                    continue
                if deps.dates_overlap(body.date_from, body.date_to, e.get('date_from', ''), e.get('date_to', '')):
                    raise HTTPException(
                        409,
                        f"Работник недоступен ({e.get('reason', 'отсутствие')}) "
                        f"{e.get('date_from')} — {e.get('date_to')}"
                    )
            for other_oid, other_list in assignments.items():
                if other_oid == key:
                    continue
                for a in other_list:
                    if a['user_id'] != str(body.user_id) or deps.assignment_status(a) == 'declined':
                        continue
                    if deps.assignment_periods_overlap(
                        {'date_from': body.date_from, 'date_to': body.date_to}, a
                    ):
                        raise HTTPException(
                            409,
                            f"Этот работник уже назначен на объект {other_oid} "
                            f"на период {a.get('date_from') or '(без даты)'} — {a.get('date_to') or '(без даты)'}"
                        )
            assigned_at = deps.utcnow_iso()
            assignment = {
                'id': uuid.uuid4().hex,
                'user_id': str(body.user_id),
                'stage_id': body.stage_id,
                'work_type_id': body.work_type_id,
                'date_from': body.date_from,
                'date_to': body.date_to,
                'assigned_at': assigned_at,
                'pending_since': assigned_at,
                'status': 'pending',
                'decline_reason': '',
                'responded_at': '',
                'task_note': body.task_note.strip()[:500],
            }
            assignments[key].append(assignment)
            result_holder['assignment'] = assignment

        deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)
        created = result_holder.get('assignment') or {}
        if created:
            worker_name = deps.object_history_worker_name(created.get('user_id', ''))
            work_label = (
                pskills.skill_display_name(created.get('work_type_id', ''))
                if created.get('work_type_id') else created.get('stage_id', '')
            )
            subtitle = ' · '.join(p for p in (worker_name, work_label, created.get('task_note', '')) if p)
            deps.append_object_history_best_effort(
                key, 'worker_assigned', f'Назначен работник: {worker_name}',
                user=user, subtitle=subtitle,
                meta={
                    "assignment_id": created.get('id', ''),
                    "worker_id": created.get('user_id', ''),
                    "work_type_id": created.get('work_type_id', ''),
                    "stage_id": created.get('stage_id', ''),
                    "date_from": created.get('date_from', ''),
                    "date_to": created.get('date_to', ''),
                },
            )
        return {"status": "ok"}

    @router.delete("/api/objects/{object_id}/assign/{user_id}")
    def unassign_user(object_id: str, user_id: str, user: dict = Depends(deps.get_current_user),
                      _: None = Depends(deps.require_owner)):
        key = str(object_id)
        uid = str(user_id)
        result_holder = {}

        def _mutator(assignments):
            lst = assignments.get(key, [])
            active_indices = [
                i for i, a in enumerate(lst)
                if str(a.get('user_id')) == uid and deps.assignment_status(a) != 'declined'
            ]
            if not active_indices:
                result_holder['not_found'] = True
                return
            if len(active_indices) > 1:
                result_holder['multiple'] = True
                return
            target_index = active_indices[0]
            assignments[key] = [a for i, a in enumerate(lst) if i != target_index]

        deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)
        if result_holder.get('not_found'):
            raise HTTPException(404, "Активное назначение этого работника не найдено")
        if result_holder.get('multiple'):
            raise HTTPException(409, "У работника несколько назначений. Используйте assignment_id.")
        return {"status": "ok"}

    @router.patch("/api/objects/{object_id}/assignments/{assignment_id}")
    def update_assignment(object_id: str, assignment_id: str, body: AssignmentUpdateBody,
                          user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        key = str(object_id)
        updates = body.dict(exclude_unset=True)
        if 'task_note' in updates and updates['task_note'] is not None:
            updates['task_note'] = updates['task_note'].strip()[:500]

        if 'work_type_id' in updates and updates['work_type_id'] is not None:
            wtype = wt.get_work_type(updates['work_type_id'])
            if wtype is None or not wtype.get('active'):
                raise HTTPException(400, "Неизвестный или неактивный вид работ")
        if 'date_from' in updates and updates['date_from'] is not None:
            deps.validate_date_str(updates['date_from'], 'date_from')
        if 'date_to' in updates and updates['date_to'] is not None:
            deps.validate_date_str(updates['date_to'], 'date_to')

        result_holder = {}

        def _mutator(assignments):
            lst = assignments.get(key, [])
            target = next((a for a in lst if a.get('id') == assignment_id), None)
            if target is None:
                result_holder['not_found'] = True
                return

            merged_work_type = updates.get('work_type_id', target.get('work_type_id'))
            merged_date_from = updates.get('date_from', target.get('date_from', ''))
            merged_date_to = updates.get('date_to', target.get('date_to', ''))
            if merged_date_from and merged_date_to and merged_date_from > merged_date_to:
                result_holder['error'] = "date_from не может быть позже date_to"
                return

            uid = str(target.get('user_id'))
            role = deps.load_roles().get(uid)
            if role != 'worker':
                result_holder['error'] = f"Пользователь {uid} не является Worker (роль: {role})"
                return

            rows = deps.cached_get_used_range('Объекты')
            object_row = None
            if rows:
                header, data = rows[0], rows[1:]
                for r in data:
                    row = dict(zip(header, r))
                    if str(row.get('ID объекта', '')) == str(object_id):
                        object_row = row
                        break
            if object_row is None:
                result_holder['error'] = "Объект не найден"
                return

            abwesenheit = deps.load_abwesenheit()
            if any(str(e.get('user_id')) == uid and e.get('status') == 'approved'
                   and deps.dates_overlap(merged_date_from, merged_date_to, e.get('date_from', ''), e.get('date_to', ''))
                   for e in abwesenheit):
                result_holder['error'] = "Работник недоступен (отсутствие) на этот период"
                return

            overlap = False
            for other_oid, other_list in assignments.items():
                for a in other_list:
                    if a.get('id') == assignment_id:
                        continue
                    if str(a.get('user_id')) != uid or deps.assignment_status(a) == 'declined':
                        continue
                    if other_oid == key:
                        continue
                    if deps.assignment_periods_overlap(
                        {'date_from': merged_date_from, 'date_to': merged_date_to}, a
                    ):
                        overlap = True
                        break
                if overlap:
                    break
            if overlap:
                result_holder['error'] = "Пересекается с другим назначением этого работника на другом объекте"
                return

            result_holder['ok'] = True
            was_accepted = deps.assignment_status(target) == 'accepted'
            significant_change = (
                merged_work_type != target.get('work_type_id') or
                merged_date_from != target.get('date_from', '') or
                merged_date_to != target.get('date_to', '') or
                ('task_note' in updates and updates['task_note'] != target.get('task_note', ''))
            )
            updated_at = deps.utcnow_iso()
            target.update({k: v for k, v in updates.items() if v is not None})
            if was_accepted and significant_change:
                target['status'] = 'pending'
                target['decline_reason'] = ''
                target['responded_at'] = ''
                target['pending_since'] = updated_at
            target['updated_at'] = updated_at

        deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)
        if result_holder.get('not_found'):
            raise HTTPException(404, "Назначение не найдено")
        if result_holder.get('error'):
            raise HTTPException(
                409 if 'Пересекается' in result_holder['error'] or 'недоступен' in result_holder['error'] else 400,
                result_holder['error'],
            )
        return {"status": "ok"}

    @router.delete("/api/objects/{object_id}/assignments/{assignment_id}")
    def delete_assignment(object_id: str, assignment_id: str,
                          user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        key = str(object_id)
        found = {}

        def _mutator(assignments):
            lst = assignments.get(key, [])
            if not any(a.get('id') == assignment_id for a in lst):
                return
            found['ok'] = True
            assignments[key] = [a for a in lst if a.get('id') != assignment_id]

        deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)
        if not found.get('ok'):
            raise HTTPException(404, "Назначение не найдено")
        return {"status": "ok"}

    @router.post("/api/objects/{object_id}/assign/{user_id}/respond")
    def respond_to_assignment(object_id: str, user_id: str, body: AssignmentRespondBody,
                              user: dict = Depends(deps.get_current_user)):
        if str(user['id']) != str(user_id):
            raise HTTPException(403, "Можно отвечать только на собственное назначение")
        if not body.accept and not body.decline_reason.strip():
            raise HTTPException(400, "Укажите причину отказа")
        key = str(object_id)

        def _mutator(assignments):
            lst = assignments.get(key, [])
            if body.assignment_id:
                target = next((a for a in lst if a.get('id') == body.assignment_id
                               and a['user_id'] == str(user_id) and deps.assignment_status(a) == 'pending'), None)
            else:
                target = next((a for a in lst if a['user_id'] == str(user_id)
                               and deps.assignment_status(a) == 'pending'), None)
            if not target:
                raise HTTPException(404, "Ожидающее назначение не найдено")
            target['status'] = 'accepted' if body.accept else 'declined'
            target['decline_reason'] = body.decline_reason.strip()[:500] if not body.accept else ''
            target['responded_at'] = deps.utcnow_iso()
            return target

        return deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)

    @router.get("/api/assignment-candidates")
    def get_assignment_candidates(object_id: str, work_type_id: str, date_from: str, date_to: str,
                                  user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        if not deps.sanitize_display_name(work_type_id, ''):
            raise HTTPException(400, "work_type_id обязателен")
        deps.validate_date_str(date_from, 'date_from')
        deps.validate_date_str(date_to, 'date_to')

        roles = deps.load_roles()
        profiles = deps.load_worker_profiles()
        worker_ids = {uid for uid, r in roles.items() if r == 'worker'}

        workers = []
        for uid in worker_ids:
            profile = profiles.get(uid, {})
            name = deps.sanitize_display_name(profile.get('name'), uid)
            workers.append({
                "user_id": uid, "name": name,
                "has_avatar": bool(profile.get('avatar')),
                "profile": profile,
            })

        return amatch.build_candidates(
            work_type_id, object_id, date_from, date_to,
            workers, deps.load_assignments(), deps.load_abwesenheit(), deps.load_checkin_meta(),
        )

    @router.post("/api/objects/{object_id}/assignments/batch")
    def batch_assign(object_id: str, body: BatchAssignBody,
                     user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        work_type_ids = list(dict.fromkeys(body.work_type_ids))
        if not work_type_ids:
            raise HTTPException(400, "Укажите хотя бы один вид работ")
        wtypes = {}
        for wtid in work_type_ids:
            wtype = wt.get_work_type(wtid)
            if wtype is None or not wtype.get('active'):
                raise HTTPException(400, "Неизвестный или неактивный вид работ")
            wtypes[wtid] = wtype

        rows = deps.cached_get_used_range('Объекты')
        object_row = None
        if rows:
            header, data = rows[0], rows[1:]
            for r in data:
                row = dict(zip(header, r))
                if str(row.get('ID объекта', '')) == str(object_id):
                    object_row = row
                    break
        if object_row is None:
            raise HTTPException(404, "Объект не найден")
        if object_row.get('Статус') == 'Завершён':
            raise HTTPException(400, "Объект завершён, назначение недоступно")

        user_ids = list(dict.fromkeys(body.user_ids))
        if not user_ids:
            raise HTTPException(400, "Укажите хотя бы одного работника")
        deps.validate_date_str(body.date_from, 'date_from')
        deps.validate_date_str(body.date_to, 'date_to')
        if body.date_from > body.date_to:
            raise HTTPException(400, "date_from не может быть позже date_to")
        task_note = body.task_note.strip()[:500]

        roles = deps.load_roles()
        for uid in user_ids:
            role = roles.get(uid)
            if role is None:
                raise HTTPException(400, f"Пользователь {uid} не найден в списке доступа")
            if role != 'worker':
                raise HTTPException(400, f"Пользователь {uid} не является Worker (роль: {role})")

        key = str(object_id)
        result_holder = {"created": [], "skipped": []}

        def _mutator(assignments):
            if key not in assignments:
                assignments[key] = []
            abwesenheit = deps.load_abwesenheit()
            created, skipped = [], []
            for uid in user_ids:
                for wtid in work_type_ids:
                    dup = any(
                        a['user_id'] == uid and a.get('work_type_id') == wtid
                        and deps.assignment_status(a) != 'declined'
                        and deps.dates_overlap(body.date_from, body.date_to, a.get('date_from', ''), a.get('date_to', ''))
                        for a in assignments[key]
                    )
                    if dup:
                        skipped.append({"user_id": uid, "work_type_id": wtid, "reason": "overlap"})
                        continue
                    absence_hit = any(
                        str(e.get('user_id')) == uid and e.get('status') == 'approved'
                        and deps.dates_overlap(body.date_from, body.date_to, e.get('date_from', ''), e.get('date_to', ''))
                        for e in abwesenheit
                    )
                    if absence_hit:
                        skipped.append({"user_id": uid, "work_type_id": wtid, "reason": "absence"})
                        continue
                    cross_object_hit = False
                    for other_oid, other_list in assignments.items():
                        if other_oid == key:
                            continue
                        if any(a['user_id'] == uid and deps.assignment_status(a) != 'declined'
                               and deps.assignment_periods_overlap(
                                   {'date_from': body.date_from, 'date_to': body.date_to}, a
                               )
                               for a in other_list):
                            cross_object_hit = True
                            break
                    if cross_object_hit:
                        skipped.append({"user_id": uid, "work_type_id": wtid, "reason": "overlap"})
                        continue
                    assignment_id = uuid.uuid4().hex
                    assigned_at = deps.utcnow_iso()
                    assignments[key].append({
                        'id': assignment_id,
                        'user_id': uid,
                        'stage_id': wtypes[wtid]['name'],
                        'work_type_id': wtid,
                        'date_from': body.date_from,
                        'date_to': body.date_to,
                        'assigned_at': assigned_at,
                        'pending_since': assigned_at,
                        'status': 'pending',
                        'decline_reason': '',
                        'responded_at': '',
                        'task_note': task_note,
                        'created_by': str(user['id']),
                    })
                    created.append({"user_id": uid, "work_type_id": wtid, "assignment_id": assignment_id})
            result_holder['created'] = created
            result_holder['skipped'] = skipped

        deps.update_json_transaction(deps.object_assignments_file(), {}, _mutator)
        if not result_holder['created']:
            raise HTTPException(409, "Ни одно назначение не создано (все пропущены)")
        for created in result_holder['created']:
            worker_name = deps.object_history_worker_name(created.get('user_id', ''))
            wtid = created.get('work_type_id', '')
            work_label = wtypes.get(wtid, {}).get('name') or (pskills.skill_display_name(wtid) if wtid else '')
            subtitle = ' · '.join(p for p in (worker_name, work_label, task_note) if p)
            deps.append_object_history_best_effort(
                key, 'worker_assigned', f'Назначен работник: {worker_name}',
                user=user, subtitle=subtitle,
                meta={
                    "assignment_id": created.get('assignment_id', ''),
                    "worker_id": created.get('user_id', ''),
                    "work_type_id": wtid,
                    "date_from": body.date_from,
                    "date_to": body.date_to,
                },
            )
        return result_holder

    @router.get("/api/objects/{object_id}/description")
    def get_object_description(object_id: str, user: dict = Depends(deps.get_current_user),
                               _: None = Depends(deps.require_object_access)):
        return {"description": deps.object_info_entry(object_id).get("description", "")}

    @router.patch("/api/objects/{object_id}/description")
    def update_object_description(object_id: str, body: ObjectDescriptionBody,
                                  user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        description = body.description.strip()[:2000]

        def _mutator(data):
            entry = deps.ensure_object_info_entry(data, object_id)
            entry["description"] = description
            return entry["description"]

        saved = deps.update_json_transaction(deps.object_info_file(), {}, _mutator)
        return {"description": saved}

    @router.get("/api/objects/{object_id}/info-items")
    def get_object_info_items(object_id: str, user: dict = Depends(deps.get_current_user),
                              _: None = Depends(deps.require_object_access)):
        return {"items": deps.object_info_entry(object_id).get("items", [])}

    @router.post("/api/objects/{object_id}/info-items")
    def create_object_info_item(object_id: str, body: InfoItemBody,
                                user: dict = Depends(deps.get_current_user),
                                _: None = Depends(deps.require_object_access)):
        if not body.text.strip():
            raise HTTPException(400, "Текст не может быть пустым")
        item = {
            "id": uuid.uuid4().hex,
            "text": body.text.strip()[:300],
            "qty": body.qty.strip()[:50],
            "created_by": user.get('first_name', str(user['id'])),
            "created_at": int(time.time()),
        }

        def _mutator(data):
            entry = deps.ensure_object_info_entry(data, object_id)
            entry["items"].append(item)
            return item

        deps.update_json_transaction(deps.object_info_file(), {}, _mutator)
        return {"item": item}

    @router.delete("/api/objects/{object_id}/info-items/{item_id}")
    def delete_object_info_item(object_id: str, item_id: str, user: dict = Depends(deps.get_current_user),
                                _: None = Depends(deps.require_owner)):
        def _mutator(data):
            entry = data.get(object_id)
            if not entry:
                raise HTTPException(404, "Не найдено")
            before = len(entry.get("items", []))
            entry["items"] = [i for i in entry.get("items", []) if i["id"] != item_id]
            if len(entry["items"]) == before:
                raise HTTPException(404, "Не найдено")

        deps.update_json_transaction(deps.object_info_file(), {}, _mutator)
        return {"status": "ok"}

    @router.post("/api/objects")
    def create_object_endpoint(body: NewObjectBody, background_tasks: BackgroundTasks,
                               user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        create_object_script = deps.create_object_script()
        create_object_folder_script = deps.create_object_folder_script()
        deps.require_server_script(create_object_script, "Скрипт создания объекта")
        deps.require_server_script(create_object_folder_script, "Скрипт создания папки объекта")

        args = [sys.executable, create_object_script, body.name, body.adresse, body.budget]
        if body.start:
            args.append(f'--start={body.start}')
        if body.end:
            args.append(f'--end={body.end}')
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise HTTPException(500, f'Objekt-Erstellung fehlgeschlagen: {result.stderr[-500:]}')

        object_id = None
        for line in result.stdout.splitlines():
            if line.startswith('OK: '):
                object_id = line.split(' ')[1]
                break

        if object_id:
            background_tasks.add_task(
                subprocess.run,
                [sys.executable, create_object_folder_script, object_id, body.name],
                capture_output=True, text=True, timeout=30
            )

        return {"result": result.stdout.strip(), "object_id": object_id}

    @router.patch("/api/objects/{object_id}/status")
    def update_object_status(object_id: str, body: StatusBody,
                             user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        if body.status not in VALID_OBJECT_STATUSES:
            raise HTTPException(400, f'Недопустимый статус: {body.status}')
        o = deps.load_repo_objekte_lib()
        old_status = ''
        try:
            rows = deps.cached_get_used_range('Объекты')
            if rows:
                header, data = rows[0], rows[1:]
                for r in data:
                    obj = dict(zip(header, r))
                    if str(obj.get('ID объекта', '')) == str(object_id):
                        old_status = obj.get('Статус', '')
                        break
        except Exception:
            old_status = ''
        try:
            o.update_object_field(object_id, 'Статус', body.status)
        except ValueError as e:
            raise HTTPException(404, str(e))
        subtitle = f'{old_status} -> {body.status}' if old_status else body.status
        deps.append_object_history_best_effort(
            object_id, 'object_status_changed', 'Статус объекта изменён',
            user=user, subtitle=subtitle,
            meta={"old_status": old_status, "new_status": body.status},
        )
        return {"status": "ok"}

    handlers = SimpleNamespace(
        list_objects=list_objects,
        my_assignments=my_assignments,
        get_object_history=get_object_history,
        assign_user=assign_user,
        unassign_user=unassign_user,
        update_assignment=update_assignment,
        delete_assignment=delete_assignment,
        respond_to_assignment=respond_to_assignment,
        get_assignment_candidates=get_assignment_candidates,
        batch_assign=batch_assign,
        get_object_description=get_object_description,
        update_object_description=update_object_description,
        get_object_info_items=get_object_info_items,
        create_object_info_item=create_object_info_item,
        delete_object_info_item=delete_object_info_item,
        create_object_endpoint=create_object_endpoint,
        update_object_status=update_object_status,
    )
    return router, handlers
