"""Strict public-contract and behavior guard for the final clean Phase 6 Needs extraction."""
import ast
import inspect
import os
import sys
import tempfile
import time

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
import routes.needs as needs_routes  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


def _route(method: str, path: str):
    matches = [
        route for route in iter_app_routes(backend.app)
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
    ]
    assert len(matches) == 1, (method, path, len(matches))
    return matches[0]


def test_needs_routes_keep_exact_registration_contract():
    dependency_map = {
        backend.get_current_user: "user",
        backend.get_role: "role",
        backend.require_owner: "owner",
    }
    cases = [
        ("GET", "/api/tasks", "list_tasks", "list_tasks_api_tasks_get", [("user", "user"), ("role", "role")]),
        ("POST", "/api/tasks", "create_task", "create_task_api_tasks_post", [("user", "user"), ("role", "role")]),
        ("PATCH", "/api/tasks/{task_id}", "update_task_status", "update_task_status_api_tasks__task_id__patch", [("user", "user"), ("_", "owner")]),
    ]
    for method, path, name, unique_id, expected_deps in cases:
        route = _route(method, path)
        assert route.name == name
        assert route.endpoint.__name__ == name
        assert route.endpoint.__module__ in {"routes.needs", "miniapp.routes.needs"}
        assert route.endpoint is getattr(backend, name)
        assert route.methods == {method}
        assert route.unique_id == unique_id
        assert route.status_code is None
        assert route.response_model is None
        assert route.responses == {}
        actual_deps = [(dep.name, dependency_map[dep.call]) for dep in route.dependant.dependencies]
        assert actual_deps == expected_deps


def test_needs_openapi_and_models_are_stable():
    spec = backend.app.openapi()
    get_op = spec["paths"]["/api/tasks"]["get"]
    post_op = spec["paths"]["/api/tasks"]["post"]
    patch_op = spec["paths"]["/api/tasks/{task_id}"]["patch"]
    assert get_op["operationId"] == "list_tasks_api_tasks_get"
    assert post_op["operationId"] == "create_task_api_tasks_post"
    assert patch_op["operationId"] == "update_task_status_api_tasks__task_id__patch"
    assert post_op["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/TaskCreateBody"}
    assert patch_op["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/TaskStatusBody"}

    fields = backend.TaskCreateBody.model_fields
    assert list(fields) == ["title", "description", "object_id", "priority", "category", "due_at"]
    assert fields["title"].is_required() and fields["title"].annotation is str
    assert fields["description"].default == ""
    assert fields["object_id"].default == ""
    assert fields["priority"].default == "обычная"
    assert fields["category"].default == "other"
    assert fields["due_at"].default is None
    assert list(backend.TaskStatusBody.model_fields) == ["status"]
    assert backend.TaskStatusBody.model_fields["status"].is_required()


def test_needs_module_has_no_back_edge_and_main_has_no_direct_needs_routes():
    tree = ast.parse(inspect.getsource(needs_routes))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(alias.name for alias in node.names if alias.name == "main" or alias.name.endswith(".main"))
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if name == "main" or name.endswith(".main"):
                offenders.append(name)
    assert offenders == []

    main_tree = ast.parse(inspect.getsource(backend))
    forbidden = {"/api/tasks", "/api/tasks/{task_id}"}
    for node in ast.walk(main_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and decorator.args):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "app"):
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                assert first.value not in forbidden, (node.name, first.value)


