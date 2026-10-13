from __future__ import annotations

import ast
from pathlib import Path

import main as backend


TARGETS = {
    ("GET", "/api/health"): "routes.system",
    ("GET", "/api/health/ready"): "routes.system",
    ("GET", "/api/diagnostics"): "routes.system",
    ("GET", "/api/me"): "routes.session_identity",
    ("POST", "/api/session"): "routes.session_identity",
}


def _routes():
    out = {}
    for route in backend.app.routes:
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        methods = getattr(route, "methods", set()) or set()
        if path and endpoint:
            for method in methods:
                out[(method, path)] = route
    return out


def test_phase8_routes_are_owned_by_extracted_modules():
    routes = _routes()
    for key, suffix in TARGETS.items():
        assert routes[key].endpoint.__module__.endswith(suffix)


def test_phase8_operation_ids_preserve_handler_names():
    routes = _routes()
    expected = {
        ("GET", "/api/health"): "health",
        ("GET", "/api/health/ready"): "health_ready",
        ("GET", "/api/diagnostics"): "diagnostics",
        ("GET", "/api/me"): "me",
        ("POST", "/api/session"): "create_session",
    }
    for key, name in expected.items():
        assert routes[key].name == name


def test_main_has_no_direct_phase8_route_decorators():
    tree = ast.parse(Path(backend.__file__).read_text())
    target_paths = {path for _, path in TARGETS}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                if decorator.args[0].value in target_paths:
                    found.append(decorator.args[0].value)
    assert found == []


def test_extracted_modules_do_not_import_main():
    for rel in ("backend/routes/system.py", "backend/routes/session_identity.py"):
        tree = ast.parse(Path(rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name not in {"main", "backend.main"} for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in {"main", "backend.main"}


def test_legacy_aliases_point_to_registered_handlers():
    routes = _routes()
    aliases = {
        ("GET", "/api/health"): backend.health,
        ("GET", "/api/health/ready"): backend.health_ready,
        ("GET", "/api/diagnostics"): backend.diagnostics,
        ("GET", "/api/me"): backend.me,
        ("POST", "/api/session"): backend.create_session,
    }
    for key, alias in aliases.items():
        assert routes[key].endpoint is alias
