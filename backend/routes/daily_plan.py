"""DailyPlan-domain HTTP routes.

The route layer owns HTTP/auth/Pydantic concerns only. Business orchestration is
implemented by services.daily_plan and injected here without a back-edge to
main.py. Handler names and public paths stay stable for legacy direct-call tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

try:
    from .. import daily_plan_lib as dpl
    from ..services.daily_plan import (
        DailyPlanService,
        DailyPlanServiceDeps,
        DailyPlanServiceError,
    )
except ImportError:
    import daily_plan_lib as dpl  # noqa: E402
    from services.daily_plan import (  # noqa: E402
        DailyPlanService,
        DailyPlanServiceDeps,
        DailyPlanServiceError,
    )


class PlanBlockerBody(BaseModel):
    reason_code: str
    comment: str = ""


class DailyPlanItemIn(BaseModel):
    id: str = ""
    sequence: int = 0
    title: str
    objective: str = ""
    planned_quantity: float | None = None
    unit: str = ""
    time_estimate_hours: float | None = None
    work_type_id: str | None = None
    required_tools: list = []
    required_materials: list = []


class DailyPlanIn(BaseModel):
    object_id: str
    stage_key: str
    date: str
    assigned_worker_ids: list
    items: list[DailyPlanItemIn]
    publish: bool = False


class ReplanRequestBody(BaseModel):
    notes: str = ""


@dataclass(frozen=True)
class DailyPlanRouteDeps:
    get_current_user: Callable
    get_role: Callable
    require_owner: Callable
    business_today: Callable
    business_today_str: Callable
    load_roles: Callable
    load_worker_profiles: Callable
    sanitize_display_name: Callable
    create_critical_alert: Callable
    load_checkin_meta: Callable
    is_active_photo_checkin_session: Callable


def create_daily_plan_router(deps: DailyPlanRouteDeps):
    router = APIRouter()
    service = DailyPlanService(
        DailyPlanServiceDeps(
            daily_plan_lib=dpl,
            business_today_str=deps.business_today_str,
            load_roles=deps.load_roles,
            load_worker_profiles=deps.load_worker_profiles,
            sanitize_display_name=deps.sanitize_display_name,
            create_critical_alert=deps.create_critical_alert,
            load_checkin_meta=deps.load_checkin_meta,
            is_active_photo_checkin_session=deps.is_active_photo_checkin_session,
        )
    )

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DailyPlanServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def _build_daily_plan_response(worker_id: str, date_str: str) -> dict:
        return service.build_worker_plan_response(worker_id, date_str)

    @router.get("/api/daily-plan/today")
    def daily_plan_today(
        worker_id_param: str = Query("", alias="worker_id"),
        day: str = Query("today"),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if day not in ("today", "tomorrow"):
            raise HTTPException(400, "day должен быть 'today' или 'tomorrow'")
        target_date = (
            deps.business_today()
            if day == "today"
            else deps.business_today() + timedelta(days=1)
        )
        worker_id = (
            worker_id_param
            if role == "owner" and worker_id_param
            else str(user["id"])
        )
        return service.build_worker_plan_response(
            worker_id, target_date.strftime("%Y-%m-%d")
        )

    @router.post("/api/daily-plan/{plan_id}/accept")
    def daily_plan_accept(
        plan_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role == "owner":
            raise HTTPException(403, "Owner не принимает план как работник")
        return call(service.accept_plan, plan_id, str(user["id"]))

    @router.post("/api/daily-plan/{plan_id}/amendments/{amendment_id}/accept")
    def daily_plan_accept_amendment(
        plan_id: str,
        amendment_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role == "owner":
            raise HTTPException(403, "Owner не подтверждает Amendment как работник")
        return call(
            service.accept_amendment,
            plan_id,
            amendment_id,
            str(user["id"]),
        )

    @router.post("/api/daily-plan/{plan_id}/blocker")
    def daily_plan_report_blocker(
        plan_id: str,
        body: PlanBlockerBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role == "owner":
            raise HTTPException(403, "Owner не сообщает о препятствии как работник")
        return call(
            service.report_blocker,
            plan_id,
            str(user["id"]),
            body.reason_code,
            body.comment,
        )

    @router.get("/api/daily-plan/owner/today")
    def daily_plan_owner_today(
        _: None = Depends(deps.require_owner),
        user: dict = Depends(deps.get_current_user),
    ):
        return service.owner_today()

    @router.get("/api/daily-plan/object/{object_id}")
    def daily_plan_object(
        object_id: str,
        date_from: str = "",
        date_to: str = "",
        _: None = Depends(deps.require_owner),
    ):
        return service.object_plans(object_id, date_from, date_to)

    @router.post("/api/daily-plan")
    def daily_plan_create(
        body: DailyPlanIn,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        items = [item.model_dump() for item in body.items]
        return service.create_plan(
            object_id=body.object_id,
            stage_key=body.stage_key,
            date_str=body.date,
            assigned_worker_ids=body.assigned_worker_ids,
            items=items,
            publish=body.publish,
            created_by=str(user["id"]),
        )

    @router.get("/api/daily-plan/{plan_id}")
    def daily_plan_get(
        plan_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        return call(service.plan_detail, plan_id, str(user["id"]), role)

    @router.get("/api/productivity/workers/{target_user_id}")
    def get_worker_productivity_api(
        target_user_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role != "owner" and str(user["id"]) != target_user_id:
            raise HTTPException(403, "Можно смотреть только свою производительность")
        return service.worker_productivity(target_user_id)

    @router.post("/api/productivity/workers/{target_user_id}/baseline")
    def set_worker_baseline(
        target_user_id: str,
        work_type_id: str,
        baseline: float,
        _: None = Depends(deps.require_owner),
    ):
        return call(
            service.set_worker_baseline,
            target_user_id,
            work_type_id,
            baseline,
        )

    def _daily_plan_status_label(plan: dict, acceptance: dict | None, amendments: list) -> str:
        return service.daily_plan_status_label(plan, acceptance, amendments)

    def _checkin_shift_status(session_key, active: dict, finished: dict) -> str:
        return service.checkin_shift_status(session_key, active, finished)

    def _execution_summary(execution: dict | None) -> dict | None:
        return service.execution_summary(execution)

    def _compute_risk_level(
        carryovers: list,
        amendments: list,
        blockers_for_plan: list,
        execution: dict | None,
        predicted_finish_date: str | None = None,
        contract_finish_date: str | None = None,
        internal_target_date: str | None = None,
    ) -> str:
        return service.compute_risk_level(
            carryovers,
            amendments,
            blockers_for_plan,
            execution,
            predicted_finish_date,
            contract_finish_date,
            internal_target_date,
        )

    @router.get("/api/daily-plan/owner/matrix")
    def daily_plan_owner_matrix(
        date_from: str = "",
        date_to: str = "",
        object_id: str = "",
        _: None = Depends(deps.require_owner),
    ):
        return service.owner_matrix(date_from, date_to, object_id)

    @router.post("/api/daily-plan/replan/{object_id}")
    def daily_plan_replan(
        object_id: str,
        body: ReplanRequestBody = ReplanRequestBody(),
        _: None = Depends(deps.require_owner),
    ):
        return service.replan(object_id)

    handlers = SimpleNamespace(
        daily_plan_today=daily_plan_today,
        daily_plan_accept=daily_plan_accept,
        daily_plan_accept_amendment=daily_plan_accept_amendment,
        daily_plan_report_blocker=daily_plan_report_blocker,
        daily_plan_owner_today=daily_plan_owner_today,
        daily_plan_object=daily_plan_object,
        daily_plan_create=daily_plan_create,
        daily_plan_get=daily_plan_get,
        get_worker_productivity_api=get_worker_productivity_api,
        set_worker_baseline=set_worker_baseline,
        daily_plan_owner_matrix=daily_plan_owner_matrix,
        daily_plan_replan=daily_plan_replan,
        build_daily_plan_response=_build_daily_plan_response,
        daily_plan_status_label=_daily_plan_status_label,
        checkin_shift_status=_checkin_shift_status,
        execution_summary=_execution_summary,
        compute_risk_level=_compute_risk_level,
    )
    return router, handlers
