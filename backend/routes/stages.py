"""HTTP adapter for stages, roadmap, and stage-change requests."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

try:
    from ..services.stages import StageService, StageServiceDeps, StageServiceError
except ImportError:
    from services.stages import StageService, StageServiceDeps, StageServiceError  # noqa: E402


class NewStageBody(BaseModel):
    name: str
    description: str = ''


class StageDescriptionBody(BaseModel):
    description: str


class StageStatusBody(BaseModel):
    status: str


class StageSwapBody(BaseModel):
    row_num_b: int


class StageBlockerBody(BaseModel):
    quick_reason: str = ''
    comment: str = ''
    photo_url: str = ''
    who_decides: str = ''
    expected_date: str = ''


class RoadmapCategoryBody(BaseModel):
    title: str


class RoadmapItemCreateBody(BaseModel):
    title: str
    category_id: str | None = None
    description: str = ''
    required: bool = True
    safety_critical: bool = False
    weight: int = 1


class RoadmapItemEditBody(BaseModel):
    title: str | None = None
    description: str | None = None
    required: bool | None = None
    safety_critical: bool | None = None
    weight: int | None = None
    category_id: str | None = None


class RoadmapItemStatusBody(BaseModel):
    status: str


class RoadmapNoteBody(BaseModel):
    text: str
    item_id: str | None = None


class StageRequestBody(BaseModel):
    kind: str
    new_status: str = ''


class StageRequestDecisionBody(BaseModel):
    approve: bool


@dataclass(frozen=True)
class StagesRouteDeps:
    get_current_user: Callable
    get_role: Callable
    require_owner: Callable
    require_object_access: Callable
    cached_get_used_range: Callable
    load_repo_objekte_lib: Callable
    roadmap_lib: Callable
    safe_load_json: Callable
    update_json_transaction: Callable
    business_today_str: Callable
    append_object_history_best_effort: Callable
    get_worker_profile: Callable
    sanitize_display_name: Callable
    load_roles: Callable
    create_critical_alert: Callable
    send_telegram_message: Callable


def create_stages_router(deps: StagesRouteDeps):
    router = APIRouter()
    service = StageService(StageServiceDeps(
        cached_get_used_range=deps.cached_get_used_range,
        load_repo_objekte_lib=deps.load_repo_objekte_lib,
        roadmap_lib=deps.roadmap_lib,
        safe_load_json=deps.safe_load_json,
        update_json_transaction=deps.update_json_transaction,
        business_today_str=deps.business_today_str,
        append_object_history_best_effort=deps.append_object_history_best_effort,
        get_worker_profile=deps.get_worker_profile,
        sanitize_display_name=deps.sanitize_display_name,
        load_roles=deps.load_roles,
        create_critical_alert=deps.create_critical_alert,
        send_telegram_message=deps.send_telegram_message,
    ))

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StageServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    @router.get('/api/objects/{object_id}/stages')
    def get_stages(object_id: str, user: dict = Depends(deps.get_current_user)):
        return call(service.list_stages, object_id)

    @router.post('/api/objects/{object_id}/stages')
    def create_stage(object_id: str, body: NewStageBody,
                     user: dict = Depends(deps.get_current_user),
                     _: None = Depends(deps.require_object_access)):
        return call(service.create_stage, object_id, body.name, body.description)

    @router.patch('/api/objects/{object_id}/stages/{row_num}/description')
    def update_stage_description_endpoint(object_id: str, row_num: int, body: StageDescriptionBody,
                                          user: dict = Depends(deps.get_current_user),
                                          _: None = Depends(deps.require_object_access)):
        return call(service.update_stage_description, object_id, row_num, body.description)

    @router.patch('/api/objects/{object_id}/stages/{row_num}')
    def update_stage(object_id: str, row_num: int, body: StageStatusBody,
                     user: dict = Depends(deps.get_current_user),
                     _: None = Depends(deps.require_owner)):
        return call(service.update_stage_status, object_id, row_num, body.status, user)

    @router.delete('/api/objects/{object_id}/stages/{row_num}')
    def remove_stage(object_id: str, row_num: int,
                     user: dict = Depends(deps.get_current_user),
                     _: None = Depends(deps.require_owner)):
        return call(service.remove_stage, object_id, row_num)

    @router.patch('/api/objects/{object_id}/stages/{row_num}/swap')
    def swap_stage(object_id: str, row_num: int, body: StageSwapBody,
                   user: dict = Depends(deps.get_current_user),
                   _: None = Depends(deps.require_owner)):
        return call(service.swap_stage, object_id, row_num, body.row_num_b)

    @router.post('/api/objects/{object_id}/stages/{row_num}/complete')
    def worker_complete_stage(object_id: str, row_num: int,
                              user: dict = Depends(deps.get_current_user),
                              _: None = Depends(deps.require_object_access)):
        return call(service.complete_stage, object_id, row_num, user)

    @router.post('/api/objects/{object_id}/stages/{row_num}/blocker')
    def set_stage_blocker(object_id: str, row_num: int, body: StageBlockerBody,
                          user: dict = Depends(deps.get_current_user),
                          _: None = Depends(deps.require_object_access)):
        return call(
            service.set_stage_blocker, object_id, row_num, user,
            quick_reason=body.quick_reason, comment=body.comment,
            photo_url=body.photo_url, who_decides=body.who_decides,
            expected_date=body.expected_date,
        )

    @router.delete('/api/objects/{object_id}/stages/{row_num}/blocker')
    def clear_stage_blocker(object_id: str, row_num: int,
                            user: dict = Depends(deps.get_current_user),
                            _: None = Depends(deps.require_object_access)):
        return call(service.clear_stage_blocker, object_id, row_num)

    @router.get('/api/objects/{object_id}/stages/{row_num}/roadmap')
    def get_stage_roadmap(object_id: str, row_num: int,
                          user: dict = Depends(deps.get_current_user)):
        return call(service.get_stage_roadmap, object_id, row_num)

    @router.post('/api/objects/{object_id}/stages/{row_num}/roadmap/categories')
    def create_roadmap_category(object_id: str, row_num: int, body: RoadmapCategoryBody,
                                user: dict = Depends(deps.get_current_user),
                                _: None = Depends(deps.require_owner)):
        return call(service.create_roadmap_category, object_id, row_num, body.title)

    @router.delete('/api/objects/{object_id}/stages/{row_num}/roadmap/categories/{category_id}')
    def delete_roadmap_category(object_id: str, row_num: int, category_id: str,
                                user: dict = Depends(deps.get_current_user),
                                _: None = Depends(deps.require_owner)):
        return call(service.delete_roadmap_category, object_id, row_num, category_id)

    @router.post('/api/objects/{object_id}/stages/{row_num}/roadmap/items')
    def create_roadmap_item(object_id: str, row_num: int, body: RoadmapItemCreateBody,
                            user: dict = Depends(deps.get_current_user),
                            _: None = Depends(deps.require_owner)):
        return call(
            service.create_roadmap_item, object_id, row_num,
            title=body.title, category_id=body.category_id,
            description=body.description, required=body.required,
            safety_critical=body.safety_critical, weight=body.weight,
        )

    @router.patch('/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}')
    def edit_roadmap_item(object_id: str, row_num: int, item_id: str, body: RoadmapItemEditBody,
                          user: dict = Depends(deps.get_current_user),
                          _: None = Depends(deps.require_owner)):
        fields = {key: value for key, value in body.model_dump().items() if value is not None}
        return call(service.edit_roadmap_item, object_id, row_num, item_id, fields)

    @router.delete('/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}')
    def delete_roadmap_item(object_id: str, row_num: int, item_id: str,
                            user: dict = Depends(deps.get_current_user),
                            _: None = Depends(deps.require_owner)):
        return call(service.delete_roadmap_item, object_id, row_num, item_id)

    @router.post('/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}/status')
    def update_roadmap_item_status(object_id: str, row_num: int, item_id: str,
                                   body: RoadmapItemStatusBody,
                                   user: dict = Depends(deps.get_current_user),
                                   _: None = Depends(deps.require_object_access)):
        return call(service.update_roadmap_item_status,
                    object_id, row_num, item_id, body.status, user)

    @router.post('/api/objects/{object_id}/stages/{row_num}/roadmap/notes')
    def create_roadmap_note(object_id: str, row_num: int, body: RoadmapNoteBody,
                            user: dict = Depends(deps.get_current_user),
                            _: None = Depends(deps.require_object_access)):
        return call(service.create_roadmap_note,
                    object_id, row_num, body.text, body.item_id, user)

    @router.get('/api/objects/{object_id}/stages/{row_num}/roadmap/notes')
    def list_roadmap_notes(object_id: str, row_num: int, item_id: str = '',
                           user: dict = Depends(deps.get_current_user)):
        return call(service.list_roadmap_notes, object_id, row_num, item_id)

    @router.post('/api/objects/{object_id}/stages/{row_num}/request')
    def create_stage_request(object_id: str, row_num: int, body: StageRequestBody,
                             user: dict = Depends(deps.get_current_user),
                             role: str = Depends(deps.get_role),
                             _: None = Depends(deps.require_object_access)):
        return call(
            service.create_stage_request, object_id, row_num,
            kind=body.kind, new_status=body.new_status, user=user, role=role,
        )

    @router.get('/api/objects/{object_id}/stages/requests')
    def list_stage_requests(object_id: str,
                            user: dict = Depends(deps.get_current_user),
                            _: None = Depends(deps.require_owner)):
        return call(service.list_stage_requests, object_id)

    @router.post('/api/objects/{object_id}/stages/requests/{request_id}/decide')
    def decide_stage_request_endpoint(object_id: str, request_id: str,
                                      body: StageRequestDecisionBody,
                                      user: dict = Depends(deps.get_current_user),
                                      _: None = Depends(deps.require_owner)):
        return call(service.decide_stage_request,
                    object_id, request_id, body.approve, user)

    return router, SimpleNamespace(
        get_stages=get_stages,
        create_stage=create_stage,
        update_stage_description_endpoint=update_stage_description_endpoint,
        update_stage=update_stage,
        remove_stage=remove_stage,
        swap_stage=swap_stage,
        worker_complete_stage=worker_complete_stage,
        set_stage_blocker=set_stage_blocker,
        clear_stage_blocker=clear_stage_blocker,
        get_stage_roadmap=get_stage_roadmap,
        create_roadmap_category=create_roadmap_category,
        delete_roadmap_category=delete_roadmap_category,
        create_roadmap_item=create_roadmap_item,
        edit_roadmap_item=edit_roadmap_item,
        delete_roadmap_item=delete_roadmap_item,
        update_roadmap_item_status=update_roadmap_item_status,
        create_roadmap_note=create_roadmap_note,
        list_roadmap_notes=list_roadmap_notes,
        create_stage_request=create_stage_request,
        list_stage_requests=list_stage_requests,
        decide_stage_request_endpoint=decide_stage_request_endpoint,
    )
