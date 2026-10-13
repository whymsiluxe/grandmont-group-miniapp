#!/usr/bin/env python3
"""One-shot guarded transform for Phase 7 object-media extraction.

The script is intentionally temporary. It removes only the five known object-media
route implementations from backend/main.py, wires backend/routes/object_media.py,
and adds a strict contract/behavior regression test.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "backend" / "main.py"
ROUTE = ROOT / "backend" / "routes" / "object_media.py"
TEST = ROOT / "tests" / "test_phase7_object_media_contract.py"

TARGET_FUNCTIONS = {
    "upload_object_image",
    "delete_object_image",
    "get_object_image_file",
    "upload_blocker_photo",
    "get_blocker_photo",
}

ROUTE_SOURCE = '''"""Object and blocker photo HTTP routes extracted from backend.main."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


@dataclass(frozen=True)
class ObjectMediaDeps:
    get_current_user: Callable[..., Any]
    require_owner: Callable[..., Any]
    require_object_access: Callable[..., Any]
    load_object_images: Callable[[], dict]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    object_images_file: Callable[[], str]
    object_photo_dir: Callable[[], str]
    blocker_photo_dir: Callable[[], str]
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]
    object_photo_max: int
    object_photo_max_bytes: int
    blocker_photo_max_bytes: int


@dataclass(frozen=True)
class ObjectMediaHandlers:
    upload_object_image: Callable[..., Any]
    delete_object_image: Callable[..., Any]
    get_object_image_file: Callable[..., Any]
    upload_blocker_photo: Callable[..., Any]
    get_blocker_photo: Callable[..., Any]


