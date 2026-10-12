"""Needs / Потребности domain extracted from ``backend.main``.

This module owns the /api/tasks request workflow (worker -> owner).  It is
intentionally separate from routes/tasks.py, which owns object-scoped project
work items under /api/objects/{object_id}/tasks.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Collection

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


class TaskCreateBody(BaseModel):
    title: str
    description: str = ""
    object_id: str = ""
    priority: str = "обычная"
    category: str = "other"
    due_at: int | None = None


class TaskStatusBody(BaseModel):
    status: str


@dataclass(frozen=True)
class NeedsRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    tasks_file: Callable[[], str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    task_priorities: Collection[str]
    task_categories: Collection[str]
    task_statuses: Collection[str]
    has_active_object_access: Callable[[str, str], bool]
    load_roles: Callable[[], dict]
    get_worker_profile: Callable[[Any], dict]
    sanitize_display_name: Callable[[Any, str], str]
    send_telegram_message: Callable[..., Any]
    business_now: Callable[[], Any]
    load_repo_objekte_lib: Callable[[], Any]


@dataclass(frozen=True)
class NeedsHandlers:
    load_tasks: Callable[[], list]
    save_tasks: Callable[[list], None]
    task_is_open: Callable[[dict], bool]
    normalize_task_due_at: Callable[[Any], int | None]
    format_due_at: Callable[[int], str]
    overdue_task_alerts: Callable[..., list]
    list_tasks: Callable[..., dict]
    create_task: Callable[..., dict]
    update_task_status: Callable[..., dict]


def create_needs_router(deps: NeedsRouteDeps) -> tuple[APIRouter, NeedsHandlers]:
    router = APIRouter()

    def load_tasks() -> list:
        return deps.safe_load_json(deps.tasks_file(), [])

    def save_tasks(items: list) -> None:
        deps.atomic_write_json(deps.tasks_file(), items)

    def task_is_open(task: dict) -> bool:
        return task.get("status") not in ("закрыто", "выдано", "отклонено", "cancelled")

    def normalize_task_due_at(raw_due_at) -> int | None:
        if raw_due_at in (None, ""):
            return None
        try:
            due_at = int(raw_due_at)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Некорректный срок задачи") from exc
        if due_at <= 0:
            raise HTTPException(400, "Некорректный срок задачи")
        if due_at < int(time.time()) - 60:
            raise HTTPException(400, "Срок задачи не может быть в прошлом")
        return due_at

    def format_due_at(due_at: int) -> str:
        try:
            return datetime.fromtimestamp(int(due_at), deps.business_now().tzinfo).strftime("%d.%m %H:%M")
        except Exception:
            return ""

    def overdue_task_alerts(now: int | None = None) -> list:
        now = int(time.time()) if now is None else int(now)
        result = []
        for task in load_tasks():
            if not task_is_open(task):
                continue
            try:
                due_at = int(task.get("due_at") or 0)
            except (TypeError, ValueError):
                continue
            if due_at <= 0 or due_at > now:
                continue
            title = (task.get("title") or "Потребность")[:80]
            parts = []
            if task.get("object_id"):
                parts.append(str(task.get("object_id")))
            due_label = format_due_at(due_at)
            if due_label:
                parts.append(f"срок {due_label}")
            if task.get("from_name") or task.get("from_user_id"):
                parts.append(f"запросил {task.get('from_name') or task.get('from_user_id')}")
            result.append({
                "id": f"task-overdue-{task.get('id', '')}",
                "type": "red",
                "role_filter": "owner",
                "title": f"Просрочена потребность: {title}",
                "subtitle": " · ".join(parts),
                "at": due_at,
                "task_id": task.get("id", ""),
                "task_overdue": True,
            })
        return result

    @router.get("/api/tasks")
    def list_tasks(
        object_id: str = "",
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if object_id and role != "owner" and not deps.has_active_object_access(str(user["id"]), object_id):
            raise HTTPException(403, "Нет доступа к этому объекту")

        items = load_tasks()
        if role != "owner" and not object_id:
            items = [task for task in items if str(task.get("from_user_id")) == str(user["id"])]
        if object_id:
            items = [task for task in items if task.get("object_id") == object_id]
        return {
            "tasks": sorted(
                items,
                key=lambda task: (task.get("priority") != "срочно", -task.get("created_at", 0)),
            )
        }

    @router.post("/api/tasks")
    def create_task(
        body: TaskCreateBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if role == "owner":
            raise HTTPException(403, "Потребности создают работники")
        if not body.title.strip():
            raise HTTPException(400, "Название обязательно")
        if not body.object_id.strip():
            raise HTTPException(400, "Объект обязателен")
        if not deps.has_active_object_access(str(user["id"]), body.object_id.strip()):
            raise HTTPException(403, "Нет доступа к этому объекту")

        priority = body.priority.strip() or "обычная"
        if priority not in deps.task_priorities:
            raise HTTPException(400, "Недопустимый приоритет")
        category = body.category.strip() or "other"
        if category not in deps.task_categories:
            raise HTTPException(400, "Недопустимая категория")
        due_at = normalize_task_due_at(body.due_at)

        roles = deps.load_roles()
        owner_id = next((uid for uid, role_name in roles.items() if role_name == "owner"), None)
        profile = deps.get_worker_profile(user["id"])
        task = {
            "id": uuid.uuid4().hex,
            "type": "request",
            "from_user_id": str(user["id"]),
            "from_name": deps.sanitize_display_name(profile.get("name"), str(user["id"])),
            "to_user_id": owner_id,
            "object_id": body.object_id.strip(),
            "title": body.title.strip()[:200],
            "description": body.description.strip()[:1000],
            "priority": priority,
            "category": category,
            "status": "открыто",
            "created_at": int(time.time()),
            "due_at": due_at,
            "closed_at": None,
        }

        def _mutate(items):
            items.append(task)
            return task

        deps.update_json_transaction(deps.tasks_file(), [], _mutate)
        if owner_id:
            try:
                urgent_prefix = "🔴 СРОЧНО! " if priority == "срочно" else "📋 "
                deps.send_telegram_message(
                    int(owner_id),
                    f"{urgent_prefix}Новая потребность от {task['from_name']}: {task['title']}",
                )
            except Exception:
                # Notification delivery is best-effort and never changes creation semantics.
                pass
        return task

    @router.patch("/api/tasks/{task_id}")
    def update_task_status(
        task_id: str,
        body: TaskStatusBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        if body.status not in deps.task_statuses:
            raise HTTPException(400, "Недопустимый статус")
        previous_status: dict[str, Any] = {}

        def _mutate(items):
            task = next((item for item in items if item["id"] == task_id), None)
            if not task:
                raise HTTPException(404, "Потребность не найдена")
            previous_status["value"] = task.get("status")
            task["status"] = body.status
            if body.status == "закрыто":
                task["closed_at"] = int(time.time())
            else:
                task["closed_at"] = None
            return task

        task = deps.update_json_transaction(deps.tasks_file(), [], _mutate)
        prev_status = previous_status["value"]

        if body.status == "закрыто" and prev_status != "закрыто":
            try:
                objects_lib = deps.load_repo_objekte_lib()
                created_str = (
                    datetime.fromtimestamp(task.get("created_at", 0)).strftime("%Y-%m-%d %H:%M")
                    if task.get("created_at")
                    else ""
                )
                closed_str = datetime.fromtimestamp(task["closed_at"]).strftime("%Y-%m-%d %H:%M")
                objects_lib.append_row_safe("Потребности", [
                    task.get("id", ""),
                    task.get("object_id", ""),
                    task.get("title", ""),
                    task.get("description", ""),
                    task.get("category", ""),
                    task.get("priority", ""),
                    task.get("from_name", task.get("from_user_id", "")),
                    created_str,
                    closed_str,
                ])
            except Exception as exc:
                print(f"WARNING: не удалось заархивировать потребность {task_id} в Sheets: {exc}")
        return task

    return router, NeedsHandlers(
        load_tasks=load_tasks,
        save_tasks=save_tasks,
        task_is_open=task_is_open,
        normalize_task_due_at=normalize_task_due_at,
        format_due_at=format_due_at,
        overdue_task_alerts=overdue_task_alerts,
        list_tasks=list_tasks,
        create_task=create_task,
        update_task_status=update_task_status,
    )
