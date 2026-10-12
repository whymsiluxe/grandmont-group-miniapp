"""Task routes extracted from ``backend.main``.

The module owns the existing three task endpoints while runtime authentication,
object access and repository loading are injected by the composition root.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


class TaskBody(BaseModel):
    text: str


@dataclass(frozen=True)
class TasksRouteDeps:
    get_current_user: Callable[..., dict]
    require_owner: Callable[..., None]
    require_object_access: Callable[..., None]
    load_objekte_lib: Callable[[], Any]


@dataclass(frozen=True)
class TasksHandlers:
    get_tasks: Callable[..., dict]
    create_task: Callable[..., dict]
    complete_task: Callable[..., dict]


def create_tasks_router(deps: TasksRouteDeps) -> tuple[APIRouter, TasksHandlers]:
    """Build one isolated router for object tasks."""
    router = APIRouter()

    @router.get("/api/objects/{object_id}/tasks")
    def get_tasks(
        object_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_object_access),
    ):
        objects_lib = deps.load_objekte_lib()
        return {"tasks": objects_lib.list_tasks(object_id)}

    @router.post("/api/objects/{object_id}/tasks")
    def create_task(
        object_id: str,
        body: TaskBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        objects_lib = deps.load_objekte_lib()
        if not body.text.strip():
            raise HTTPException(400, "Текст не может быть пустым")
        task_id = objects_lib.add_task(
            object_id,
            body.text.strip(),
            user.get("first_name", str(user["id"])),
        )
        return {"task_id": task_id}

    @router.patch("/api/tasks/{task_id}/complete")
    def complete_task(
        task_id: str,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        objects_lib = deps.load_objekte_lib()
        try:
            objects_lib.complete_task(task_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"status": "ok"}

    return router, TasksHandlers(
        get_tasks=get_tasks,
        create_task=create_task,
        complete_task=complete_task,
    )
