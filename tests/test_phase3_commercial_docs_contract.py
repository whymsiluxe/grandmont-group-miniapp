"""Strict public contract and focused behavior guard for commercial PDFs."""
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
import routes.commercial_docs as commercial  # noqa: E402


PROBE = r'''
import ast
import inspect
import os
import sys

sys.path.insert(0, os.environ["PHASE3_BACKEND_DIR"])
import main as backend
import routes.commercial_docs as commercial


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
        result.append((item["name"], item["in"], bool(item.get("required", False)), schema.get("type")))
    return result

routes = list(iter_routes(backend.app))
openapi = backend.app.openapi()
dependency_map = {
    "user": backend.get_current_user,
    "commercial_access": backend.require_angebot_access,
}
AUTH_HEADERS = [
    ("authorization", "header", False, None),
    ("x-telegram-init-data", "header", False, None),
]

ROUTES = [
    ("POST", "/api/angebot", "create_angebot", "create_angebot_api_angebot_post", "AngebotBody"),
    ("POST", "/api/rechnung", "create_rechnung", "create_rechnung_api_rechnung_post", "RechnungBody"),
]
for method, path, handler_name, operation_id, body_model in ROUTES:
    matches = [r for r in routes if getattr(r, "path", None) == path and method in getattr(r, "methods", set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    assert route.endpoint is getattr(backend, handler_name), (method, path, "endpoint identity")
    assert route.name == handler_name, (method, path, route.name)
    assert route.methods == {method}, (method, path, route.methods)
    assert route.unique_id == operation_id, (method, path, route.unique_id)
    assert route.status_code is None and route.response_model is None and route.responses == {}, (method, path)
    actual_deps = [
        (dep.name, next(key for key, call in dependency_map.items() if dep.call is call))
        for dep in route.dependant.dependencies
    ]
    assert actual_deps == [("user", "user"), ("_", "commercial_access")], (method, path, actual_deps)

    operation = openapi["paths"][path][method.lower()]
    assert operation["operationId"] == operation_id, (method, path, operation["operationId"])
    assert parameter_contract(operation) == AUTH_HEADERS, (method, path, parameter_contract(operation))
    body = operation.get("requestBody")
    assert body is not None and body.get("required") is True, (method, path, body)
    assert body["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{body_model}"}, (method, path, body)

# Exact request-model field contract from the pre-extraction implementation.
def assert_model(model, expected):
    assert list(model.model_fields) == list(expected), (model.__name__, list(model.model_fields), list(expected))
    for name, (annotation, required, default) in expected.items():
        field = model.model_fields[name]
        assert field.annotation == annotation, (model.__name__, name, field.annotation, annotation)
        assert field.is_required() is required, (model.__name__, name, field.is_required(), required)
        if not required:
            assert field.default == default, (model.__name__, name, field.default, default)

assert_model(backend.AngebotKunde, {
    "typ": (str, False, "privat"), "anrede": (str, False, ""), "name": (str, True, None),
    "kontakt": (str, False, ""), "ustId": (str, False, ""), "adresse": (str, False, ""), "email": (str, False, ""),
})
assert_model(backend.AngebotPosition, {
    "titel": (str, True, None), "beschreibung": (str, False, ""), "menge": (float, True, None),
    "einheit": (str, False, "Stk"), "preis": (float, True, None),
})
assert_model(backend.AngebotBody, {
    "kunde": (backend.AngebotKunde, True, None), "objektAdresse": (str, False, ""),
    "positionen": (list[backend.AngebotPosition], True, None), "gueltigTage": (int, False, 14),
    "mwstSatz": (float, False, 19), "anzahlungPct": (float, False, 40), "signatureBase64": (str | None, False, None),
})
assert_model(backend.RechnungKunde, {
    "typ": (str, False, "privat"), "anrede": (str, False, ""), "name": (str, True, None),
    "kontakt": (str, False, ""), "ustId": (str, False, ""), "adresse": (str, False, ""), "email": (str, False, ""),
})
assert_model(backend.RechnungPosition, {
    "titel": (str, True, None), "beschreibung": (str, False, ""), "menge": (float, True, None),
    "einheit": (str, False, "Stk"), "preis": (float, True, None),
})
assert_model(backend.RechnungBody, {
    "nummer": (str, True, None), "kunde": (backend.RechnungKunde, True, None), "projekt": (str, False, ""),
    "positionen": (list[backend.RechnungPosition], True, None), "zahlungsfristTage": (int, False, 14),
    "mwstSatz": (float, False, 19), "signatureBase64": (str | None, False, None),
})

# Dependency direction and composition boundaries.
tree = ast.parse(inspect.getsource(commercial))
offenders = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        offenders.extend(alias.name for alias in node.names if alias.name == "main" or alias.name.endswith(".main"))
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == "main" or module.endswith(".main"):
            offenders.append(module)
assert not offenders, offenders

main_source = inspect.getsource(backend)
main_tree = ast.parse(main_source)
for node in ast.walk(main_tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for decorator in node.decorator_list:
        if not (isinstance(decorator, ast.Call) and decorator.args):
            continue
        func = decorator.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "app":
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and first.value in {"/api/angebot", "/api/rechnung"}:
                raise AssertionError((node.name, first.value))

# Shared script helper must remain available for Objects composition.
assert main_source.count("def _require_server_script(") == 1
assert "require_server_script=lambda script_path, label: _require_server_script" in main_source

# Compatibility aliases must point at the registered extracted handlers.
for method, path, handler_name, *_ in ROUTES:
    route = next(r for r in routes if getattr(r, "path", None) == path and method in getattr(r, "methods", set()))
    assert getattr(backend, handler_name) is route.endpoint, (path, handler_name)

print("phase3-commercial-contract-ok", len(ROUTES))
'''