def create_object_media_router(deps: ObjectMediaDeps) -> tuple[APIRouter, ObjectMediaHandlers]:
    router = APIRouter()

    # Preserve the legacy import-time blocker-photo directory creation behavior.
    os.makedirs(deps.blocker_photo_dir(), exist_ok=True)

    @router.post("/api/objects/{object_id}/image")
    async def upload_object_image(
        object_id: str,
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        raw = await file.read()
        if len(raw) > deps.object_photo_max_bytes:
            raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
        detected = deps.sniff_image(raw)
        if not detected:
            raise HTTPException(400, "Файл должен быть изображением")
        ext = deps.allowed_image_mime_ext[detected]
        photo_dir = deps.object_photo_dir()
        os.makedirs(photo_dir, exist_ok=True)
        fname = f"{uuid.uuid4().hex}.{ext}"
        fpath = os.path.join(photo_dir, fname)
        with open(fpath, "wb") as f_out:
            f_out.write(raw)

        def _mutator(images):
            existing = images.get(object_id) or []
            if len(existing) >= deps.object_photo_max:
                raise HTTPException(400, f"Максимум {deps.object_photo_max} фото на объект")
            images[object_id] = existing + [fname]
            return images[object_id]

        try:
            photos = deps.update_json_transaction(deps.object_images_file(), {}, _mutator)
        except HTTPException:
            if os.path.exists(fpath):
                os.remove(fpath)
            raise
        return {"status": "ok", "photos": photos}

    @router.delete("/api/objects/{object_id}/image/{fname}")
    def delete_object_image(
        object_id: str,
        fname: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        safe_name = os.path.basename(fname)

        def _mutator(images):
            existing = images.get(object_id) or []
            if safe_name not in existing:
                raise HTTPException(404, "Фото не найдено")
            images[object_id] = [f for f in existing if f != safe_name]
            return images[object_id]

        photos = deps.update_json_transaction(deps.object_images_file(), {}, _mutator)
        path = os.path.join(deps.object_photo_dir(), safe_name)
        if os.path.exists(path):
            os.remove(path)
        return {"status": "ok", "photos": photos}

    @router.get("/api/objects/{object_id}/image/file")
    def get_object_image_file(
        object_id: str,
        index: int = 0,
        user: dict = Depends(deps.get_current_user),
    ):
        # Deliberately preserves the legacy contract: authenticated users can read
        # object images without the stricter require_object_access dependency.
        images = deps.load_object_images()
        photos = images.get(object_id) or []
        if not photos or index < 0 or index >= len(photos):
            raise HTTPException(404, "Фото не загружено")
        path = os.path.join(deps.object_photo_dir(), photos[index])
        if not os.path.exists(path):
            raise HTTPException(404, "Файл отсутствует")
        return FileResponse(path)

    @router.post("/api/objects/{object_id}/blocker-photo")
    async def upload_blocker_photo(
        object_id: str,
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        raw = await file.read()
        if len(raw) > deps.blocker_photo_max_bytes:
            raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
        detected = deps.sniff_image(raw)
        if not detected:
            raise HTTPException(400, "Файл должен быть изображением")
        ext = deps.allowed_image_mime_ext[detected]
        fname = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(deps.blocker_photo_dir(), fname), "wb") as out:
            out.write(raw)
        return {"photo_url": f"/api/objects/{object_id}/blocker-photo/{fname}"}

    @router.get("/api/objects/{object_id}/blocker-photo/{fname}")
    def get_blocker_photo(
        object_id: str,
        fname: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        if "/" in fname or ".." in fname:
            raise HTTPException(400, "Некорректное имя файла")
        path = os.path.join(deps.blocker_photo_dir(), fname)
        if not os.path.exists(path):
            raise HTTPException(404, "Файл отсутствует")
        return FileResponse(path)

    return router, ObjectMediaHandlers(
        upload_object_image=upload_object_image,
        delete_object_image=delete_object_image,
        get_object_image_file=get_object_image_file,
        upload_blocker_photo=upload_blocker_photo,
        get_blocker_photo=get_blocker_photo,
    )
'''

TEST_SOURCE = '''"""Strict contract and behavior guard for Phase 7 object-media extraction."""
import ast
import asyncio
import inspect
import io
import json
import os
import sys
import tempfile

import pytest
from fastapi import HTTPException, UploadFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import main as backend  # noqa: E402
import routes.object_media as object_media_routes  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


def _route(method: str, path: str):
    matches = [
        route for route in iter_app_routes(backend.app)
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
    ]
    assert len(matches) == 1, (method, path, len(matches))
    return matches[0]


def test_object_media_routes_keep_exact_registration_contract():
    dependency_map = {
        backend.get_current_user: "user",
        backend.require_owner: "owner",
        backend.require_object_access: "object_access",
    }
    cases = [
        ("POST", "/api/objects/{object_id}/image", "upload_object_image", "upload_object_image_api_objects__object_id__image_post", [("user", "user"), ("_", "owner")]),
        ("DELETE", "/api/objects/{object_id}/image/{fname}", "delete_object_image", "delete_object_image_api_objects__object_id__image__fname__delete", [("user", "user"), ("_", "owner")]),
        ("GET", "/api/objects/{object_id}/image/file", "get_object_image_file", "get_object_image_file_api_objects__object_id__image_file_get", [("user", "user")]),
        ("POST", "/api/objects/{object_id}/blocker-photo", "upload_blocker_photo", "upload_blocker_photo_api_objects__object_id__blocker_photo_post", [("user", "user"), ("_", "object_access")]),
        ("GET", "/api/objects/{object_id}/blocker-photo/{fname}", "get_blocker_photo", "get_blocker_photo_api_objects__object_id__blocker_photo__fname__get", [("user", "user"), ("_", "object_access")]),
    ]
    for method, path, name, unique_id, expected_deps in cases:
        route = _route(method, path)
        assert route.name == name
        assert route.endpoint.__name__ == name
        assert route.endpoint.__module__ in {"routes.object_media", "miniapp.routes.object_media"}
        assert route.endpoint is getattr(backend, name)
        assert route.methods == {method}
        assert route.unique_id == unique_id
        assert route.status_code is None
        assert route.response_model is None
        assert route.responses == {}
        actual_deps = [(dep.name, dependency_map[dep.call]) for dep in route.dependant.dependencies]
        assert actual_deps == expected_deps


def test_object_media_openapi_shape_stays_stable():
    spec = backend.app.openapi()["paths"]
    upload = spec["/api/objects/{object_id}/image"]["post"]
    delete = spec["/api/objects/{object_id}/image/{fname}"]["delete"]
    image_file = spec["/api/objects/{object_id}/image/file"]["get"]
    blocker_upload = spec["/api/objects/{object_id}/blocker-photo"]["post"]
    blocker_file = spec["/api/objects/{object_id}/blocker-photo/{fname}"]["get"]
    assert upload["operationId"] == "upload_object_image_api_objects__object_id__image_post"
    assert delete["operationId"] == "delete_object_image_api_objects__object_id__image__fname__delete"
    assert image_file["operationId"] == "get_object_image_file_api_objects__object_id__image_file_get"
    assert blocker_upload["operationId"] == "upload_blocker_photo_api_objects__object_id__blocker_photo_post"
    assert blocker_file["operationId"] == "get_blocker_photo_api_objects__object_id__blocker_photo__fname__get"
    assert upload["requestBody"]["required"] is True
    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert blocker_upload["requestBody"]["required"] is True
    assert "multipart/form-data" in blocker_upload["requestBody"]["content"]
    assert [p["name"] for p in image_file["parameters"] if p["in"] == "query"] == ["index"]


def test_object_media_module_has_no_back_edge_and_main_has_no_direct_routes():
    tree = ast.parse(inspect.getsource(object_media_routes))
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
    forbidden = {
        "/api/objects/{object_id}/image",
        "/api/objects/{object_id}/image/{fname}",
        "/api/objects/{object_id}/image/file",
        "/api/objects/{object_id}/blocker-photo",
        "/api/objects/{object_id}/blocker-photo/{fname}",
    }
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


def _handlers(tmpdir, *, transaction_override=None, max_photos=3):
    object_photo_dir = os.path.join(tmpdir, "object-photos")
    blocker_photo_dir = os.path.join(tmpdir, "blocker-photos")
    images_file = os.path.join(tmpdir, "object-images.json")

    def load_images():
        if not os.path.exists(images_file):
            return {}
        with open(images_file, encoding="utf-8") as fh:
            return json.load(fh)

    def transaction(path, default, mutator):
        value = load_images() if os.path.exists(path) else dict(default)
        result = mutator(value)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
        return result

    deps = object_media_routes.ObjectMediaDeps(
        get_current_user=lambda: {"id": 10},
        require_owner=lambda: None,
        require_object_access=lambda: None,
        load_object_images=load_images,
        update_json_transaction=transaction_override or transaction,
        object_images_file=lambda: images_file,
        object_photo_dir=lambda: object_photo_dir,
        blocker_photo_dir=lambda: blocker_photo_dir,
        sniff_image=lambda raw: "image/png" if raw else None,
        allowed_image_mime_ext={"image/png": "png"},
        object_photo_max=max_photos,
        object_photo_max_bytes=8 * 1024 * 1024,
        blocker_photo_max_bytes=8 * 1024 * 1024,
    )
    _, handlers = object_media_routes.create_object_media_router(deps)
    return handlers, object_photo_dir, blocker_photo_dir, images_file


def test_object_image_upload_get_delete_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        handlers, photo_dir, _, images_file = _handlers(tmp)
        uploaded = asyncio.run(handlers.upload_object_image(
            "OBJ-1", UploadFile(filename="x.bin", file=io.BytesIO(b"png-data")), user={"id": 1}, _=None
        ))
        assert uploaded["status"] == "ok"
        assert len(uploaded["photos"]) == 1
        fname = uploaded["photos"][0]
        assert fname.endswith(".png")
        assert os.path.isfile(os.path.join(photo_dir, fname))
        with open(images_file, encoding="utf-8") as fh:
            assert json.load(fh) == {"OBJ-1": [fname]}

        response = handlers.get_object_image_file("OBJ-1", index=0, user={"id": 10})
        assert os.path.basename(response.path) == fname

        deleted = handlers.delete_object_image("OBJ-1", fname, user={"id": 1}, _=None)
        assert deleted == {"status": "ok", "photos": []}
        assert not os.path.exists(os.path.join(photo_dir, fname))


def test_object_image_failed_metadata_transaction_rolls_back_binary():
    def fail_transaction(path, default, mutator):
        raise HTTPException(400, "boom")

    with tempfile.TemporaryDirectory() as tmp:
        handlers, photo_dir, _, _ = _handlers(tmp, transaction_override=fail_transaction)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(handlers.upload_object_image(
                "OBJ-1", UploadFile(filename="x.bin", file=io.BytesIO(b"png-data")), user={"id": 1}, _=None
            ))
        assert exc.value.status_code == 400
        assert os.path.isdir(photo_dir)
        assert os.listdir(photo_dir) == []


def test_blocker_photo_upload_get_and_filename_guard():
    with tempfile.TemporaryDirectory() as tmp:
        handlers, _, blocker_dir, _ = _handlers(tmp)
        result = asyncio.run(handlers.upload_blocker_photo(
            "OBJ-9", UploadFile(filename="x.bin", file=io.BytesIO(b"photo")), user={"id": 10}, _=None
        ))
        fname = result["photo_url"].rsplit("/", 1)[-1]
        assert os.path.isfile(os.path.join(blocker_dir, fname))
        response = handlers.get_blocker_photo("OBJ-9", fname, user={"id": 10}, _=None)
        assert os.path.basename(response.path) == fname
        with pytest.raises(HTTPException) as exc:
            handlers.get_blocker_photo("OBJ-9", "../secret.png", user={"id": 10}, _=None)
        assert exc.value.status_code == 400
'''

WIRING = '''# ---------- Object media domain ----------
try:
    from .routes.object_media import ObjectMediaDeps, create_object_media_router
except ImportError:
    from routes.object_media import ObjectMediaDeps, create_object_media_router  # noqa: E402

_object_media_router, _object_media_handlers = create_object_media_router(
    ObjectMediaDeps(
        get_current_user=get_current_user,
        require_owner=require_owner,
        require_object_access=require_object_access,
        load_object_images=lambda: _load_object_images(),
        update_json_transaction=lambda path, default, mutator: update_json_transaction(path, default, mutator),
        object_images_file=lambda: OBJECT_IMAGES_FILE,
        object_photo_dir=lambda: OBJECT_PHOTO_DIR,
        blocker_photo_dir=lambda: BLOCKER_PHOTO_DIR,
        sniff_image=lambda raw: sniff_image(raw),
        allowed_image_mime_ext=_ALLOWED_IMAGE_MIME_EXT,
        object_photo_max=OBJECT_PHOTO_MAX,
        object_photo_max_bytes=8 * 1024 * 1024,
        blocker_photo_max_bytes=PHOTO_MAX_BYTES,
    )
)
app.include_router(_object_media_router)

upload_object_image = _object_media_handlers.upload_object_image
delete_object_image = _object_media_handlers.delete_object_image
get_object_image_file = _object_media_handlers.get_object_image_file
upload_blocker_photo = _object_media_handlers.upload_blocker_photo
get_blocker_photo = _object_media_handlers.get_blocker_photo


'''


def remove_target_functions(source: str) -> str:
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TARGET_FUNCTIONS:
            if node.name in found:
                raise SystemExit(f"duplicate target function: {node.name}")
            start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
            found[node.name] = (start, node.end_lineno)
    missing = TARGET_FUNCTIONS - set(found)
    if missing:
        raise SystemExit(f"missing target functions: {sorted(missing)}")

    lines = source.splitlines(keepends=True)
    for start, end in sorted(found.values(), reverse=True):
        del lines[start:end]
    return "".join(lines)


def main() -> None:
    source = MAIN.read_text(encoding="utf-8")
    if "routes.object_media" in source:
        raise SystemExit("object_media wiring already present")

    source = remove_target_functions(source)

    blocker_setup = "# moved to core/paths.py -- BLOCKER_PHOTO_DIR\nos.makedirs(BLOCKER_PHOTO_DIR, exist_ok=True)\n\n\n"
    if source.count(blocker_setup) != 1:
        raise SystemExit(f"expected exactly one blocker setup block, got {source.count(blocker_setup)}")
    source = source.replace(blocker_setup, "# Blocker-photo directory setup is owned by routes/object_media.py.\n\n", 1)

    anchor = "def _save_object_images(images: dict):\n    _atomic_write_json(OBJECT_IMAGES_FILE, images)\n\n\n# moved to core/limits.py -- OBJECT_PHOTO_MAX\n"
    if source.count(anchor) != 1:
        raise SystemExit(f"object-media wiring anchor mismatch: {source.count(anchor)}")
    replacement = (
        "def _save_object_images(images: dict):\n"
        "    _atomic_write_json(OBJECT_IMAGES_FILE, images)\n\n\n"
        "# moved to core/limits.py -- OBJECT_PHOTO_MAX\n\n\n"
        + WIRING
    )
    source = source.replace(anchor, replacement, 1)

    # Guard the central goal: no direct target app decorators remain in main.
    parsed = ast.parse(source)
    remaining = {
        node.name for node in ast.walk(parsed)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TARGET_FUNCTIONS
    }
    if remaining:
        raise SystemExit(f"target implementations still present: {sorted(remaining)}")

    MAIN.write_text(source, encoding="utf-8")
    ROUTE.write_text(ROUTE_SOURCE, encoding="utf-8")
    TEST.write_text(TEST_SOURCE, encoding="utf-8")
    print("Phase 7 object-media transform applied")


if __name__ == "__main__":
    main()
