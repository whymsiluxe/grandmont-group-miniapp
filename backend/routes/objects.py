"""Objects-domain HTTP routes.

This module deliberately does not import backend.main. main.py wires the
runtime dependencies through ObjectsRouteDeps, then includes the returned
router and re-exports the handlers for legacy direct-call tests.
"""
from __future__ import annotations

import logging
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
    from ..services.object_assignments import (
        ObjectAssignmentDeps, ObjectAssignmentError, ObjectAssignmentService,
    )
except ImportError:
    import assignment_matching as amatch  # noqa: E402
    import profile_skills as pskills  # noqa: E402
    import work_types as wt  # noqa: E402
    from core.constants import VALID_OBJECT_STATUSES  # noqa: E402
    from services.object_assignments import (  # noqa: E402
        ObjectAssignmentDeps, ObjectAssignmentError, ObjectAssignmentService,
    )


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
    shadow_compare_assignments: Callable | None = None


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
    assignment_service = ObjectAssignmentService(ObjectAssignmentDeps(
        cached_get_used_range=deps.cached_get_used_range,
        load_assignments=deps.load_assignments,
        load_worker_profiles=deps.load_worker_profiles,
        load_abwesenheit=deps.load_abwesenheit,
        load_roles=deps.load_roles,
        load_checkin_meta=deps.load_checkin_meta,
        update_json_transaction=deps.update_json_transaction,
        object_assignments_file=deps.object_assignments_file,
        validate_date_str=deps.validate_date_str,
        dates_overlap=deps.dates_overlap,
        assignment_periods_overlap=deps.assignment_periods_overlap,
        assignment_status=deps.assignment_status,
        utcnow_iso=deps.utcnow_iso,
        sanitize_display_name=deps.sanitize_display_name,
        object_history_worker_name=deps.object_history_worker_name,
        append_object_history_best_effort=deps.append_object_history_best_effort,
        shadow_compare_assignments=deps.shadow_compare_assignments,
        get_work_type=wt.get_work_type,
        skill_display_name=pskills.skill_display_name,
        build_candidates=amatch.build_candidates,
        new_id=lambda: uuid.uuid4().hex,
    ))

    def call_assignment(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ObjectAssignmentError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

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
        return call_assignment(assignment_service.my_assignments, str(user['id']))

    @router.get("/api/objects/{object_id}/history")
    def get_object_history(object_id: str, limit: int = Query(50, ge=1, le=200),
                           user: dict = Depends(deps.get_current_user),
                           _: None = Depends(deps.require_object_access)):
        items = deps.safe_load_json(deps.object_history_file(), [])
        if not isinstance(items, list):
            items = []
        filtered = [e for e in items if isinstance(e, dict) and str(e.get('object_id')) == str(object_id)]
        filtered.sort(key=lambda e: str(e.get('at', '')), reverse=True)
        return {"history": filtered[:limit]}

    @router.post("/api/objects/{object_id}/assign")
    def assign_user(object_id: str, body: AssignBody, user: dict = Depends(deps.get_current_user),
          _: None = Depends(deps.require_owner)):
        return call_assignment(assignment_service.assign_user, object_id, body.model_dump(), user)

    @router.delete("/api/objects/{object_id}/assign/{user_id}")
    def unassign_user(object_id: str, user_id: str, user: dict = Depends(deps.get_current_user),
            _: None = Depends(deps.require_owner)):
        return call_assignment(assignment_service.unassign_user, object_id, user_id)

    @router.patch("/api/objects/{object_id}/assignments/{assignment_id}")
    def update_assignment(object_id: str, assignment_id: str, body: AssignmentUpdateBody,
                user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        return call_assignment(
            assignment_service.update_assignment,
            object_id,
            assignment_id,
            body.model_dump(exclude_unset=True),
        )

    @router.delete("/api/objects/{object_id}/assignments/{assignment_id}")
    def delete_assignment(object_id: str, assignment_id: str,
                user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        return call_assignment(assignment_service.delete_assignment, object_id, assignment_id)

    @router.post("/api/objects/{object_id}/assign/{user_id}/respond")
    def respond_to_assignment(object_id: str, user_id: str, body: AssignmentRespondBody,
          user: dict = Depends(deps.get_current_user)):
        if str(user['id']) != str(user_id):
            raise HTTPException(403, "Можно отвечать только на собственное назначение")
        return call_assignment(
            assignment_service.respond_to_assignment,
            object_id,
            user_id,
            assignment_id=body.assignment_id,
            accept=body.accept,
            decline_reason=body.decline_reason,
        )

    @router.get("/api/assignment-candidates")
    def get_assignment_candidates(object_id: str, work_type_id: str, date_from: str, date_to: str,
              user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        return call_assignment(
            assignment_service.assignment_candidates,
            object_id,
            work_type_id,
            date_from,
            date_to,
        )

    @router.post("/api/objects/{object_id}/assignments/batch")
    def batch_assign(object_id: str, body: BatchAssignBody,
           user: dict = Depends(deps.get_current_user), _: None = Depends(deps.require_owner)):
        return call_assignment(assignment_service.batch_assign, object_id, body.model_dump(), user)

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
