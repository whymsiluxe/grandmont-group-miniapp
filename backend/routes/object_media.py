"""Object and blocker photo HTTP routes extracted from backend.main."""
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


def _safe_stored_filename(value: Any) -> str | None:
    """Return a basename-only stored filename, rejecting corrupt path metadata."""
    name = str(value)
    safe_name = os.path.basename(name)
    if not safe_name or safe_name != name or safe_name in (".", ".."):
        return None
    return safe_name


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
        safe_name = _safe_stored_filename(photos[index])
        if not safe_name:
            raise HTTPException(404, "Файл отсутствует")
        path = os.path.join(deps.object_photo_dir(), safe_name)
        if not os.path.isfile(path):
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
