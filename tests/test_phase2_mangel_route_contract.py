"""Strict public-contract and architecture guard for the Phase 2 Mangel extraction."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402

PROBE = r'''
import ast
import inspect
import os
import sys

sys.path.insert(0, os.environ["PHASE2_BACKEND_DIR"])
import main as backend
import routes.mangel as mangel_routes


def iter_routes(app_or_router):
    for route in app_or_router.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from iter_routes(nested)
        else:
            yield route


def parameter_contract(operation):
    result = []
    for item in operation.get("parameters", []):
        schema = item.get("schema", {})
        result.append((
            item["name"], item["in"], bool(item.get("required", False)),
            schema.get("type"), "<required>" if item.get("required") else schema.get("default"),
        ))
    return result


routes = list(iter_routes(backend.app))
openapi = backend.app.openapi()
dependency_map = {
    "user": backend.get_current_user,
    "role": backend.get_role,
    "owner": backend.require_owner,
    "mangel_access": backend.require_mangel_access,
}
AUTH_HEADERS = [
    ("authorization", "header", False, None, None),
    ("x-telegram-init-data", "header", False, None, None),
]

# method, path, handler, operationId, exact top-level deps, local OpenAPI params,
# JSON body component (if applicable), request content type (if applicable).
ROUTES = [
    ("GET", "/api/mangel", "get_mangel_list", "get_mangel_list_api_mangel_get", [("user","user"),("role","role")], [("object_id","query",False,"string","")], None, None),
    ("GET", "/api/mangel/counts", "get_mangel_counts", "get_mangel_counts_api_mangel_counts_get", [("user","user")], [], None, None),
    ("GET", "/api/mangel/{ticket_id}", "get_mangel_ticket", "get_mangel_ticket_api_mangel__ticket_id__get", [("user","user"),("_","mangel_access")], [("ticket_id","path",True,"string","<required>")], None, None),
    ("POST", "/api/mangel", "create_mangel_ticket", "create_mangel_ticket_api_mangel_post", [("user","user"),("role","role")], [], "Body_create_mangel_ticket_api_mangel_post", "multipart/form-data"),
    ("GET", "/api/mangel/photos/{fname}/file", "get_mangel_photo_file", "get_mangel_photo_file_api_mangel_photos__fname__file_get", [("user","user"),("role","role")], [("fname","path",True,"string","<required>")], None, None),
    ("PATCH", "/api/mangel/{ticket_id}/status", "update_mangel_status", "update_mangel_status_api_mangel__ticket_id__status_patch", [("user","user"),("_","owner")], [("ticket_id","path",True,"string","<required>")], "MangelStatusBody", "application/json"),
    ("POST", "/api/mangel/{ticket_id}/comments", "add_mangel_comment", "add_mangel_comment_api_mangel__ticket_id__comments_post", [("user","user"),("_","mangel_access")], [("ticket_id","path",True,"string","<required>")], "MangelCommentBody", "application/json"),
    ("DELETE", "/api/mangel/{ticket_id}", "delete_mangel_ticket", "delete_mangel_ticket_api_mangel__ticket_id__delete", [("user","user"),("_","owner")], [("ticket_id","path",True,"string","<required>")], None, None),
    ("GET", "/api/mangel/{ticket_id}/comments", "get_mangel_comments", "get_mangel_comments_api_mangel__ticket_id__comments_get", [("user","user"),("_","mangel_access")], [("ticket_id","path",True,"string","<required>")], None, None),
]

for method, path, handler_name, operation_id, deps, local_params, body_component, content_type in ROUTES:
    matches = [r for r in routes if getattr(r, "path", None) == path and method in getattr(r, "methods", set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    handler = getattr(backend, handler_name)
    assert route.endpoint is handler, (method, path, "endpoint identity")
    assert route.name == handler_name, (method, path, route.name)
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

    request_body = operation.get("requestBody")
    if body_component:
        assert request_body is not None and request_body.get("required") is True, (method, path, request_body)
        schema = request_body["content"][content_type]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{body_component}"}, (method, path, schema)
    else:
        assert request_body is None, (method, path, request_body)

# Exact Pydantic JSON-body contracts.
assert {
    name: (str(field.annotation), field.is_required(), "<required>" if field.is_required() else field.default)
    for name, field in backend.MangelStatusBody.model_fields.items()
} == {"status": ("<class 'str'>", True, "<required>")}
assert {
    name: (str(field.annotation), field.is_required(), "<required>" if field.is_required() else field.default)
    for name, field in backend.MangelCommentBody.model_fields.items()
} == {"text": ("<class 'str'>", True, "<required>")}

# Multipart body component is part of the external API too. Pin its public schema.
multipart_schema = openapi["components"]["schemas"]["Body_create_mangel_ticket_api_mangel_post"]
assert multipart_schema["type"] == "object"
assert multipart_schema.get("required") == [] or "required" not in multipart_schema
props = multipart_schema["properties"]
assert props["object_id"].get("default") == "" and props["object_id"].get("type") == "string"
assert props["description"].get("default") == "" and props["description"].get("type") == "string"
assert props["assigned_worker_id"].get("default") == "" and props["assigned_worker_id"].get("type") == "string"
assert props["file"].get("format") == "binary"

# No duplicate Mangel registrations.
expected = {(method, path) for method, path, *_ in ROUTES}
seen = {}
for route in routes:
    for method in getattr(route, "methods", set()):
        key = (method, getattr(route, "path", None))
        if key in expected:
            seen[key] = seen.get(key, 0) + 1
assert seen == {key: 1 for key in expected}, seen

# Dependency direction: extracted route module must never import main.py.
tree = ast.parse(inspect.getsource(mangel_routes))
offenders = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        offenders.extend(alias.name for alias in node.names if alias.name == "main" or alias.name.endswith(".main"))
    elif isinstance(node, ast.ImportFrom):
        name = node.module or ""
        if name == "main" or name.endswith(".main"):
            offenders.append(name)
assert not offenders, offenders

# No direct Mangel @app.* implementation may return to main.py.
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
            assert not first.value.startswith("/api/mangel"), (node.name, first.value)

# Compatibility is alias-only and retains the canonical repo-backed module object.
assert backend.ml is backend._load_repo_mangel_lib()
assert backend.delete_mangel_ticket is next(r.endpoint for r in routes if getattr(r,"path",None)=="/api/mangel/{ticket_id}" and "DELETE" in getattr(r,"methods",set()))

print("phase2-mangel-contract-ok", len(ROUTES))
'''


def test_phase2_mangel_contract_in_clean_process():
    env = os.environ.copy()
    env["PHASE2_BACKEND_DIR"] = os.path.dirname(backend.__file__)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase2-mangel-contract-ok 9" in result.stdout
