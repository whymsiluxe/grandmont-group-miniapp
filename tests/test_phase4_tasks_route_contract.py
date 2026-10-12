"""Strict public-contract and behavior guard for the Phase 4 Tasks extraction."""
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
import routes.tasks as tasks_routes  # noqa: E402


PROBE = r'''
import ast
import inspect
import os
import sys

sys.path.insert(0, os.environ["PHASE4_BACKEND_DIR"])
import main as backend
import routes.tasks as tasks_routes


def iter_routes(app_or_router):
    for route in app_or_router.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from iter_routes(nested)
        else:
            yield route


def parameter_contract(operation):
    out = []
    for item in operation.get("parameters", []):
        schema = item.get("schema", {})
        out.append((
            item["name"], item["in"], bool(item.get("required", False)),
            schema.get("type"), "<required>" if item.get("required") else schema.get("default"),
        ))
    return out


routes = list(iter_routes(backend.app))
openapi = backend.app.openapi()
dependency_map = {
    "user": backend.get_current_user,
    "owner": backend.require_owner,
    "object_access": backend.require_object_access,
}
AUTH_HEADERS = [
    ("authorization", "header", False, None, None),
    ("x-telegram-init-data", "header", False, None, None),
]
# The legacy object-task POST function was named create_task, but a later
# /api/tasks handler reused that same module-level name. So only GET and PATCH
# had stable main.py symbols; POST is guarded by its registered endpoint/module
# identity instead of inventing a compatibility alias that never existed.
ROUTES = [
    ("GET", "/api/objects/{object_id}/tasks", "get_tasks", True, "get_tasks_api_objects__object_id__tasks_get", [("user","user"),("_","object_access")], [("object_id","path",True,"string","<required>")], None),
    ("POST", "/api/objects/{object_id}/tasks", "create_task", False, "create_task_api_objects__object_id__tasks_post", [("user","user"),("_","owner")], [("object_id","path",True,"string","<required>")], "TaskBody"),
    ("PATCH", "/api/tasks/{task_id}/complete", "complete_task", True, "complete_task_api_tasks__task_id__complete_patch", [("user","user"),("_","owner")], [("task_id","path",True,"string","<required>")], None),
]

for method, path, handler_name, stable_main_alias, operation_id, deps, local_params, body_model in ROUTES:
    matches = [r for r in routes if getattr(r, "path", None) == path and method in getattr(r, "methods", set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    assert route.name == handler_name, (method, path, route.name)
    assert route.endpoint.__name__ == handler_name, (method, path, route.endpoint.__name__)
    assert route.endpoint.__module__ in {"routes.tasks", "miniapp.routes.tasks"}, (method, path, route.endpoint.__module__)
    if stable_main_alias:
        assert route.endpoint is getattr(backend, handler_name), (method, path, "endpoint identity")
    assert route.methods == {method}, (method, path, route.methods)
    assert route.unique_id == operation_id, (method, path, route.unique_id)
    assert route.status_code is None and route.response_model is None and route.responses == {}, (method, path)

    actual_deps = [
        (dep.name, next(key for key, call in dependency_map.items() if dep.call is call))
        for dep in route.dependant.dependencies
    ]
    assert actual_deps == deps, (method, path, actual_deps, deps)

    operation = openapi["paths"][path][method.lower()]
    assert operation["operationId"] == operation_id, (method, path, operation["operationId"])
    params = parameter_contract(operation)
    assert [p for p in params if p[1] != "header"] == local_params, (method, path, params, local_params)
    assert [p for p in params if p[1] == "header"] == AUTH_HEADERS, (method, path, params)
    body = operation.get("requestBody")
    if body_model:
        assert body is not None and body.get("required") is True, (method, path, body)
        assert body["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{body_model}"}, (method, path, body)
    else:
        assert body is None, (method, path, body)

assert list(backend.TaskBody.model_fields) == ["text"]
field = backend.TaskBody.model_fields["text"]
assert field.annotation is str and field.is_required() is True

expected = {(method, path) for method, path, *_ in ROUTES}
seen = {}
for route in routes:
    for method in getattr(route, "methods", set()):
        key = (method, getattr(route, "path", None))
        if key in expected:
            seen[key] = seen.get(key, 0) + 1
assert seen == {key: 1 for key in expected}, seen

# The extracted module may never reach back into main.py.
tree = ast.parse(inspect.getsource(tasks_routes))
offenders = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        offenders.extend(alias.name for alias in node.names if alias.name == "main" or alias.name.endswith(".main"))
    elif isinstance(node, ast.ImportFrom):
        name = node.module or ""
        if name == "main" or name.endswith(".main"):
            offenders.append(name)
assert not offenders, offenders

# No direct object-task endpoint implementation may creep back into main.py.
main_tree = ast.parse(inspect.getsource(backend))
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
            assert first.value not in {
                "/api/objects/{object_id}/tasks",
                "/api/tasks/{task_id}/complete",
            }, (node.name, first.value)

# Preserve the pre-existing name collision instead of changing public module
# behavior: backend.create_task must still be the later /api/tasks handler, not
# the extracted object-task POST handler.
object_create_route = next(
    r for r in routes
    if getattr(r, "path", None) == "/api/objects/{object_id}/tasks" and "POST" in getattr(r, "methods", set())
)
global_create_route = next(
    r for r in routes
    if getattr(r, "path", None) == "/api/tasks" and "POST" in getattr(r, "methods", set())
)
assert backend.create_task is global_create_route.endpoint
assert backend.create_task is not object_create_route.endpoint

print("phase4-tasks-contract-ok", len(ROUTES))
'''


