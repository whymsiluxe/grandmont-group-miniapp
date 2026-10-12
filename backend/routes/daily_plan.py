"""DailyPlan-domain HTTP routes (Production Control: publish/accept/amend/
blocker/owner matrix/replan) plus the two dpl-backed productivity endpoints
that live inside the same contiguous block in the original main.py.

This module deliberately does not import backend.main. main.py wires the
runtime dependencies through DailyPlanRouteDeps, then includes the returned
router and re-exports the handlers for legacy direct-call tests.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

try:
    from .. import daily_plan_lib as dpl
except ImportError:
    import daily_plan_lib as dpl  # noqa: E402


_PLAN_BLOCKER_REASON_LABELS = {
    'material_missing': 'нет материалов',
    'tool_missing': 'нет инструмента',
    'access_denied': 'нет доступа на объект',
    'safety_concern': 'проблема безопасности',
    'weather': 'погодные условия',
    'coordinator_missing': 'нет ответственного лица',
    'other': 'другое',
}


class PlanBlockerBody(BaseModel):
    reason_code: str
    comment: str = ''


class DailyPlanItemIn(BaseModel):
    id: str = ''
    sequence: int = 0
    title: str
    objective: str = ''
    planned_quantity: float | None = None
    unit: str = ''
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
    notes: str = ''


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

    def _build_daily_plan_response(worker_id: str, date_str: str) -> dict:
        plan = dpl.get_today_plan_for_worker(worker_id, date_str)
        if not plan:
            return {"has_plan": False, "date": date_str}

        carryovers = dpl.get_carryovers_for_worker(worker_id, date_str)
        acceptance = dpl.get_acceptance(plan["id"], worker_id)
        amendments = dpl.get_pending_amendments(plan["id"], worker_id)

        # Если план принят — отдаём snapshot принятой версии, не текущий Sheet
        if acceptance:
            accepted_items = dpl.get_accepted_snapshot(plan["id"], worker_id)
        else:
            accepted_items = plan["items"]

        return {
            "has_plan": True,
            "plan": {
                "id": plan["id"],
                "object_id": plan["object_id"],
                "stage_key": plan["stage_key"],
                "date": plan["date"],
                "status": plan["status"],
                "version": plan["version"],
                "items": accepted_items,
                "published_at": plan.get("published_at"),
            },
            "acceptance": acceptance,
            "pending_amendments": amendments,
            "carryovers": carryovers,
            "date": date_str,
        }

    @router.get("/api/daily-plan/today")
    def daily_plan_today(
        worker_id_param: str = Query('', alias='worker_id'),
        day: str = Query('today'),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Работник видит свой план на сегодня (или сообщение «нет плана»).
        Owner может смотреть план за любого worker: ?worker_id=<id>.
        18.09: ?day=tomorrow -- предпросмотр завтрашнего плана (owner попросил, чтобы
        работник заранее знал какой инструмент/материал готовить). Тот же shape ответа,
        accept_plan()/daily_plan_lib уже date-agnostic -- завтрашний план можно принять
        тем же /accept endpoint'ом, ничего дополнительно строить не нужно."""
        if day not in ('today', 'tomorrow'):
            raise HTTPException(400, "day должен быть 'today' или 'tomorrow'")
        target_date = deps.business_today() if day == 'today' else deps.business_today() + timedelta(days=1)
        target_date_str = target_date.strftime('%Y-%m-%d')

        if role == 'owner' and worker_id_param:
            worker_id = worker_id_param
        else:
            worker_id = str(user['id'])

        return _build_daily_plan_response(worker_id, target_date_str)

    @router.post("/api/daily-plan/{plan_id}/accept")
    def daily_plan_accept(
        plan_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Работник принимает план: «ПЛАН ПОНЯТЕН — БЕРУ В РАБОТУ»."""
        if role == 'owner':
            raise HTTPException(403, "Owner не принимает план как работник")
        plan = dpl.get_plan(plan_id)
        if not plan:
            raise HTTPException(404, "План не найден")
        # Статус-гейт живёт в dpl.accept_plan() (включая 'accepted' для multi-worker
        # планов, где один работник уже принял) -- не дублировать его здесь со
        # старым списком статусов, которые расходятся с библиотекой.

        try:
            acceptance = dpl.accept_plan(plan_id, plan["version"], str(user['id']))
        except PermissionError:
            raise HTTPException(403, "Вы не назначены на этот план")
        except dpl.StaleAcceptanceError as e:
            # P0 fix: plan changed between this route's get_plan() read and
            # accept_plan()'s own lock-protected re-check -- 409, not 400, so
            # the client knows to reload the plan and retry, not that its
            # request was malformed.
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

        return {"status": "accepted", "acceptance": acceptance}

    @router.post("/api/daily-plan/{plan_id}/amendments/{amendment_id}/accept")
    def daily_plan_accept_amendment(
        plan_id: str,
        amendment_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Работник подтверждает Amendment после Start."""
        if role == 'owner':
            raise HTTPException(403, "Owner не подтверждает Amendment как работник")
        try:
            amendment = dpl.acknowledge_amendment(plan_id, amendment_id, str(user['id']))
        except KeyError:
            raise HTTPException(404, "Amendment не найден")
        except PermissionError:
            raise HTTPException(403, "Вы не назначены на этот план")

        return {"status": "acknowledged", "amendment": amendment}

    @router.post("/api/daily-plan/{plan_id}/blocker")
    def daily_plan_report_blocker(
        plan_id: str,
        body: PlanBlockerBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Работник сообщает о препятствии при утреннем принятии — «ЕСТЬ ПРЕПЯТСТВИЕ».
        Сохраняет причину + отправляет critical alert владельцу."""
        if role == 'owner':
            raise HTTPException(403, "Owner не сообщает о препятствии как работник")
        plan = dpl.get_plan(plan_id)
        if not plan:
            raise HTTPException(404, "План не найден")
        if str(user['id']) not in [str(w) for w in plan.get('assigned_worker_ids', [])]:
            raise HTTPException(403, "Вы не назначены на этот план")
        if body.reason_code not in _PLAN_BLOCKER_REASON_LABELS:
            raise HTTPException(400, f"Недопустимый reason_code. Допустимые: {list(_PLAN_BLOCKER_REASON_LABELS)}")

        blocker = dpl.record_blocker(plan_id, str(user['id']), body.reason_code, body.comment.strip()[:500])

        roles = deps.load_roles()
        owner_id = next((uid for uid, r in roles.items() if r == 'owner'), None)
        if owner_id:
            profiles = deps.load_worker_profiles()
            worker_name = deps.sanitize_display_name(
                profiles.get(str(user['id']), {}).get('name'), str(user['id'])
            )
            reason_label = _PLAN_BLOCKER_REASON_LABELS.get(body.reason_code, body.reason_code)
            deps.create_critical_alert(
                target_user_id=owner_id,
                kind='plan_blocker',
                title=f"{worker_name} не может начать — {reason_label}",
                subtitle=body.comment[:100] if body.comment else '',
                ref_id=plan_id,
            )

        return {"status": "recorded", "blocker": blocker}

    @router.get("/api/daily-plan/owner/today")
    def daily_plan_owner_today(
        _: None = Depends(deps.require_owner),
        user: dict = Depends(deps.get_current_user),
    ):
        """Owner Контроль дня — unified DTO. Собирается из local DailyPlan store + checkin_meta.
        Без N+1 Sheets-вызовов: все Sheets-данные приходят через plan_sync_state.json (кэш).
        """
        today = deps.business_today_str()
        plans = dpl.get_pending_plans_for_owner_today(today)

        checkin_items = deps.load_checkin_meta()
        # Item 9: key sessions on (worker_id, object_id) not worker_id alone -- a
        # worker with two same-day shifts on different objects must not have one
        # session silently overwrite the other in this dict.
        active_sessions = {
            (str(s["user_id"]), str(s.get("object_id", ""))): s for s in checkin_items
            if s.get("date") == today and deps.is_active_photo_checkin_session(s)
        }
        finished_sessions = {
            (str(s["user_id"]), str(s.get("object_id", ""))): s for s in checkin_items
            if s.get("date") == today and s.get("finish_at") is not None
        }

        rows = []
        for plan in plans:
            for worker_id in plan.get("assigned_worker_ids", []):
                wid = str(worker_id)
                skey = (wid, str(plan.get("object_id", "")))
                acceptance = dpl.get_acceptance(plan["id"], wid)
                amendments = dpl.get_pending_amendments(plan["id"], wid)
                execution = None
                session = finished_sessions.get(skey) or active_sessions.get(skey)
                if session and session.get("finish_at"):
                    execution = dpl.get_execution(session["id"])
                carryovers = dpl.get_carryovers_for_worker(wid, today)

                plan_status_label = _daily_plan_status_label(plan, acceptance, amendments)
                shift_status = _checkin_shift_status(skey, active_sessions, finished_sessions)

                plan_blockers = dpl.get_blockers_for_plan(plan["id"])
                risk_level = _compute_risk_level(carryovers, amendments, plan_blockers, execution)
                row = {
                    "plan_id": plan["id"],
                    "worker_id": wid,
                    "object_id": plan["object_id"],
                    "date": today,
                    "plan_summary": {
                        "stage_key": plan["stage_key"],
                        "item_count": len(plan["items"]),
                        "version": plan["version"],
                    },
                    "plan_status": plan["status"],
                    "plan_status_label": plan_status_label,
                    "acceptance": {
                        "accepted_at": acceptance["accepted_at"] if acceptance else None,
                        "plan_version": acceptance["plan_version"] if acceptance else None,
                    } if acceptance else None,
                    "shift_status": shift_status,
                    "pending_amendments_count": len(amendments),
                    "carryover_count": len(carryovers),
                    "execution_summary": _execution_summary(execution) if execution else None,
                    "risk_level": risk_level,
                }
                rows.append(row)

        total = len(rows)
        accepted = sum(1 for r in rows if r["acceptance"] is not None)
        working = sum(1 for r in rows if r["shift_status"] == "working")
        finished = sum(1 for r in rows if r["shift_status"] == "finished")
        carryover_count = sum(1 for r in rows if r["carryover_count"] > 0)

        return {
            "date": today,
            "summary": {
                "total_plans": total,
                "accepted": accepted,
                "working": working,
                "finished": finished,
                "has_carryovers": carryover_count,
            },
            "rows": rows,
        }

    @router.get("/api/daily-plan/object/{object_id}")
    def daily_plan_object(
        object_id: str,
        date_from: str = '',
        date_to: str = '',
        _: None = Depends(deps.require_owner),
    ):
        """Планы объекта за диапазон дат (для матрицы объекта)."""
        today = deps.business_today_str()
        df = date_from or today
        dt = date_to or today

        store = dpl.get_store_snapshot()
        plans = [
            p for p in store["daily_plans"].values()
            if p["object_id"] == object_id and df <= p["date"] <= dt
        ]
        plans.sort(key=lambda p: p["date"])

        result = []
        for plan in plans:
            result.append({
                "id": plan["id"],
                "date": plan["date"],
                "stage_key": plan["stage_key"],
                "status": plan["status"],
                "version": plan["version"],
                "item_count": len(plan["items"]),
                "assigned_worker_ids": plan["assigned_worker_ids"],
            })
        return {"object_id": object_id, "date_from": df, "date_to": dt, "plans": result}

    @router.post("/api/daily-plan")
    def daily_plan_create(
        body: DailyPlanIn,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        """Owner создаёт/публикует DailyPlan (вручную, без Sheets-синка)."""
        items = []
        for i, item in enumerate(body.items):
            item_id = item.id or uuid.uuid4().hex
            items.append({
                "id": item_id,
                "sequence": item.sequence or i + 1,
                "title": item.title,
                "objective": item.objective,
                "planned_quantity": item.planned_quantity,
                "unit": item.unit,
                "time_estimate_hours": item.time_estimate_hours,
                "work_type_id": item.work_type_id,
                "required_tools": item.required_tools,
                "required_materials": item.required_materials,
            })

        plan = dpl.create_plan(
            object_id=body.object_id,
            stage_key=body.stage_key,
            date_str=body.date,
            assigned_worker_ids=body.assigned_worker_ids,
            items=items,
            created_by=str(user['id']),
        )

        if body.publish:
            plan = dpl.publish_plan(plan["id"], str(user['id']))

        return plan

    @router.get("/api/daily-plan/{plan_id}")
    def daily_plan_get(
        plan_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Полный план с версиями, принятием, amendments."""
        plan = dpl.get_plan(plan_id)
        if not plan:
            raise HTTPException(404, "План не найден")

        worker_id = str(user['id'])
        if role != 'owner' and worker_id not in [str(w) for w in plan["assigned_worker_ids"]]:
            raise HTTPException(403, "Нет доступа к этому плану")

        acceptance = dpl.get_acceptance(plan_id, worker_id) if role != 'owner' else None
        amendments = dpl.get_pending_amendments(plan_id)
        versions = dpl.get_plan_versions(plan_id)

        return {
            "plan": plan,
            "versions": versions,
            "acceptance": acceptance,
            "pending_amendments": amendments,
        }

    @router.get("/api/productivity/workers/{target_user_id}")
    def get_worker_productivity_api(
        target_user_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Производительность работника. Работник видит только свою, owner — любого."""
        if role != 'owner' and str(user['id']) != target_user_id:
            raise HTTPException(403, "Можно смотреть только свою производительность")
        data = dpl.get_worker_productivity(target_user_id)
        return data or {"aggregates": {}, "observations": []}

    @router.post("/api/productivity/workers/{target_user_id}/baseline")
    def set_worker_baseline(
        target_user_id: str,
        work_type_id: str,
        baseline: float,
        _: None = Depends(deps.require_owner),
    ):
        """Owner устанавливает manual_baseline_factor (prior) для работника × вид работ."""
        if baseline <= 0:
            raise HTTPException(400, "baseline должен быть > 0")
        agg = dpl.set_manual_baseline(target_user_id, work_type_id, baseline)
        return agg

    def _daily_plan_status_label(plan: dict, acceptance: dict | None, amendments: list) -> str:
        if not plan:
            return "NO_PLAN"
        s = plan["status"]
        if s == "draft":
            return "DRAFT"
        if s == "published" and not acceptance:
            return "PUBLISHED"
        if amendments:
            return "AMENDED_PENDING_ACK"
        if acceptance:
            return "ACCEPTED"
        return s.upper()

    def _checkin_shift_status(session_key, active: dict, finished: dict) -> str:
        """session_key: (worker_id, object_id) tuple (item 9) — matches active/finished dicts."""
        if session_key in active:
            s = active[session_key]
            if s.get("pause_started_at"):
                return "paused"
            return "working"
        if session_key in finished:
            return "finished"
        return "not_started"

    def _execution_summary(execution: dict | None) -> dict | None:
        if not execution:
            return None
        results = execution.get("item_results", [])
        done = sum(1 for r in results if r.get("status") == "done")
        partial = sum(1 for r in results if r.get("status") == "partial")
        not_done = sum(1 for r in results if r.get("status") == "not_done")
        blocked = sum(1 for r in results if r.get("status") == "blocked")
        return {
            "done": done,
            "partial": partial,
            "not_done": not_done,
            "blocked": blocked,
            "total": len(results),
        }

    def _compute_risk_level(
        carryovers: list,
        amendments: list,
        blockers_for_plan: list,
        execution: dict | None,
        predicted_finish_date: str | None = None,
        contract_finish_date: str | None = None,
        internal_target_date: str | None = None,
    ) -> str:
        """GREEN / YELLOW / ORANGE / RED per spec risk model.

        RED:    predicted_finish_date > contract_finish_date (confirmed schedule breach)
        ORANGE: blocker or blocked execution item; OR internal_target threatened with carryovers
        YELLOW: pending amendment or carryover without a RED/ORANGE condition
        GREEN:  none of the above
        """
        has_blocker = bool(blockers_for_plan)
        blocked_in_exec = execution and any(
            r.get("status") == "blocked"
            for r in execution.get("item_results", [])
        )
        # RED: predicted delivery is past the contract deadline
        if predicted_finish_date and contract_finish_date:
            try:
                if predicted_finish_date > contract_finish_date:
                    return "red"
            except Exception:
                pass
        if has_blocker or blocked_in_exec:
            # ORANGE: also escalate when internal target is threatened with carryovers
            return "orange"
        if internal_target_date and carryovers and predicted_finish_date:
            try:
                if predicted_finish_date > internal_target_date:
                    return "orange"
            except Exception:
                pass
        if amendments:
            return "yellow"
        if carryovers:
            return "yellow"
        return "green"

    @router.get("/api/daily-plan/owner/matrix")
    def daily_plan_owner_matrix(
        date_from: str = '',
        date_to: str = '',
        object_id: str = '',
        _: None = Depends(deps.require_owner),
    ):
        """Мульти-объектная матрица плана/факта за диапазон дат.
        date_from/date_to: YYYY-MM-DD. object_id: фильтр по объекту (необязательно)."""
        today = deps.business_today_str()
        df = date_from or today
        dt = date_to or today

        store = dpl.get_store_snapshot()
        plans = list(store["daily_plans"].values())
        if object_id:
            plans = [p for p in plans if p["object_id"] == object_id]
        plans = [p for p in plans if df <= p["date"] <= dt]
        plans.sort(key=lambda p: (p["date"], p["object_id"]))

        rows = []
        for plan in plans:
            plan_id = plan["id"]
            acceptance_list = [
                a for a in store["acceptances"].values()
                if a.get("daily_plan_id") == plan_id
            ]
            executions = [
                e for e in store["executions"].values()
                if e.get("plan_id") == plan_id
            ]
            carryovers = [
                c for c in store["carryovers"].values()
                if c.get("source_plan_id") == plan_id
            ]
            # Per item 8: amendments have no object_id/status field of their own —
            # resolve via daily_plan_id, and use the pending-amendment semantics
            # already implemented in get_pending_amendments (not-acked-by-all).
            amendments = [
                a for a in dpl.get_pending_amendments(plan_id)
            ]
            rows.append({
                "plan_id": plan_id,
                "date": plan["date"],
                "object_id": plan["object_id"],
                "stage_key": plan["stage_key"],
                "status": plan["status"],
                "version": plan["version"],
                "assigned_worker_count": len(plan.get("assigned_worker_ids", [])),
                "accepted_count": len(acceptance_list),
                "item_count": len(plan["items"]),
                "execution_count": len(executions),
                "carryover_count": len(carryovers),
                "pending_amendment_count": len(amendments),
            })

        return {
            "date_from": df,
            "date_to": dt,
            "object_id": object_id or None,
            "rows": rows,
            "total": len(rows),
        }

    @router.post("/api/daily-plan/replan/{object_id}")
    def daily_plan_replan(
        object_id: str,
        body: ReplanRequestBody = ReplanRequestBody(),
        _: None = Depends(deps.require_owner),
    ):
        """Лёгкая оценка рисков объекта + рекомендации по перепланированию.
        Не изменяет данные — только читает и возвращает risk-summary."""
        store = dpl.get_store_snapshot()

        plans = [p for p in store["daily_plans"].values() if p["object_id"] == object_id]
        plans.sort(key=lambda p: p["date"])

        if not plans:
            return {"object_id": object_id, "risk_level": "green", "issues": [], "recommendations": []}

        issues = []
        open_carryovers = [
            c for c in store["carryovers"].values()
            if c.get("object_id") == object_id and c.get("status") != "applied"
        ]
        if open_carryovers:
            issues.append({
                "type": "open_carryovers",
                "count": len(open_carryovers),
                "description": f"Есть незакрытые переносы ({len(open_carryovers)} ед.)",
            })

        plans_with_blockers = []
        for plan in plans:
            plan_blockers = [
                b for b in store.get("blockers", {}).values()
                if b.get("daily_plan_id") == plan["id"]
            ]
            if plan_blockers:
                plans_with_blockers.append({"plan_id": plan["id"], "date": plan["date"],
                                             "blocker_count": len(plan_blockers)})
        if plans_with_blockers:
            issues.append({
                "type": "blockers",
                "count": len(plans_with_blockers),
                "description": f"Зафиксированы препятствия на {len(plans_with_blockers)} дн.",
                "detail": plans_with_blockers,
            })

        # Item 8: amendments have no object_id/status field — resolve via the
        # object's plans -> daily_plan_id chain instead, using the existing
        # not-acked-by-all semantics in get_pending_amendments().
        pending_amendments = [
            a for plan in plans for a in dpl.get_pending_amendments(plan["id"])
        ]
        if pending_amendments:
            issues.append({
                "type": "pending_amendments",
                "count": len(pending_amendments),
                "description": f"Неподтверждённые изменения плана ({len(pending_amendments)})",
            })

        # Item 10: use the shared 4-tier risk function instead of an independent
        # inline 3-tier calc (which had no RED tier and could drift from owner/today's
        # logic). RED stays unreachable here until a caller supplies
        # predicted_finish_date/contract_finish_date -- no contract-date source exists
        # yet in this codebase; adding one is out of scope for a Round 1 bug fix.
        risk_level = _compute_risk_level(
            carryovers=open_carryovers,
            amendments=pending_amendments,
            blockers_for_plan=plans_with_blockers,
            execution=None,
        )

        recommendations = []
        if open_carryovers:
            recommendations.append("Пересмотрите план на следующие рабочие дни с учётом открытых переносов.")
        if plans_with_blockers:
            recommendations.append("Устраните препятствия (материалы/доступ) до следующей смены.")
        if pending_amendments:
            recommendations.append("Убедитесь, что работники подтвердили внесённые изменения.")

        return {
            "object_id": object_id,
            "risk_level": risk_level,
            "issues": issues,
            "recommendations": recommendations,
            "plan_count": len(plans),
            "open_carryover_count": len(open_carryovers),
        }

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
