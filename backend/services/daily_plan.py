"""Application service for DailyPlan and productivity workflows.

Framework-agnostic by design: no FastAPI/Pydantic imports and no back-edge to
main.py or routes. HTTP authentication/DTO handling stays in routes.daily_plan;
the DailyPlan store plus runtime collaborators are injected by the router factory.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

PLAN_BLOCKER_REASON_LABELS = {
    "material_missing": "нет материалов",
    "tool_missing": "нет инструмента",
    "access_denied": "нет доступа на объект",
    "safety_concern": "проблема безопасности",
    "weather": "погодные условия",
    "coordinator_missing": "нет ответственного лица",
    "other": "другое",
}


class DailyPlanServiceError(RuntimeError):
    """Expected application failure that the HTTP adapter maps to a status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class DailyPlanServiceDeps:
    daily_plan_lib: Any
    business_today_str: Callable[[], str]
    load_roles: Callable[[], dict]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[..., str]
    create_critical_alert: Callable[..., Any]
    load_checkin_meta: Callable[[], list]
    is_active_photo_checkin_session: Callable[[dict], bool]


@dataclass(frozen=True)
class DailyPlanService:
    deps: DailyPlanServiceDeps

    @property
    def dpl(self):
        return self.deps.daily_plan_lib

    @staticmethod
    def daily_plan_status_label(plan: dict, acceptance: dict | None, amendments: list) -> str:
        if not plan:
            return "NO_PLAN"
        status = plan["status"]
        if status == "draft":
            return "DRAFT"
        if status == "published" and not acceptance:
            return "PUBLISHED"
        if amendments:
            return "AMENDED_PENDING_ACK"
        if acceptance:
            return "ACCEPTED"
        return status.upper()

    @staticmethod
    def checkin_shift_status(session_key, active: dict, finished: dict) -> str:
        if session_key in active:
            session = active[session_key]
            if session.get("pause_started_at"):
                return "paused"
            return "working"
        if session_key in finished:
            return "finished"
        return "not_started"

    @staticmethod
    def execution_summary(execution: dict | None) -> dict | None:
        if not execution:
            return None
        results = execution.get("item_results", [])
        return {
            "done": sum(1 for row in results if row.get("status") == "done"),
            "partial": sum(1 for row in results if row.get("status") == "partial"),
            "not_done": sum(1 for row in results if row.get("status") == "not_done"),
            "blocked": sum(1 for row in results if row.get("status") == "blocked"),
            "total": len(results),
        }

    @staticmethod
    def compute_risk_level(
        carryovers: list,
        amendments: list,
        blockers_for_plan: list,
        execution: dict | None,
        predicted_finish_date: str | None = None,
        contract_finish_date: str | None = None,
        internal_target_date: str | None = None,
    ) -> str:
        """Return GREEN/YELLOW/ORANGE/RED according to the existing risk model."""
        if predicted_finish_date and contract_finish_date:
            if predicted_finish_date > contract_finish_date:
                return "red"

        blocked_in_execution = bool(
            execution
            and any(
                row.get("status") == "blocked"
                for row in execution.get("item_results", [])
            )
        )
        if blockers_for_plan or blocked_in_execution:
            return "orange"

        if internal_target_date and carryovers and predicted_finish_date:
            if predicted_finish_date > internal_target_date:
                return "orange"
        if amendments or carryovers:
            return "yellow"
        return "green"

    def build_worker_plan_response(self, worker_id: str, date_str: str) -> dict:
        plan = self.dpl.get_today_plan_for_worker(worker_id, date_str)
        if not plan:
            return {"has_plan": False, "date": date_str}

        carryovers = self.dpl.get_carryovers_for_worker(worker_id, date_str)
        acceptance = self.dpl.get_acceptance(plan["id"], worker_id)
        amendments = self.dpl.get_pending_amendments(plan["id"], worker_id)
        accepted_items = (
            self.dpl.get_accepted_snapshot(plan["id"], worker_id)
            if acceptance
            else plan["items"]
        )
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

    def accept_plan(self, plan_id: str, worker_id: str) -> dict:
        plan = self.dpl.get_plan(plan_id)
        if not plan:
            raise DailyPlanServiceError(404, "План не найден")
        try:
            acceptance = self.dpl.accept_plan(plan_id, plan["version"], worker_id)
        except PermissionError as exc:
            raise DailyPlanServiceError(403, "Вы не назначены на этот план") from exc
        except self.dpl.StaleAcceptanceError as exc:
            raise DailyPlanServiceError(409, str(exc)) from exc
        except ValueError as exc:
            raise DailyPlanServiceError(400, str(exc)) from exc
        return {"status": "accepted", "acceptance": acceptance}

    def accept_amendment(self, plan_id: str, amendment_id: str, worker_id: str) -> dict:
        try:
            amendment = self.dpl.acknowledge_amendment(plan_id, amendment_id, worker_id)
        except KeyError as exc:
            raise DailyPlanServiceError(404, "Amendment не найден") from exc
        except PermissionError as exc:
            raise DailyPlanServiceError(403, "Вы не назначены на этот план") from exc
        return {"status": "acknowledged", "amendment": amendment}

    def report_blocker(
        self,
        plan_id: str,
        worker_id: str,
        reason_code: str,
        comment: str,
    ) -> dict:
        plan = self.dpl.get_plan(plan_id)
        if not plan:
            raise DailyPlanServiceError(404, "План не найден")
        if worker_id not in [str(value) for value in plan.get("assigned_worker_ids", [])]:
            raise DailyPlanServiceError(403, "Вы не назначены на этот план")
        if reason_code not in PLAN_BLOCKER_REASON_LABELS:
            raise DailyPlanServiceError(
                400,
                "Недопустимый reason_code. "
                f"Допустимые: {list(PLAN_BLOCKER_REASON_LABELS)}",
            )

        blocker = self.dpl.record_blocker(
            plan_id,
            worker_id,
            reason_code,
            comment.strip()[:500],
        )

        roles = self.deps.load_roles()
        owner_id = next((uid for uid, role in roles.items() if role == "owner"), None)
        if owner_id:
            profiles = self.deps.load_worker_profiles()
            worker_name = self.deps.sanitize_display_name(
                profiles.get(worker_id, {}).get("name"),
                worker_id,
            )
            reason_label = PLAN_BLOCKER_REASON_LABELS.get(reason_code, reason_code)
            try:
                self.deps.create_critical_alert(
                    target_user_id=owner_id,
                    kind="plan_blocker",
                    title=f"{worker_name} не может начать — {reason_label}",
                    subtitle=comment[:100] if comment else "",
                    ref_id=plan_id,
                )
            except Exception:
                # The blocker is the durable primary operation. Notification is a
                # secondary side effect and must never turn an already-recorded
                # blocker into a false HTTP 500/retry loop.
                logger.exception(
                    "Critical alert failed after DailyPlan blocker was recorded",
                    extra={"plan_id": plan_id, "worker_id": worker_id},
                )

        return {"status": "recorded", "blocker": blocker}

    def owner_today(self) -> dict:
        today = self.deps.business_today_str()
        plans = self.dpl.get_pending_plans_for_owner_today(today)
        checkin_items = self.deps.load_checkin_meta()
        active_sessions = {
            (str(session["user_id"]), str(session.get("object_id", ""))): session
            for session in checkin_items
            if session.get("date") == today
            and self.deps.is_active_photo_checkin_session(session)
        }
        finished_sessions = {
            (str(session["user_id"]), str(session.get("object_id", ""))): session
            for session in checkin_items
            if session.get("date") == today and session.get("finish_at") is not None
        }

        rows = []
        for plan in plans:
            for worker_id in plan.get("assigned_worker_ids", []):
                wid = str(worker_id)
                session_key = (wid, str(plan.get("object_id", "")))
                acceptance = self.dpl.get_acceptance(plan["id"], wid)
                amendments = self.dpl.get_pending_amendments(plan["id"], wid)
                session = finished_sessions.get(session_key) or active_sessions.get(session_key)
                execution = None
                if session and session.get("finish_at"):
                    execution = self.dpl.get_execution(session["id"])
                carryovers = self.dpl.get_carryovers_for_worker(wid, today)
                blockers = self.dpl.get_blockers_for_plan(plan["id"])
                rows.append(
                    {
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
                        "plan_status_label": self.daily_plan_status_label(
                            plan, acceptance, amendments
                        ),
                        "acceptance": (
                            {
                                "accepted_at": acceptance["accepted_at"],
                                "plan_version": acceptance["plan_version"],
                            }
                            if acceptance
                            else None
                        ),
                        "shift_status": self.checkin_shift_status(
                            session_key,
                            active_sessions,
                            finished_sessions,
                        ),
                        "pending_amendments_count": len(amendments),
                        "carryover_count": len(carryovers),
                        "execution_summary": self.execution_summary(execution),
                        "risk_level": self.compute_risk_level(
                            carryovers, amendments, blockers, execution
                        ),
                    }
                )

        return {
            "date": today,
            "summary": {
                "total_plans": len(rows),
                "accepted": sum(1 for row in rows if row["acceptance"] is not None),
                "working": sum(1 for row in rows if row["shift_status"] == "working"),
                "finished": sum(1 for row in rows if row["shift_status"] == "finished"),
                "has_carryovers": sum(1 for row in rows if row["carryover_count"] > 0),
            },
            "rows": rows,
        }

    def object_plans(self, object_id: str, date_from: str, date_to: str) -> dict:
        today = self.deps.business_today_str()
        start = date_from or today
        end = date_to or today
        store = self.dpl.get_store_snapshot()
        plans = [
            plan
            for plan in store["daily_plans"].values()
            if plan["object_id"] == object_id and start <= plan["date"] <= end
        ]
        plans.sort(key=lambda plan: plan["date"])
        result = [
            {
                "id": plan["id"],
                "date": plan["date"],
                "stage_key": plan["stage_key"],
                "status": plan["status"],
                "version": plan["version"],
                "item_count": len(plan["items"]),
                "assigned_worker_ids": plan["assigned_worker_ids"],
            }
            for plan in plans
        ]
        return {
            "object_id": object_id,
            "date_from": start,
            "date_to": end,
            "plans": result,
        }

    def create_plan(
        self,
        *,
        object_id: str,
        stage_key: str,
        date_str: str,
        assigned_worker_ids: list,
        items: list[dict],
        publish: bool,
        created_by: str,
    ) -> dict:
        normalized_items = []
        for index, item in enumerate(items):
            normalized_items.append(
                {
                    "id": item.get("id") or uuid.uuid4().hex,
                    "sequence": item.get("sequence") or index + 1,
                    "title": item["title"],
                    "objective": item.get("objective", ""),
                    "planned_quantity": item.get("planned_quantity"),
                    "unit": item.get("unit", ""),
                    "time_estimate_hours": item.get("time_estimate_hours"),
                    "work_type_id": item.get("work_type_id"),
                    "required_tools": item.get("required_tools", []),
                    "required_materials": item.get("required_materials", []),
                }
            )
        plan = self.dpl.create_plan(
            object_id=object_id,
            stage_key=stage_key,
            date_str=date_str,
            assigned_worker_ids=assigned_worker_ids,
            items=normalized_items,
            created_by=created_by,
        )
        if publish:
            plan = self.dpl.publish_plan(plan["id"], created_by)
        return plan

    def plan_detail(self, plan_id: str, worker_id: str, role: str) -> dict:
        plan = self.dpl.get_plan(plan_id)
        if not plan:
            raise DailyPlanServiceError(404, "План не найден")
        if role != "owner" and worker_id not in [
            str(value) for value in plan["assigned_worker_ids"]
        ]:
            raise DailyPlanServiceError(403, "Нет доступа к этому плану")

        acceptance = self.dpl.get_acceptance(plan_id, worker_id) if role != "owner" else None
        # Worker-facing detail must use the same per-worker acknowledgement
        # semantics as /today. The old route omitted worker_id here and therefore
        # returned amendments still pending for *other* workers after this worker
        # had already acknowledged them.
        amendments = (
            self.dpl.get_pending_amendments(plan_id, worker_id)
            if role != "owner"
            else self.dpl.get_pending_amendments(plan_id)
        )
        return {
            "plan": plan,
            "versions": self.dpl.get_plan_versions(plan_id),
            "acceptance": acceptance,
            "pending_amendments": amendments,
        }

    def worker_productivity(self, target_user_id: str) -> dict:
        data = self.dpl.get_worker_productivity(target_user_id)
        return data or {"aggregates": {}, "observations": []}

    def set_worker_baseline(self, target_user_id: str, work_type_id: str, baseline: float):
        if baseline <= 0:
            raise DailyPlanServiceError(400, "baseline должен быть > 0")
        return self.dpl.set_manual_baseline(target_user_id, work_type_id, baseline)

    def owner_matrix(self, date_from: str, date_to: str, object_id: str) -> dict:
        today = self.deps.business_today_str()
        start = date_from or today
        end = date_to or today
        store = self.dpl.get_store_snapshot()
        plans = list(store["daily_plans"].values())
        if object_id:
            plans = [plan for plan in plans if plan["object_id"] == object_id]
        plans = [plan for plan in plans if start <= plan["date"] <= end]
        plans.sort(key=lambda plan: (plan["date"], plan["object_id"]))

        rows = []
        for plan in plans:
            plan_id = plan["id"]
            acceptances = [
                value
                for value in store["acceptances"].values()
                if value.get("daily_plan_id") == plan_id
            ]
            executions = [
                value
                for value in store["executions"].values()
                if value.get("plan_id") == plan_id
            ]
            carryovers = [
                value
                for value in store["carryovers"].values()
                if value.get("source_plan_id") == plan_id
            ]
            amendments = self.dpl.get_pending_amendments(plan_id)
            rows.append(
                {
                    "plan_id": plan_id,
                    "date": plan["date"],
                    "object_id": plan["object_id"],
                    "stage_key": plan["stage_key"],
                    "status": plan["status"],
                    "version": plan["version"],
                    "assigned_worker_count": len(plan.get("assigned_worker_ids", [])),
                    "accepted_count": len(acceptances),
                    "item_count": len(plan["items"]),
                    "execution_count": len(executions),
                    "carryover_count": len(carryovers),
                    "pending_amendment_count": len(amendments),
                }
            )
        return {
            "date_from": start,
            "date_to": end,
            "object_id": object_id or None,
            "rows": rows,
            "total": len(rows),
        }

    def replan(self, object_id: str) -> dict:
        store = self.dpl.get_store_snapshot()
        plans = [
            plan
            for plan in store["daily_plans"].values()
            if plan["object_id"] == object_id
        ]
        plans.sort(key=lambda plan: plan["date"])
        if not plans:
            return {
                "object_id": object_id,
                "risk_level": "green",
                "issues": [],
                "recommendations": [],
            }

        issues = []
        open_carryovers = [
            value
            for value in store["carryovers"].values()
            if value.get("object_id") == object_id and value.get("status") != "applied"
        ]
        if open_carryovers:
            issues.append(
                {
                    "type": "open_carryovers",
                    "count": len(open_carryovers),
                    "description": f"Есть незакрытые переносы ({len(open_carryovers)} ед.)",
                }
            )

        plans_with_blockers = []
        for plan in plans:
            blockers = [
                value
                for value in store.get("blockers", {}).values()
                if value.get("daily_plan_id") == plan["id"]
            ]
            if blockers:
                plans_with_blockers.append(
                    {
                        "plan_id": plan["id"],
                        "date": plan["date"],
                        "blocker_count": len(blockers),
                    }
                )
        if plans_with_blockers:
            issues.append(
                {
                    "type": "blockers",
                    "count": len(plans_with_blockers),
                    "description": (
                        "Зафиксированы препятствия на "
                        f"{len(plans_with_blockers)} дн."
                    ),
                    "detail": plans_with_blockers,
                }
            )

        pending_amendments = [
            amendment
            for plan in plans
            for amendment in self.dpl.get_pending_amendments(plan["id"])
        ]
        if pending_amendments:
            issues.append(
                {
                    "type": "pending_amendments",
                    "count": len(pending_amendments),
                    "description": (
                        "Неподтверждённые изменения плана "
                        f"({len(pending_amendments)})"
                    ),
                }
            )

        recommendations = []
        if open_carryovers:
            recommendations.append(
                "Пересмотрите план на следующие рабочие дни с учётом открытых переносов."
            )
        if plans_with_blockers:
            recommendations.append(
                "Устраните препятствия (материалы/доступ) до следующей смены."
            )
        if pending_amendments:
            recommendations.append(
                "Убедитесь, что работники подтвердили внесённые изменения."
            )

        return {
            "object_id": object_id,
            "risk_level": self.compute_risk_level(
                carryovers=open_carryovers,
                amendments=pending_amendments,
                blockers_for_plan=plans_with_blockers,
                execution=None,
            ),
            "issues": issues,
            "recommendations": recommendations,
            "plan_count": len(plans),
            "open_carryover_count": len(open_carryovers),
        }
