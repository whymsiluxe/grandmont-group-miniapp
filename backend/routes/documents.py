"""Object-document HTTP routes extracted from ``backend.main``."""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

try:
    from ..core.media_paths import resolve_owned_media_path
    from ..core.object_info_store import ObjectInfoStore
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402
    from core.object_info_store import ObjectInfoStore  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentsRouteDeps:
    get_current_user: Callable[..., dict]
    require_owner: Callable[..., None]
    require_object_access: Callable[..., None]
    store: ObjectInfoStore
    object_doc_dir: Callable[[], str]
    sniff_image_or_pdf: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    append_object_history_best_effort: Callable[..., Any]


@dataclass(frozen=True)
class DocumentsHandlers:
    get_object_documents: Callable[..., dict]
    upload_object_document: Callable[..., Any]
    delete_object_document: Callable[..., dict]
    get_object_document_file: Callable[..., Any]


def create_documents_router(deps: DocumentsRouteDeps) -> tuple[APIRouter, DocumentsHandlers]:
    router = APIRouter()

    @router.get("/api/objects/{object_id}/documents")
    def get_object_documents(
        object_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        entry = deps.store.entry(object_id)
        if not isinstance(entry, dict):
            entry = {}
        docs = entry.get("documents", [])
        return {
            "documents": [doc for doc in docs if isinstance(doc, dict)]
            if isinstance(docs, list)
            else []
        }

    @router.post("/api/objects/{object_id}/documents")
    async def upload_object_document(
        object_id: str,
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        data_bytes = await file.read()
        if len(data_bytes) > 8 * 1024 * 1024:
            raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")
        detected = deps.sniff_image_or_pdf(data_bytes)
        if not detected:
            raise HTTPException(400, "Разрешены только изображения и PDF")
        content_type = detected
        ext = ".pdf" if detected == "application/pdf" else "." + deps.allowed_image_mime_ext[detected]
        fname = f"{uuid.uuid4().hex}{ext}"
        file_path = resolve_owned_media_path(deps.object_doc_dir(), fname)
        if file_path is None:  # defensive: server-generated fname should always pass
            raise HTTPException(500, "Некорректный путь документа")
        with open(file_path, "wb") as output:
            output.write(data_bytes)

        doc = {
            "id": uuid.uuid4().hex,
            "file": fname,
            "name": file.filename or fname,
            "content_type": content_type,
            "uploaded_by": user.get("first_name", str(user["id"])),
            "uploaded_at": int(time.time()),
        }

        def _mutator(data):
            entry = deps.store.ensure_entry(data, object_id)
            entry["documents"].append(doc)
            return doc

        try:
            deps.update_json_transaction(deps.store.file_path(), {}, _mutator)
        except Exception:
            # The binary is written first so metadata never references a missing
            # file.  If metadata persistence fails, roll the fresh binary back.
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass
            raise

        deps.append_object_history_best_effort(
            object_id,
            "document_uploaded",
            "Документ загружен",
            user=user,
            subtitle=doc.get("name", ""),
            at=datetime.fromtimestamp(doc["uploaded_at"], timezone.utc)
            .replace(tzinfo=None)
            .isoformat(),
            meta={
                "document_id": doc.get("id", ""),
                "file": doc.get("file", ""),
                "content_type": doc.get("content_type", ""),
            },
        )
        return {"document": doc}

    @router.delete("/api/objects/{object_id}/documents/{doc_id}")
    def delete_object_document(
        object_id: str,
        doc_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        def _mutator(data):
            entry = data.get(object_id)
            if not isinstance(entry, dict):
                raise HTTPException(404, "Не найдено")
            docs = entry.get("documents", [])
            docs = docs if isinstance(docs, list) else []
            doc = next(
                (
                    item
                    for item in docs
                    if isinstance(item, dict) and item.get("id") == doc_id
                ),
                None,
            )
            if not doc:
                raise HTTPException(404, "Не найдено")
            entry["documents"] = [
                item
                for item in docs
                if not (isinstance(item, dict) and item.get("id") == doc_id)
            ]
            return doc

        doc = deps.update_json_transaction(deps.store.file_path(), {}, _mutator)
        file_path = resolve_owned_media_path(
            deps.object_doc_dir(), str(doc.get("file") or "")
        )
        # Metadata deletion is the primary durable operation. Once it commits the
        # document is no longer reachable through the API. A filesystem cleanup
        # failure must not turn that committed delete into a false 500/404 retry
        # loop; leave an orphan for retention cleanup and log it explicitly.
        if file_path and os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError:
                logger.exception(
                    "Document binary cleanup failed after metadata delete: %s",
                    file_path,
                )
        return {"status": "ok"}

    @router.get("/api/objects/{object_id}/documents/{fname}/file")
    def get_object_document_file(
        object_id: str,
        fname: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        entry = deps.store.entry(object_id)
        docs = entry.get("documents", []) if isinstance(entry, dict) else []
        docs = docs if isinstance(docs, list) else []
        doc = next(
            (
                item
                for item in docs
                if isinstance(item, dict) and item.get("file") == fname
            ),
            None,
        )
        if not doc:
            raise HTTPException(404, "Файл не найден")
        path = resolve_owned_media_path(deps.object_doc_dir(), fname)
        if path is None or not os.path.isfile(path):
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, media_type=doc.get("content_type") or None)

    return router, DocumentsHandlers(
        get_object_documents=get_object_documents,
        upload_object_document=upload_object_document,
        delete_object_document=delete_object_document,
        get_object_document_file=get_object_document_file,
    )
