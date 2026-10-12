"""Contract and behavior guard for Phase 5 object documents extraction."""
import asyncio
import copy
import io
import os
import subprocess
import sys
import tempfile

import pytest
from fastapi import HTTPException, UploadFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
from core.object_info_store import ObjectInfoStore  # noqa: E402
import routes.documents as documents_routes  # noqa: E402


PROBE = r'''
import ast
import inspect
import os
import sys
sys.path.insert(0, os.environ["PHASE5_BACKEND_DIR"])
import main as backend
import routes.documents as documents_routes


def iter_routes(app_or_router):
    for route in app_or_router.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from iter_routes(nested)
        else:
            yield route

routes = list(iter_routes(backend.app))
deps = {
    "user": backend.get_current_user,
    "owner": backend.require_owner,
    "access": backend.require_object_access,
}
ROUTES = [
    ("GET", "/api/objects/{object_id}/documents", "get_object_documents", [("user","user"),("_","access")]),
    ("POST", "/api/objects/{object_id}/documents", "upload_object_document", [("user","user"),("_","access")]),
    ("DELETE", "/api/objects/{object_id}/documents/{doc_id}", "delete_object_document", [("user","user"),("_","owner")]),
    ("GET", "/api/objects/{object_id}/documents/{fname}/file", "get_object_document_file", [("user","user"),("_","access")]),
]
for method, path, name, expected_deps in ROUTES:
    matches = [r for r in routes if getattr(r, "path", None) == path and method in getattr(r, "methods", set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    assert route.endpoint is getattr(backend, name), (method, path, "alias")
    assert route.endpoint.__name__ == name
    assert route.endpoint.__module__ in {"routes.documents", "miniapp.routes.documents"}
    actual = [(d.name, next(k for k, call in deps.items() if d.call is call)) for d in route.dependant.dependencies]
    assert actual == expected_deps, (method, path, actual)

openapi = backend.app.openapi()
post = openapi["paths"]["/api/objects/{object_id}/documents"]["post"]
assert post["operationId"] == "upload_object_document_api_objects__object_id__documents_post"
assert post["requestBody"]["required"] is True
assert "multipart/form-data" in post["requestBody"]["content"]

# No back-edge to main.py from the extracted route module.
tree = ast.parse(inspect.getsource(documents_routes))
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        assert all(alias.name != "main" and not alias.name.endswith(".main") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        name = node.module or ""
        assert name != "main" and not name.endswith(".main")

# No direct document endpoint implementation may return to main.py.
main_tree = ast.parse(inspect.getsource(backend))
paths = {
    "/api/objects/{object_id}/documents",
    "/api/objects/{object_id}/documents/{doc_id}",
    "/api/objects/{object_id}/documents/{fname}/file",
}
for node in ast.walk(main_tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for dec in node.decorator_list:
        if not (isinstance(dec, ast.Call) and dec.args):
            continue
        func = dec.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "app"):
            continue
        arg = dec.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            assert arg.value not in paths, (node.name, arg.value)

# Both Objects and Documents must be wired to the same store abstraction,
# and implementation helpers must no longer be defined in main.py.
source = inspect.getsource(backend)
assert "ObjectInfoStore(" in source
assert "object_info_entry=_object_info_store.entry" in source
assert "ensure_object_info_entry=_object_info_store.ensure_entry" in source
for helper in ("_load_object_info", "_save_object_info", "_new_object_info_entry", "_ensure_object_info_entry", "_object_info_entry"):
    defs = [n for n in ast.walk(main_tree) if isinstance(n, ast.FunctionDef) and n.name == helper]
    assert defs == [], helper

print("phase5-documents-contract-ok", len(ROUTES))
'''


