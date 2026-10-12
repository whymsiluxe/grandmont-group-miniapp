"""Absence (Abwesenheit) API domain.

The complete absence HTTP and persistence flow lives here. ``backend.main`` is
only the composition root: it injects runtime collaborators, includes the router,
and temporarily re-exports handler aliases for legacy direct-call callers/tests.
"""
from __future__ import annotations

import calendar
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Collection

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


class AbwesenheitBody(BaseModel):
    date_from: str
    date_to: str | None = None
    reason: str
    note: str = ""
    start_time: str | None = None
    end_time: str | None = None


class AbwesenheitStatusBody(BaseModel):
    status: str


class AbwesenheitMoveBody(BaseModel):
    date_from: str
    date_to: str | None = None


@dataclass(frozen=True)
class AbsenceRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    absence_file: Callable[[], str]
    reasons: Collection[str]
    public_fields: Collection[str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    load_roles: Callable[[], dict]
    send_telegram_message: Callable[..., Any]
    business_today_str: Callable[[], str]
    shadow_create_absence: Callable[..., Any]
    create_critical_alert: Callable[..., Any]
    resolve_current_display_name: Callable[[Any, Any], str]


@dataclass(frozen=True)
class AbsenceHandlers:
    load_abwesenheit: Callable[[], list]
    save_abwesenheit: Callable[[list], None]
    migrate_abwesenheit_legacy_ids: Callable[[], int]
    notify_owner_abwesenheit_pending: Callable[[dict], None]
    notify_worker_abwesenheit_decision: Callable[[dict], None]
    validate_date_str: Callable[..., None]
    month_end: Callable[[str], str]
    create_abwesenheit: Callable[..., dict]
    close_abwesenheit: Callable[..., dict]
    update_abwesenheit_status: Callable[..., dict]
    update_abwesenheit_dates: Callable[..., dict]
    auto_close_expired_open_ended_abwesenheit: Callable[[], None]
    list_my_abwesenheit: Callable[..., dict]
    list_all_abwesenheit: Callable[..., dict]
    delete_abwesenheit: Callable[..., dict]


def validate_date_str(date_str: str, field_name: str = "дата") -> None:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            400,
            f"Некорректный формат {field_name}: {date_str!r} (ожидается YYYY-MM-DD)",
        ) from exc


