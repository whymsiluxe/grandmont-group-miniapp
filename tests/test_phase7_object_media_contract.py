"""Strict contract and behavior guard for Phase 7 object-media extraction."""
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


def test_object_image_any_metadata_failure_rolls_back_binary():
    def fail_transaction(path, default, mutator):
        raise OSError("disk full")

    with tempfile.TemporaryDirectory() as tmp:
        handlers, photo_dir, _, _ = _handlers(tmp, transaction_override=fail_transaction)
        with pytest.raises(OSError):
            asyncio.run(handlers.upload_object_image(
                "OBJ-1", UploadFile(filename="x.bin", file=io.BytesIO(b"png-data")), user={"id": 1}, _=None
            ))
        assert os.path.isdir(photo_dir)
        assert os.listdir(photo_dir) == []


def test_object_image_rejects_path_escape_from_corrupt_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        handlers, photo_dir, _, images_file = _handlers(tmp)
        os.makedirs(photo_dir, exist_ok=True)
        secret = os.path.join(tmp, "secret.png")
        with open(secret, "wb") as fh:
            fh.write(b"secret")
        with open(images_file, "w", encoding="utf-8") as fh:
            json.dump({"OBJ-1": ["../secret.png"]}, fh)

        with pytest.raises(HTTPException) as exc:
            handlers.get_object_image_file("OBJ-1", index=0, user={"id": 10})
        assert exc.value.status_code == 404


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