def test_phase5_documents_contract_clean_process():
    env = os.environ.copy()
    env["PHASE5_BACKEND_DIR"] = os.path.dirname(backend.__file__)
    result = subprocess.run([sys.executable, "-c", PROBE], text=True, capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase5-documents-contract-ok 4" in result.stdout


def test_object_info_store_preserves_legacy_shape_and_semantics():
    state = {"obj": {"description": "x"}}
    store = ObjectInfoStore(
        file_path=lambda: "memory",
        safe_load_json=lambda path, default: state,
        atomic_write_json=lambda path, value: None,
    )
    entry = store.ensure_entry(state, "obj")
    assert entry == {"description": "x", "items": [], "documents": []}
    assert store.entry("missing") == {"items": [], "documents": [], "description": ""}
    bad = {"obj": "broken"}
    with pytest.raises(AttributeError):
        store.ensure_entry(bad, "obj")


class MemoryHarness:
    def __init__(self, doc_dir):
        self.data = {}
        self.doc_dir = doc_dir
        self.history = []

    def safe_load(self, path, default):
        return self.data

    def atomic_write(self, path, value):
        self.data = value

    def update(self, path, default, mutator):
        return mutator(self.data)


def _handlers(harness, sniff=lambda data: "application/pdf"):
    store = ObjectInfoStore(
        file_path=lambda: "memory",
        safe_load_json=harness.safe_load,
        atomic_write_json=harness.atomic_write,
    )
    _, handlers = documents_routes.create_documents_router(documents_routes.DocumentsRouteDeps(
        get_current_user=lambda: {"id": 1},
        require_owner=lambda: None,
        require_object_access=lambda: None,
        store=store,
        object_doc_dir=lambda: harness.doc_dir,
        sniff_image_or_pdf=sniff,
        allowed_image_mime_ext={"image/jpeg": "jpg", "image/png": "png"},
        update_json_transaction=harness.update,
        append_object_history_best_effort=lambda *args, **kwargs: harness.history.append((args, kwargs)),
    ))
    return handlers


def test_document_upload_list_delete_and_file_access_behavior():
    with tempfile.TemporaryDirectory() as td:
        harness = MemoryHarness(td)
        handlers = _handlers(harness)
        upload = UploadFile(filename="plan.pdf", file=io.BytesIO(b"%PDF-test"))
        result = asyncio.run(handlers.upload_object_document("obj-1", upload, user={"id": 7, "first_name": "Boris"}, _=None))
        doc = result["document"]
        assert doc["name"] == "plan.pdf"
        assert doc["content_type"] == "application/pdf"
        assert doc["uploaded_by"] == "Boris"
        assert os.path.exists(os.path.join(td, doc["file"]))
        assert handlers.get_object_documents("obj-1", user={"id": 7}, _=None)["documents"] == [doc]
        response = handlers.get_object_document_file("obj-1", doc["file"], user={"id": 7}, _=None)
        assert response.path == os.path.join(td, doc["file"])
        assert harness.history and harness.history[0][0][1:3] == ("document_uploaded", "Документ загружен")
        assert handlers.delete_object_document("obj-1", doc["id"], user={"id": 1}, _=None) == {"status": "ok"}
        assert not os.path.exists(os.path.join(td, doc["file"]))
        assert handlers.get_object_documents("obj-1", user={"id": 7}, _=None) == {"documents": []}


def test_document_validation_and_missing_semantics():
    with tempfile.TemporaryDirectory() as td:
        harness = MemoryHarness(td)
        bad_handlers = _handlers(harness, sniff=lambda data: None)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(bad_handlers.upload_object_document(
                "obj-1", UploadFile(filename="x.bin", file=io.BytesIO(b"bad")), user={"id": 1}, _=None
            ))
        assert exc.value.status_code == 400
        assert exc.value.detail == "Разрешены только изображения и PDF"

        large_handlers = _handlers(harness)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(large_handlers.upload_object_document(
                "obj-1", UploadFile(filename="huge.pdf", file=io.BytesIO(b"x" * (8 * 1024 * 1024 + 1))), user={"id": 1}, _=None
            ))
        assert exc.value.status_code == 400
        assert "8 МБ" in exc.value.detail

        with pytest.raises(HTTPException) as exc:
            large_handlers.delete_object_document("obj-1", "missing", user={"id": 1}, _=None)
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            large_handlers.get_object_document_file("obj-1", "missing.pdf", user={"id": 1}, _=None)
        assert exc.value.status_code == 404