def test_phase3_commercial_contract_in_clean_process():
    env = os.environ.copy()
    env["PHASE3_BACKEND_DIR"] = os.path.dirname(backend.__file__)
    result = subprocess.run([sys.executable, "-c", PROBE], text=True, capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase3-commercial-contract-ok 2" in result.stdout


def _deps(tmp_path, role="owner"):
    return commercial.CommercialDocsRouteDeps(
        get_current_user=lambda: {"id": 7},
        get_role=lambda: role,
        bot_token=lambda: "dummy",
        angebot_script=lambda: str(tmp_path / "angebot.js"),
        rechnung_script=lambda: str(tmp_path / "rechnung.js"),
        angebot_out_dir=lambda: str(tmp_path),
        rechnung_out_dir=lambda: str(tmp_path),
    )


def test_owner_access_guard_preserves_legacy_rule(tmp_path):
    _, handlers = commercial.create_commercial_docs_router(_deps(tmp_path, role="worker"))
    with pytest.raises(HTTPException) as exc:
        handlers.require_angebot_access(role="worker")
    assert exc.value.status_code == 403
    assert exc.value.detail == "только owner может создавать Angebot"
    assert handlers.require_angebot_access(role="owner") is None


def test_safe_pdf_filename_preserves_sanitization():
    assert commercial.safe_pdf_filename("Angebot", " ACME / Bau GmbH.pdf ") == "Angebot_ACME_Bau_GmbH.pdf"
    assert commercial.safe_pdf_filename("Rechnung", "...") == "Rechnung_kunde.pdf"


def test_pdf_generator_missing_script_preserves_error(tmp_path):
    missing = tmp_path / "missing.js"
    with pytest.raises(HTTPException) as exc:
        commercial.run_pdf_generator(str(missing), {"x": 1})
    assert exc.value.status_code == 500
    assert exc.value.detail == "PDF-Generator fehlt: missing.js"


def test_empty_positions_and_invoice_number_rejected_before_generation(tmp_path):
    _, handlers = commercial.create_commercial_docs_router(_deps(tmp_path))
    angebot = commercial.AngebotBody(
        kunde=commercial.AngebotKunde(name="A"),
        positionen=[],
    )
    with pytest.raises(HTTPException) as exc:
        handlers.create_angebot(body=angebot, user={"id": 7}, _=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "mindestens eine Position erforderlich"

    rechnung = commercial.RechnungBody(
        nummer="   ",
        kunde=commercial.RechnungKunde(name="A"),
        positionen=[commercial.RechnungPosition(titel="x", menge=1, preis=1)],
    )
    with pytest.raises(HTTPException) as exc:
        handlers.create_rechnung(body=rechnung, user={"id": 7}, _=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Rechnung-Nr. erforderlich"