def _handlers(tmpdir, *, access=True, role_map=None, profile=None, sheets=None):
    tasks_file = os.path.join(tmpdir, "tasks.json")
    messages = []

    def safe_load(path, default):
        if not os.path.exists(path):
            return default
        import json
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def atomic_write(path, value):
        import json
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)

    def transaction(path, default, mutator):
        value = safe_load(path, default)
        result = mutator(value)
        atomic_write(path, value)
        return result

    class FakeSheets:
        def __init__(self):
            self.rows = []
        def append_row_safe(self, tab, row):
            self.rows.append((tab, row))

    sheet_obj = sheets or FakeSheets()
    _, handlers = needs_routes.create_needs_router(needs_routes.NeedsRouteDeps(
        get_current_user=lambda: {"id": 10},
        get_role=lambda: "worker",
        require_owner=lambda: None,
        tasks_file=lambda: tasks_file,
        safe_load_json=safe_load,
        atomic_write_json=atomic_write,
        update_json_transaction=transaction,
        task_priorities={"обычная", "срочно"},
        task_categories={"other", "material"},
        task_statuses={"открыто", "в работе", "закрыто"},
        has_active_object_access=lambda uid, object_id: access,
        load_roles=lambda: role_map or {"1": "owner", "10": "worker"},
        get_worker_profile=lambda uid: profile or {"name": "Ivan"},
        sanitize_display_name=lambda raw, fallback: raw or fallback,
        send_telegram_message=lambda *args: messages.append(args),
        business_now=lambda: __import__("datetime").datetime.now().astimezone(),
        load_repo_objekte_lib=lambda: sheet_obj,
    ))
    return handlers, tasks_file, messages, sheet_obj


def test_needs_behavior_create_list_close_reopen_and_archive_once():
    with tempfile.TemporaryDirectory() as tmp:
        handlers, _, messages, sheets = _handlers(tmp)
        worker = {"id": 10, "first_name": "Ivan"}
        body = needs_routes.TaskCreateBody(
            title="  Нужен цемент  ", object_id="OBJ-1", priority="срочно", category="material"
        )
        created = handlers.create_task(body, user=worker, role="worker")
        assert created["title"] == "Нужен цемент"
        assert created["from_name"] == "Ivan"
        assert created["status"] == "открыто"
        assert len(messages) == 1

        assert [x["id"] for x in handlers.list_tasks(object_id="", user=worker, role="worker")["tasks"]] == [created["id"]]
        assert [x["id"] for x in handlers.list_tasks(object_id="OBJ-1", user=worker, role="worker")["tasks"]] == [created["id"]]

        closed = handlers.update_task_status(
            created["id"], needs_routes.TaskStatusBody(status="закрыто"), user={"id": 1}, _=None
        )
        assert closed["closed_at"] is not None
        assert len(sheets.rows) == 1
        handlers.update_task_status(
            created["id"], needs_routes.TaskStatusBody(status="закрыто"), user={"id": 1}, _=None
        )
        assert len(sheets.rows) == 1
        reopened = handlers.update_task_status(
            created["id"], needs_routes.TaskStatusBody(status="открыто"), user={"id": 1}, _=None
        )
        assert reopened["closed_at"] is None


def test_needs_validation_access_and_overdue_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        handlers, _, _, _ = _handlers(tmp, access=False)
        worker = {"id": 10}
        with pytest.raises(HTTPException) as exc:
            handlers.list_tasks(object_id="OBJ-1", user=worker, role="worker")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            handlers.create_task(needs_routes.TaskCreateBody(title="x", object_id="OBJ-1"), user=worker, role="worker")
        assert exc.value.status_code == 403

    with tempfile.TemporaryDirectory() as tmp:
        handlers, tasks_file, _, _ = _handlers(tmp)
        now = int(time.time())
        handlers.save_tasks([
            {"id": "late", "status": "открыто", "title": "Цемент", "object_id": "OBJ-1", "from_name": "Ivan", "due_at": now - 5},
            {"id": "done", "status": "закрыто", "title": "Done", "due_at": now - 5},
        ])
        alerts = handlers.overdue_task_alerts(now=now)
        assert len(alerts) == 1
        assert alerts[0]["id"] == "task-overdue-late"
        assert alerts[0]["type"] == "red"


def test_final_create_task_symbol_is_needs_handler_not_object_task_handler():
    needs_route = _route("POST", "/api/tasks")
    object_route = _route("POST", "/api/objects/{object_id}/tasks")
    assert backend.create_task is needs_route.endpoint
    assert backend.create_task is not object_route.endpoint
