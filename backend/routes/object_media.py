"""Object and blocker photo HTTP routes extracted from backend.main."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

try:
    from .. import roadmap_lib as _roadmap_lib
    from ..core.media_paths import resolve_owned_media_path
    from ..core.storage import _safe_load_json
except ImportError:
    import roadmap_lib as _roadmap_lib  # noqa: E402
    from core.media_paths import resolve_owned_media_path  # noqa: E402
    from core.storage import _safe_load_json  # noqa: E402


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


def _safe_stored_filename(value: Any) -> str | None:
    """Return a basename-only stored filename, rejecting corrupt path metadata."""
    name = str(value)
    safe_name = os.path.basename(name)
    if not safe_name or safe_name != name or safe_name in (".", ".."):
        return None
    return safe_name


def _blocker_object_namespace(object_id: str) -> str:
    """Stable filesystem namespace without trusting object IDs as path segments."""
    return hashlib.sha256(str(object_id).encode("utf-8")).hexdigest()


def _legacy_blocker_photo_owned(object_id: str, fname: str) -> bool:
    """Verify a pre-Phase-28 global blocker file through persisted roadmap metadata."""
    store = _safe_load_json(_roadmap_lib.ROADMAP_FILE, _roadmap_lib._default_store())
    if not isinstance(store, dict):
        return False
    blocks = store.get("stage_blocks") or {}
    if not isinstance(blocks, dict):
        return False
    expected_url = f"/api/objects/{object_id}/blocker-photo/{fname}"
    return any(
        isinstance(meta, dict) and meta.get("photo_url") == expected_url
        for meta in blocks.values()
    )


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
        except Exception:
            # Binary is created before metadata commit. Any failed metadata/storage
            # transaction must roll it back, not only HTTPException failures.
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except OSError as cleanup_exc:
                print(f"[object-media] failed to remove orphan {fpath}: {cleanup_exc}")
            raise
        return {"status": "ok", "photos": photos}

    @router.delete("/api/objects/{object_id}/image/{fname}")
    def delete_object_image(
        object_id: str,
        fname: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        safe_name = _safe_stored_filename(fname)
        if not safe_name:
            raise HTTPException(404, "Фото не найдено")

        def _mutator(images):
            existing = images.get(object_id) or []
            if safe_name not in existing:
                raise HTTPException(404, "Фото не найдено")
            images[object_id] = [f for f in existing if f != safe_name]
            return images[object_id]

        photos = deps.update_json_transaction(deps.object_images_file(), {}, _mutator)
        path = resolve_owned_media_path(deps.object_photo_dir(), safe_name)
        if path and os.path.isfile(path):
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
        safe_name = _safe_stored_filename(photos[index])
        if not safe_name:
            raise HTTPException(404, "Файл отсутствует")
        path = resolve_owned_media_path(deps.object_photo_dir(), safe_name)
        if not path or not os.path.isfile(path):
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
        blocker_root = deps.blocker_photo_dir()
        relative_path = os.path.join(_blocker_object_namespace(object_id), fname)
        path = resolve_owned_media_path(blocker_root, relative_path)
        if not path:
            raise HTTPException(500, "Не удалось подготовить хранилище фото")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Re-resolve after directory creation so a pre-existing symlink namespace
        # cannot redirect this object's upload to another media identity.
        path = resolve_owned_media_path(blocker_root, relative_path)
        if not path:
            raise HTTPException(500, "Не удалось подготовить хранилище фото")
        with open(path, "wb") as out:
            out.write(raw)
        return {"photo_url": f"/api/objects/{object_id}/blocker-photo/{fname}"}

    @router.get("/api/objects/{object_id}/blocker-photo/{fname}")
    def get_blocker_photo(
        object_id: str,
        fname: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        safe_name = _safe_stored_filename(fname)
        if not safe_name:
            raise HTTPException(400, "Некорректное имя файла")

        blocker_root = deps.blocker_photo_dir()
        relative_path = os.path.join(_blocker_object_namespace(object_id), safe_name)
        path = resolve_owned_media_path(blocker_root, relative_path)
        if path and os.path.isfile(path):
            return FileResponse(path)

        # Pre-Phase-28 files lived directly in BLOCKER_PHOTO_DIR. Preserve those
        # links only when roadmap metadata proves this exact URL belongs to the
        # requested object; physical file existence alone is no longer authority.
        if not _legacy_blocker_photo_owned(object_id, safe_name):
            raise HTTPException(404, "Файл отсутствует")
        legacy_path = resolve_owned_media_path(blocker_root, safe_name)
        if not legacy_path or not os.path.isfile(legacy_path):
            raise HTTPException(404, "Файл отсутствует")
        return FileResponse(legacy_path)

    return router, ObjectMediaHandlers(
        upload_object_image=upload_object_image,
        delete_object_image=delete_object_image,
        get_object_image_file=get_object_image_file,
        upload_blocker_photo=upload_blocker_photo,
        get_blocker_photo=get_blocker_photo,
    )
