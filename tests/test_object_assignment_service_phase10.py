from copy import deepcopy
from pathlib import Path

import pytest

from services.object_assignments import (
    ObjectAssignmentDeps,
    ObjectAssignmentError,
    ObjectAssignmentService,
)


def _service(*, assignments=None, object_rows=None, work_types=None, events=None):
    store = deepcopy(assignments or {})
    events = events if events is not None else []
    rows = object_rows if object_rows is not None else [
        ["ID объекта", "Статус"],
        ["OBJ-1", "Активен"],
    ]
    work_types = work_types or {"wt-1": {"id": "wt-1", "name": "Trockenbau", "active": True}}

    def cached_get_used_range(tab_name):
        assert tab_name == "Объекты"
        events.append("objects-read")
        return rows

    def load_abwesenheit():
        events.append("absence-read")
        return []

    def update_json_transaction(_path, _default, mutator):
        events.append("txn-start")
        result = mutator(store)
        events.append("txn-end")
        return result

    deps = ObjectAssignmentDeps(
        cached_get_used_range=cached_get_used_range,
        load_assignments=lambda: store,
        load_worker_profiles=lambda: {},
        load_abwesenheit=load_abwesenheit,
        load_roles=lambda: {"42": "worker"},
        load_checkin_meta=lambda: {},
        update_json_transaction=update_json_transaction,
        object_assignments_file=lambda: "/tmp/assignments.json",
        validate_date_str=lambda value, field: value,
        dates_overlap=lambda a_from, a_to, b_from, b_to: bool(
            a_from and a_to and b_from and b_to and a_from <= b_to and b_from <= a_to
        ),
        assignment_periods_overlap=lambda a, b: (
            True
            if not (a.get("date_from") and a.get("date_to") and b.get("date_from") and b.get("date_to"))
            else a["date_from"] <= b["date_to"] and b["date_from"] <= a["date_to"]
        ),
        assignment_status=lambda item: item.get("status") or "accepted",
        utcnow_iso=lambda: "2026-10-13T18:00:00",
        sanitize_display_name=lambda value, fallback: str(value or fallback or "").strip(),
        object_history_worker_name=lambda uid: f"Worker {uid}",
        append_object_history_best_effort=lambda *args, **kwargs: None,
        shadow_compare_assignments=None,
        get_work_type=lambda work_type_id: work_types.get(work_type_id),
        skill_display_name=lambda work_type_id: work_types.get(work_type_id, {}).get("name", work_type_id),
        build_candidates=lambda *args, **kwargs: [],
        new_id=lambda: "new-assignment-id",
    )
    return ObjectAssignmentService(deps), store, events


def test_legacy_response_without_assignment_id_rejects_ambiguous_pending_jobs():
    service, store, _ = _service(assignments={
        "OBJ-1": [
            {"id": "a1", "user_id": "42", "status": "pending"},
            {"id": "a2", "user_id": "42", "status": "pending"},
        ]
    })

    with pytest.raises(ObjectAssignmentError) as exc_info:
        service.respond_to_assignment(
            "OBJ-1",
            "42",
            assignment_id="",
            accept=True,
            decline_reason="",
        )

    assert exc_info.value.status_code == 409
    assert [item["status"] for item in store["OBJ-1"]] == ["pending", "pending"]


def test_legacy_response_without_assignment_id_stays_compatible_when_unambiguous():
    service, store, _ = _service(assignments={
        "OBJ-1": [{"id": "a1", "user_id": "42", "status": "pending"}]
    })

    result = service.respond_to_assignment(
        "OBJ-1",
        "42",
        assignment_id="",
        accept=True,
        decline_reason="",
    )

    assert result["id"] == "a1"
    assert store["OBJ-1"][0]["status"] == "accepted"


def test_single_assign_does_not_create_phantom_object_bucket():
    service, store, events = _service(object_rows=[["ID объекта", "Статус"]])

    with pytest.raises(ObjectAssignmentError) as exc_info:
        service.assign_user(
            "MISSING",
            {
                "user_id": "42",
                "stage_id": "stage-1",
                "work_type_id": "",
                "date_from": "2026-10-14",
                "date_to": "2026-10-15",
            },
            {"id": "owner"},
        )

    assert exc_info.value.status_code == 404
    assert store == {}
    assert "txn-start" not in events


def test_single_assign_rejects_completed_object_before_mutation():
    service, store, events = _service(object_rows=[
        ["ID объекта", "Статус"],
        ["OBJ-1", "Завершён"],
    ])

    with pytest.raises(ObjectAssignmentError) as exc_info:
        service.assign_user(
            "OBJ-1",
            {
                "user_id": "42",
                "stage_id": "stage-1",
                "work_type_id": "",
                "date_from": "2026-10-14",
                "date_to": "2026-10-15",
            },
            {"id": "owner"},
        )

    assert exc_info.value.status_code == 400
    assert store == {}
    assert "txn-start" not in events


def test_single_assign_rejects_unknown_or_inactive_work_type():
    service, store, events = _service(work_types={})

    with pytest.raises(ObjectAssignmentError) as exc_info:
        service.assign_user(
            "OBJ-1",
            {
                "user_id": "42",
                "stage_id": "stage-1",
                "work_type_id": "unknown",
                "date_from": "2026-10-14",
                "date_to": "2026-10-15",
            },
            {"id": "owner"},
        )

    assert exc_info.value.status_code == 400
    assert store == {}
    assert "txn-start" not in events


def test_slow_availability_read_happens_before_assignment_transaction():
    service, store, events = _service()

    result = service.assign_user(
        "OBJ-1",
        {
            "user_id": "42",
            "stage_id": "stage-1",
            "work_type_id": "wt-1",
            "date_from": "2026-10-14",
            "date_to": "2026-10-15",
            "task_note": "Montage",
        },
        {"id": "owner"},
    )

    assert result == {"status": "ok"}
    assert store["OBJ-1"][0]["id"] == "new-assignment-id"
    assert events.index("absence-read") < events.index("txn-start")


def test_object_assignment_service_stays_framework_agnostic():
    source = Path(__file__).parents[1] / "backend" / "services" / "object_assignments.py"
    text = source.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "fastapi" not in lowered
    assert "pydantic" not in lowered
    assert "from routes.objects" not in lowered
    assert "from ..routes" not in lowered
    assert "import main" not in lowered
