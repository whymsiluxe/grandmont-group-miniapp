"""Mangel/defects API domain extracted from ``backend.main``.

The module owns the existing defects HTTP/orchestration flow. Runtime collaborators
are injected by the composition root so this module never imports ``main.py`` and
legacy direct-call callers can be kept temporarily through aliases in ``main``.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.responses import FileResponse

logger = logging.getLogger(__name__)


class MangelStatusBody(BaseModel):
    status: str


class MangelCommentBody(BaseModel):
    text: str


@dataclass(frozen=True)
class MangelRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    load_mangel_lib: Callable[[], Any]
    load_objekte_lib: Callable[[], Any]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: Mapping[str, str]
    mangel_photo_dir: Callable[[], str]
    photo_dir: Callable[[], str]
    can_access_object: Callable[[dict, str, str], bool]
    append_object_history_best_effort: Callable[..., Any]


@dataclass(frozen=True)
class MangelHandlers:
    require_mangel_access: Callable[..., None]
    enrich_mangel_tickets_with_author: Callable[[list], list]
    get_mangel_list: Callable[..., dict]
    get_mangel_counts: Callable[..., dict]
    get_mangel_ticket: Callable[..., dict]
    create_mangel_ticket: Callable[..., Any]
    get_mangel_photo_file: Callable[..., FileResponse]
    update_mangel_status: Callable[..., dict]
    add_mangel_comment: Callable[..., dict]
    delete_mangel_ticket: Callable[..., dict]
    get_mangel_comments: Callable[..., dict]


def create_mangel_router(deps: MangelRouteDeps) -> tuple[APIRouter, MangelHandlers]:
    """Build an isolated Mangel router using only explicitly injected collaborators."""
    router = APIRouter()

    def require_mangel_access(
        ticket_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        """Resolve ticket existence; all workers may view/comment any defect."""
        mangel_lib = deps.load_mangel_lib()
        try:
            mangel_lib.get_ticket(ticket_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    def enrich_mangel_tickets_with_author(tickets: list) -> list:
        profiles = deps.load_worker_profiles()
        for ticket in tickets:
            created_by = ticket.get("created_by")
            if created_by:
                created_id = str(created_by)
                ticket["created_by_name"] = deps.sanitize_display_name(
                    profiles.get(created_id, {}).get("name"), created_id
                )
            assigned = ticket.get("assigned_worker_id")
            if assigned:
                assigned_id = str(assigned)
                ticket["assigned_worker_name"] = deps.sanitize_display_name(
                    profiles.get(assigned_id, {}).get("name"), assigned_id
                )
        return tickets

    @router.get("/api/mangel")
    def get_mangel_list(
        object_id: str = "",
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        mangel_lib = deps.load_mangel_lib()
        tickets = enrich_mangel_tickets_with_author(
            mangel_lib.list_tickets(object_id or None)
        )
        return {"tickets": tickets, "total": len(tickets)}

    @router.get("/api/mangel/counts")
    def get_mangel_counts(user: dict = Depends(deps.get_current_user)):
        return deps.load_mangel_lib().count_by_status()

    @router.get("/api/mangel/{ticket_id}")
    def get_mangel_ticket(
        ticket_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(require_mangel_access),
    ):
        mangel_lib = deps.load_mangel_lib()
        try:
            return enrich_mangel_tickets_with_author(
                [mangel_lib.get_ticket(ticket_id)]
            )[0]
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/mangel")
    async def create_mangel_ticket(
        object_id: str = Form(""),
        description: str = Form(""),
        assigned_worker_id: str = Form(""),
        file: UploadFile = File(None),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if not description.strip():
            raise HTTPException(400, "Описание обязательно")

        photo_paths: list[str] = []
        if file and file.filename:
            raw = await file.read()
            if len(raw) > 8 * 1024 * 1024:
                raise HTTPException(400, "Фото слишком большое (макс. 8 МБ)")
            detected = deps.sniff_image(raw)
            if not detected:
                raise HTTPException(400, "Файл должен быть изображением")
            ext = deps.allowed_image_mime_ext[detected]
            photo_id = uuid.uuid4().hex
            filename = f"mangel_{photo_id}.{ext}"
            with open(os.path.join(deps.mangel_photo_dir(), filename), "wb") as output:
                output.write(raw)
            photo_paths.append(filename)

        mangel_lib = deps.load_mangel_lib()
        ticket = mangel_lib.create_ticket(
            object_id=object_id.strip()[:100],
            description=description.strip()[:500],
            created_by=str(user["id"]),
            photo_paths=photo_paths,
            assigned_worker_id=assigned_worker_id.strip() if role == "owner" else "",
        )

        # Google Sheets is a best-effort viewing mirror; JSON remains the source
        # of truth and a mirror outage must not fail defect creation.
        try:
            objects_lib = deps.load_objekte_lib()
            objects_lib.append_row_safe(
                "Дефекты",
                [
                    ticket.get("id", ""),
                    ticket.get("object_id", ""),
                    ticket.get("description", ""),
                    ticket.get("status", ""),
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                ],
            )
        except Exception as exc:
            logger.warning(
                "mangel create Sheets mirror failed for ticket %s: %s",
                ticket.get("id"), exc,
            )

        deps.append_object_history_best_effort(
            ticket.get("object_id", ""),
            "defect_created",
            "Создан дефект",
            user=user,
            subtitle=ticket.get("description", ""),
            meta={
                "ticket_id": ticket.get("id", ""),
                "assigned_worker_id": ticket.get("assigned_worker_id", ""),
                "photo_count": len(ticket.get("photo_paths") or []),
            },
        )
        return ticket

    @router.get("/api/mangel/photos/{fname}/file")
    def get_mangel_photo_file(
        fname: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        safe_name = os.path.basename(fname)
        if safe_name != fname or not safe_name.startswith("mangel_"):
            raise HTTPException(404, "Файл отсутствует")

        mangel_lib = deps.load_mangel_lib()
        if role != "owner":
            owning_ticket = next(
                (
                    ticket
                    for ticket in mangel_lib.list_tickets(None)
                    if safe_name in ticket.get("photo_paths", [])
                ),
                None,
            )
            if not owning_ticket or not deps.can_access_object(
                user, role, owning_ticket.get("object_id", "")
            ):
                raise HTTPException(403, "Нет доступа к этому файлу")

        path = os.path.join(deps.photo_dir(), safe_name)
        if not os.path.exists(path):
            raise HTTPException(404, "Файл отсутствует")
        return FileResponse(path)

    @router.patch("/api/mangel/{ticket_id}/status")
    def update_mangel_status(
        ticket_id: str,
        body: MangelStatusBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        mangel_lib = deps.load_mangel_lib()
        try:
            result = mangel_lib.update_status(ticket_id, body.status)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            objects_lib = deps.load_objekte_lib()
            rows = objects_lib.get_used_range("Дефекты")
            if rows:
                for row_number, row in enumerate(rows[1:], start=2):
                    if row and row[0] == ticket_id:
                        objects_lib.update_range(
                            f"Дефекты!D{row_number}:D{row_number}", [[body.status]]
                        )
                        break
        except Exception as exc:
            logger.warning(
                "mangel status Sheets mirror failed for ticket %s: %s",
                ticket_id, exc,
            )
        return result

    @router.post("/api/mangel/{ticket_id}/comments")
    def add_mangel_comment(
        ticket_id: str,
        body: MangelCommentBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(require_mangel_access),
    ):
        if not body.text.strip():
            raise HTTPException(400, "Текст комментария обязателен")
        try:
            return deps.load_mangel_lib().add_comment(
                ticket_id,
                str(user["id"]),
                body.text.strip()[:500],
                name=user.get("first_name", str(user["id"])),
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.delete("/api/mangel/{ticket_id}")
    def delete_mangel_ticket(
        ticket_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        try:
            deps.load_mangel_lib().soft_delete_ticket(ticket_id, str(user["id"]))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "id": ticket_id}

    @router.get("/api/mangel/{ticket_id}/comments")
    def get_mangel_comments(
        ticket_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(require_mangel_access),
    ):
        try:
            ticket = deps.load_mangel_lib().get_ticket(ticket_id)
            return {"comments": ticket.get("comments", [])}
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    return router, MangelHandlers(
        require_mangel_access=require_mangel_access,
        enrich_mangel_tickets_with_author=enrich_mangel_tickets_with_author,
        get_mangel_list=get_mangel_list,
        get_mangel_counts=get_mangel_counts,
        get_mangel_ticket=get_mangel_ticket,
        create_mangel_ticket=create_mangel_ticket,
        get_mangel_photo_file=get_mangel_photo_file,
        update_mangel_status=update_mangel_status,
        add_mangel_comment=add_mangel_comment,
        delete_mangel_ticket=delete_mangel_ticket,
        get_mangel_comments=get_mangel_comments,
    )