def test_phase4_tasks_contract_in_clean_process():
    env = os.environ.copy()
    env["PHASE4_BACKEND_DIR"] = os.path.dirname(backend.__file__)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase4-tasks-contract-ok 3" in result.stdout


class FakeObjectsLib:
    def __init__(self):
        self.add_calls = []
        self.completed = []

    def list_tasks(self, object_id):
        return [{"id": "t1", "object_id": object_id}]

    def add_task(self, object_id, text, author):
        self.add_calls.append((object_id, text, author))
        return "new-task"

    def complete_task(self, task_id):
        if task_id == "missing":
            raise ValueError("task missing")
        self.completed.append(task_id)


def _handlers(fake):
    _, handlers = tasks_routes.create_tasks_router(tasks_routes.TasksRouteDeps(
        get_current_user=lambda: {"id": 1},
        require_owner=lambda: None,
        require_object_access=lambda: None,
        load_objekte_lib=lambda: fake,
    ))
    return handlers


def test_tasks_behavior_preserved():
    fake = FakeObjectsLib()
    handlers = _handlers(fake)

    assert handlers.get_tasks("obj-1", user={"id": 1}, _=None) == {
        "tasks": [{"id": "t1", "object_id": "obj-1"}]
    }

    result = handlers.create_task(
        "obj-1",
        tasks_routes.TaskBody(text="  Проверить стену  "),
        user={"id": 7, "first_name": "Boris"},
        _=None,
    )
    assert result == {"task_id": "new-task"}
    assert fake.add_calls == [("obj-1", "Проверить стену", "Boris")]

    assert handlers.complete_task("t1", user={"id": 1}, _=None) == {"status": "ok"}
    assert fake.completed == ["t1"]


def test_task_validation_and_missing_semantics_preserved():
    fake = FakeObjectsLib()
    handlers = _handlers(fake)

    with pytest.raises(HTTPException) as exc:
        handlers.create_task(
            "obj-1",
            tasks_routes.TaskBody(text="   "),
            user={"id": 7},
            _=None,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Текст не может быть пустым"

    with pytest.raises(HTTPException) as exc:
        handlers.complete_task("missing", user={"id": 7}, _=None)
    assert exc.value.status_code == 404
    assert exc.value.detail == "task missing"