def month_end(date_str: str) -> str:
    try:
        year, month, _ = (int(value) for value in date_str.split("-"))
        if not 1 <= month <= 12:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, f"Некорректная дата: {date_str}") from exc
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def create_absence_router(deps: AbsenceRouteDeps) -> tuple[APIRouter, AbsenceHandlers]:
    """Build one fully isolated absence router instance.

    All stateful collaborators are captured by this factory. No module-global
    runtime dependency container exists, so creating another app/router cannot
    overwrite the dependencies used by an already configured instance.
    """
    router = APIRouter()

    def load_abwesenheit() -> list:
        return deps.safe_load_json(deps.absence_file(), [])

    def save_abwesenheit(items: list) -> None:
        """Replace the complete store; intended for fixtures/migrations, not RMW."""
        deps.atomic_write_json(deps.absence_file(), items)

    def migrate_abwesenheit_legacy_ids() -> int:
        def _mutate(items):
            count = 0
            if not isinstance(items, list):
                return count
            for entry in items:
                if isinstance(entry, dict) and not entry.get("id"):
                    entry["id"] = uuid.uuid4().hex
                    count += 1
            return count

        return deps.update_json_transaction(deps.absence_file(), [], _mutate)

    def notify_owner_abwesenheit_pending(entry: dict) -> None:
        roles = deps.load_roles()
        owner_id = next((uid for uid, role in roles.items() if role == "owner"), None)
        if not owner_id:
            return
        text = (
            f"🔴 Заявка на отсутствие: {entry['name']}\n"
            f"{entry['date_from']} — {entry['date_to']} · {entry['reason']}\n"
            f"{entry['note'] or ''}\n\nПодтвердите или отклоните в приложении."
        )
        try:
            deps.send_telegram_message(owner_id, text)
        except Exception:
            # Notification delivery is deliberately best-effort.
            pass

    def notify_worker_abwesenheit_decision(entry: dict) -> None:
        status_text = "одобрена" if entry["status"] == "approved" else "не одобрена"
        text = (
            f"{'✅' if entry['status'] == 'approved' else '❌'} Ваша заявка на отсутствие "
            f"{status_text} руководством\n{entry['date_from']} — {entry['date_to']}"
        )
        try:
            deps.send_telegram_message(int(entry["user_id"]), text)
        except Exception:
            # Notification delivery is deliberately best-effort.
            pass

    @router.post("/api/abwesenheit")
    def create_abwesenheit(
        body: AbwesenheitBody,
        user: dict = Depends(deps.get_current_user),
    ):
        if body.reason not in deps.reasons:
            raise HTTPException(400, f"Недопустимая причина: {body.reason}")
        validate_date_str(body.date_from, "date_from")
        if body.date_to:
            validate_date_str(body.date_to, "date_to")
            if body.date_to < body.date_from:
                raise HTTPException(400, "date_to не может быть раньше date_from")

        open_ended = not body.date_to
        date_to = body.date_to or month_end(body.date_from)
        entry = {
            "id": uuid.uuid4().hex,
            "user_id": str(user["id"]),
            "name": user.get("first_name", str(user["id"])),
            "date_from": body.date_from,
            "date_to": date_to,
            "open_ended": open_ended,
            "reason": body.reason,
            "note": body.note.strip()[:300],
            "start_time": body.start_time,
            "end_time": body.end_time,
            "created_at": int(time.time()),
            "status": "pending",
        }
        deps.update_json_transaction(
            deps.absence_file(), [], lambda items: items.append(entry)
        )
        notify_owner_abwesenheit_pending(entry)
        try:
            deps.shadow_create_absence(user["id"], entry)
        except Exception:
            # Core shadow synchronization must never change the legacy response.
            pass
        return entry

    @router.patch("/api/abwesenheit/{entry_id}/close")
    def close_abwesenheit(
        entry_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        def _mutate(items):
            entry = next((item for item in items if item.get("id") == entry_id), None)
            if not entry:
                raise HTTPException(404, "Запись не найдена")
            if entry["user_id"] != str(user["id"]) and role != "owner":
                raise HTTPException(403, "Можно закрывать только свои записи")
            entry["date_to"] = deps.business_today_str()
            entry["open_ended"] = False
            return dict(entry)

        return deps.update_json_transaction(deps.absence_file(), [], _mutate)

    @router.patch("/api/abwesenheit/{entry_id}/status")
    def update_abwesenheit_status(
        entry_id: str,
        body: AbwesenheitStatusBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        if body.status not in ("approved", "rejected"):
            raise HTTPException(400, "status должен быть approved или rejected")

        def _mutate(items):
            entry = next((item for item in items if item.get("id") == entry_id), None)
            if not entry:
                raise HTTPException(404, "Запись не найдена")
            entry["status"] = body.status
            return dict(entry)

        entry = deps.update_json_transaction(deps.absence_file(), [], _mutate)
        notify_worker_abwesenheit_decision(entry)
        deps.create_critical_alert(
            target_user_id=entry["user_id"],
            kind="abwesenheit_decision",
            title=(
                f"Отсутствие {entry['date_from']}—{entry['date_to']}: "
                f"{'одобрено' if body.status == 'approved' else 'не одобрено'}"
            ),
            ref_id=entry.get("id", ""),
        )
        return entry

    @router.patch("/api/abwesenheit/{entry_id}")
    def update_abwesenheit_dates(
        entry_id: str,
        body: AbwesenheitMoveBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        validate_date_str(body.date_from, "date_from")

        def _mutate(items):
            entry = next((item for item in items if item.get("id") == entry_id), None)
            if not entry:
                raise HTTPException(404, "Запись не найдена")
            if entry["user_id"] != str(user["id"]) and role != "owner":
                raise HTTPException(403, "Можно переносить только свои записи")

            if body.date_to:
                validate_date_str(body.date_to, "date_to")
                date_to = body.date_to
            elif entry.get("open_ended"):
                date_to = month_end(body.date_from)
            else:
                original_span_days = (
                    datetime.strptime(entry["date_to"], "%Y-%m-%d").date()
                    - datetime.strptime(entry["date_from"], "%Y-%m-%d").date()
                ).days
                original_span_days = max(0, original_span_days)
                new_from_date = datetime.strptime(body.date_from, "%Y-%m-%d").date()
                date_to = (
                    new_from_date + timedelta(days=original_span_days)
                ).isoformat()

            if date_to < body.date_from:
                raise HTTPException(400, "date_to не может быть раньше date_from")

            entry["date_from"] = body.date_from
            entry["date_to"] = date_to
            entry["updated_at"] = int(time.time())
            return dict(entry)

        return deps.update_json_transaction(deps.absence_file(), [], _mutate)

    def auto_close_expired_open_ended_abwesenheit() -> None:
        today_str = deps.business_today_str()

        def _mutate(items):
            expired_local = [
                item
                for item in items
                if item.get("open_ended") and item["date_to"] < today_str
            ]
            for entry in expired_local:
                entry["open_ended"] = False
            return [dict(entry) for entry in expired_local]

        expired = deps.update_json_transaction(deps.absence_file(), [], _mutate)
        if not expired:
            return

        roles = deps.load_roles()
        owner_ids = [uid for uid, role in roles.items() if role == "owner"]
        for entry in expired:
            text = (
                "⏰ Заявка на отсутствие автоматически закрыта (истёк месяц)\n"
                f"{entry['date_from']} — {entry['date_to']} · "
                f"{entry.get('name', entry['user_id'])}"
            )
            try:
                deps.send_telegram_message(int(entry["user_id"]), text)
            except Exception:
                pass
            for owner_id in owner_ids:
                try:
                    deps.send_telegram_message(owner_id, text)
                except Exception:
                    pass

    @router.get("/api/abwesenheit")
    def list_my_abwesenheit(user: dict = Depends(deps.get_current_user)):
        auto_close_expired_open_ended_abwesenheit()
        items = [
            item
            for item in load_abwesenheit()
            if item["user_id"] == str(user["id"])
        ]
        for entry in items:
            entry.setdefault("status", "pending")
            entry["name"] = deps.resolve_current_display_name(
                entry.get("user_id"), entry.get("name")
            )
        return {"entries": items}

    @router.get("/api/abwesenheit/all")
    def list_all_abwesenheit(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        auto_close_expired_open_ended_abwesenheit()
        my_id = str(user["id"])
        result = []
        for entry in load_abwesenheit():
            entry.setdefault("status", "pending")
            entry["name"] = deps.resolve_current_display_name(
                entry.get("user_id"), entry.get("name")
            )
            if role == "owner" or entry.get("user_id") == my_id:
                result.append(entry)
            else:
                result.append({key: entry.get(key) for key in deps.public_fields})
        return {"entries": result}

    @router.delete("/api/abwesenheit/{entry_id}")
    def delete_abwesenheit(
        entry_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        def _mutate(items):
            entry = next((item for item in items if item.get("id") == entry_id), None)
            if not entry:
                raise HTTPException(404, "Запись не найдена")
            if entry["user_id"] != str(user["id"]) and role != "owner":
                raise HTTPException(403, "Можно удалять только свои записи")
            items[:] = [item for item in items if item.get("id") != entry_id]

        deps.update_json_transaction(deps.absence_file(), [], _mutate)
        return {"status": "ok"}

    return router, AbsenceHandlers(
        load_abwesenheit=load_abwesenheit,
        save_abwesenheit=save_abwesenheit,
        migrate_abwesenheit_legacy_ids=migrate_abwesenheit_legacy_ids,
        notify_owner_abwesenheit_pending=notify_owner_abwesenheit_pending,
        notify_worker_abwesenheit_decision=notify_worker_abwesenheit_decision,
        validate_date_str=validate_date_str,
        month_end=month_end,
        create_abwesenheit=create_abwesenheit,
        close_abwesenheit=close_abwesenheit,
        update_abwesenheit_status=update_abwesenheit_status,
        update_abwesenheit_dates=update_abwesenheit_dates,
        auto_close_expired_open_ended_abwesenheit=auto_close_expired_open_ended_abwesenheit,
        list_my_abwesenheit=list_my_abwesenheit,
        list_all_abwesenheit=list_all_abwesenheit,
        delete_abwesenheit=delete_abwesenheit,
    )
