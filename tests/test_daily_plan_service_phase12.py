from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from backend.services.daily_plan import DailyPlanService, DailyPlanServiceDeps


class FakeDpl:
    class StaleAcceptanceError(RuntimeError):
        pass

    def __init__(self):
        self.pending_calls = []
        self.blockers = []
        self.plan = {
            "id": "plan-1",
            "object_id": "OBJ-1",
            "stage_key": "OBJ-1-S1",
            "date": "2026-10-13",
            "status": "amendment_pending",
            "version": 2,
            "items": [],
            "assigned_worker_ids": ["42", "43"],
        }

    def get_plan(self, plan_id):
        return self.plan if plan_id == self.plan["id"] else None

    def get_acceptance(self, plan_id, worker_id):
        return {"worker_id": worker_id, "plan_version": 1}

    def get_pending_amendments(self, plan_id, *args):
        self.pending_calls.append((plan_id, args))
        if args == ("42",):
            return []
        return [{"id": "amendment-for-other-worker"}]

    def get_plan_versions(self, plan_id):
        return [{"version": 1}, {"version": 2}]

    def record_blocker(self, plan_id, worker_id, reason_code, comment):
        blocker = {
            "id": "blocker-1",
            "daily_plan_id": plan_id,
            "worker_id": worker_id,
            "reason_code": reason_code,
            "comment": comment,
        }
        self.blockers.append(blocker)
        return blocker


def make_service(dpl, *, create_alert=lambda **kwargs: {"id": "alert-1"}):
    return DailyPlanService(
        DailyPlanServiceDeps(
            daily_plan_lib=dpl,
            business_today_str=lambda: "2026-10-13",
            load_roles=lambda: {"1": "owner", "42": "worker"},
            load_worker_profiles=lambda: {"42": {"name": "Worker 42"}},
            sanitize_display_name=lambda value, fallback="": value or fallback,
            create_critical_alert=create_alert,
            load_checkin_meta=lambda: [],
            is_active_photo_checkin_session=lambda session: False,
        )
    )


def test_worker_plan_detail_filters_pending_amendments_by_worker():
    dpl = FakeDpl()
    service = make_service(dpl)

    result = service.plan_detail("plan-1", "42", "worker")

    assert result["pending_amendments"] == []
    assert dpl.pending_calls[-1] == ("plan-1", ("42",))


def test_owner_plan_detail_keeps_global_pending_amendment_semantics():
    dpl = FakeDpl()
    service = make_service(dpl)

    result = service.plan_detail("plan-1", "1", "owner")

    assert result["pending_amendments"] == [{"id": "amendment-for-other-worker"}]
    assert dpl.pending_calls[-1] == ("plan-1", ())


def test_blocker_stays_successful_when_secondary_alert_side_effect_fails():
    dpl = FakeDpl()

    def fail_alert(**kwargs):
        raise OSError("chat/alert storage unavailable")

    service = make_service(dpl, create_alert=fail_alert)

    result = service.report_blocker(
        "plan-1",
        "42",
        "material_missing",
        "Need boards",
    )

    assert result["status"] == "recorded"
    assert result["blocker"]["id"] == "blocker-1"
    assert len(dpl.blockers) == 1


def test_daily_plan_service_has_no_framework_or_route_back_edge():
    path = Path("backend/services/daily_plan.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_roots = {"fastapi", "pydantic"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(name.name.split(".")[0] not in forbidden_roots for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] not in forbidden_roots
            assert "routes" not in module.split(".")
            assert module not in {"main", "backend.main"}


def test_risk_model_preserves_four_tiers():
    service = make_service(FakeDpl())

    assert service.compute_risk_level([], [], [], None) == "green"
    assert service.compute_risk_level([{"id": "c1"}], [], [], None) == "yellow"
    assert service.compute_risk_level([], [], [{"id": "b1"}], None) == "orange"
    assert (
        service.compute_risk_level(
            [], [], [], None,
            predicted_finish_date="2026-10-20",
            contract_finish_date="2026-10-19",
        )
        == "red"
    )
