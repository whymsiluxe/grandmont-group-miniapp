from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from services.stages import StageService, StageServiceDeps, StageServiceError


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeObjects:
    VALID_STAGE_STATUS = ("Не начат", "В работе", "Готово")

    def __init__(self, stages=None, *, status_error=None):
        self.stages = list(stages or [])
        self.status_error = status_error
        self.description_updates = []
        self.status_updates = []
        self.syncs = []

    def all_stages(self, object_id):
        return [deepcopy(stage) for stage in self.stages if stage.get("object_id") == object_id]

    def update_stage_description(self, row_num, description):
        self.description_updates.append((row_num, description))

    def update_stage_status(self, row_num, status, today):
        self.status_updates.append((row_num, status, today))
        if self.status_error:
            raise ValueError(self.status_error)

    def sync_current_stage(self, object_id):
        self.syncs.append(object_id)

    def delete_stage(self, object_id, row_num):
        if self.status_error:
            raise ValueError(self.status_error)


class FakeRoadmap:
    ROADMAP_FILE = "roadmap.json"
    STAGE_REQUESTS_FILE = "stage-requests.json"
    REQUEST_KINDS = ("delete_stage", "change_status")

    @staticmethod
    def _default_store():
        return {"categories": {}, "items": {}, "notes": {}, "stage_blocks": {}}

    @staticmethod
    def _default_requests():
        return []

    @staticmethod
    def find_stage_request(requests, request_id):
        return next((item for item in requests if item["id"] == request_id), None)

    @classmethod
    def decide_stage_request(cls, requests, request_id, approve, decided_by):
        request = cls.find_stage_request(requests, request_id)
        if not request or request["status"] != "pending":
            return None
        request["status"] = "approved" if approve else "rejected"
        request["decided_at"] = 123
        request["decided_by"] = str(decided_by)
        return request


def build_service(objects, roadmap, requests=None):
    stores = {
        roadmap.ROADMAP_FILE: roadmap._default_store(),
        roadmap.STAGE_REQUESTS_FILE: deepcopy(requests or []),
    }

    def safe_load_json(path, default):
        return deepcopy(stores.get(path, default))

    def update_json_transaction(path, default_factory, mutate):
        store = deepcopy(stores.get(path, default_factory()))
        result = mutate(store)
        stores[path] = store
        return deepcopy(result)

    deps = StageServiceDeps(
        cached_get_used_range=lambda _tab: [],
        load_repo_objekte_lib=lambda: objects,
        roadmap_lib=lambda: roadmap,
        safe_load_json=safe_load_json,
        update_json_transaction=update_json_transaction,
        business_today_str=lambda: "2026-10-13",
        append_object_history_best_effort=lambda *args, **kwargs: None,
        get_worker_profile=lambda _uid: {"name": "Worker"},
        sanitize_display_name=lambda raw, fallback: raw or fallback,
        load_roles=lambda: {"1": "owner"},
        create_critical_alert=lambda **kwargs: {"id": "alert-1"},
        send_telegram_message=lambda *args, **kwargs: None,
    )
    return StageService(deps), stores


def test_service_layer_has_no_fastapi_or_main_back_edge():
    path = REPO_ROOT / "backend" / "services" / "stages.py"
    tree = ast.parse(path.read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)
    assert not any(name.endswith("main") or ".routes" in name or name == "routes" for name in imports)


def test_description_update_rejects_object_row_mismatch_before_mutation():
    objects = FakeObjects([
        {"object_id": "OBJ-B", "_row": 7, "ID строки этапа": "OBJ-B-S1"},
    ])
    service, _ = build_service(objects, FakeRoadmap())

    with pytest.raises(StageServiceError) as exc_info:
        service.update_stage_description("OBJ-A", 7, "must not leak")

    assert exc_info.value.status_code == 404
    assert objects.description_updates == []


def test_approved_request_rolls_back_to_pending_if_sheet_mutation_fails():
    objects = FakeObjects(status_error="Sheets write failed")
    request = {
        "id": "req-1",
        "object_id": "OBJ-1",
        "stage_key": "OBJ-1-S1",
        "stage_row": 12,
        "kind": "change_status",
        "payload": {"new_status": "Готово"},
        "requested_by": "77",
        "status": "pending",
        "decided_at": None,
        "decided_by": None,
    }
    service, stores = build_service(objects, FakeRoadmap(), [request])

    with pytest.raises(StageServiceError) as exc_info:
        service.decide_stage_request("OBJ-1", "req-1", True, {"id": "1"})

    assert exc_info.value.status_code == 400
    stored = stores[FakeRoadmap.STAGE_REQUESTS_FILE][0]
    assert stored["status"] == "pending"
    assert stored["decided_at"] is None
    assert stored["decided_by"] is None
    assert objects.status_updates == [(12, "Готово", "2026-10-13")]
