"""Tools API routes.

This module owns the actual tools-domain HTTP/business flow that previously lived
inside ``backend/main.py``.  Runtime dependencies are injected by ``main`` so the
module remains importable both as ``miniapp.routes.tools`` in production and as
``routes.tools`` in the test harness, while legacy direct-call tests can keep using
aliases exported from ``main`` during the migration.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ToolBookingBody(BaseModel):
    date_from: str
    date_to: str
    object_name: str
    holder_id: str = ""
    holder: str = ""
    note: str = ""


class CheckoutBody(BaseModel):
    object_name: str
    # Kept for request compatibility only. Self-checkout never trusts a
    # client-supplied holder name; the authenticated Telegram user is canonical.
    holder: str = ""


class ToolUpdateBody(BaseModel):
    status: str
    holder: str = ""
    object_name: str = ""
    holder_id: str = ""


class NewToolBody(BaseModel):
    name: str
    category: str


@dataclass(frozen=True)
class ToolsRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    load_tools_lib: Callable[[], Any]
    load_worker_profiles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    business_today: Callable[[], Any]
    business_today_str: Callable[[], str]
    safe_load_json: Callable[[str, Any], Any]
    update_json_transaction: Callable[[str, Callable[[], Any], Callable[[Any], Any]], Any]
    tool_bookings_file: Callable[[], str]


def parse_tool_booking_date(value: str, field_name: str):
    raw = (value or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(400, f"{field_name} должен быть YYYY-MM-DD") from exc


def tool_booking_default_store():
    return {"bookings": []}


def tool_booking_overlaps(a_from: str, a_to: str, b_from: str, b_to: str) -> bool:
    return bool(a_from and a_to and b_from and b_to and a_from <= b_to and b_from <= a_to)


def holder_name_from_user(user: dict) -> str:
    """Resolve the canonical holder display name from the authenticated user."""
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    holder_name = " ".join(v for v in (first_name, last_name) if v).strip()
    if not holder_name:
        holder_name = (user.get("username") or "").strip()
    if not holder_name:
        holder_name = str(user["id"])
    return holder_name


_tool_locks: dict[str, threading.Lock] = {}
_tool_locks_guard = threading.Lock()
_tool_create_lock = threading.Lock()


def lock_for_tool(serial: str) -> threading.Lock:
    """Serialize a tool's read/check/write cycle across checkout and return."""
    with _tool_locks_guard:
        if serial not in _tool_locks:
            _tool_locks[serial] = threading.Lock()
        return _tool_locks[serial]


def create_tools_router(deps: ToolsRouteDeps):
    router = APIRouter()

    def _tool_booking_holder(body: ToolBookingBody, user: dict, role: str) -> tuple[str, str]:
        if role != "owner":
            return str(user["id"]), holder_name_from_user(user)
        holder_id = (body.holder_id or "").strip()
        if holder_id:
            profiles = deps.load_worker_profiles()
            holder_name = deps.sanitize_display_name(profiles.get(holder_id, {}).get("name"), holder_id)
            return holder_id, holder_name
        fallback = (body.holder or holder_name_from_user(user)).strip() or str(user["id"])
        return str(user["id"]), fallback

    def _active_tool_booking_conflict(serial: str, on_date: str, requester_id: str):
        data = deps.safe_load_json(deps.tool_bookings_file(), tool_booking_default_store())
        for booking in data.get("bookings", []):
            if booking.get("status") == "cancelled":
                continue
            if str(booking.get("serial", "")) != str(serial):
                continue
            date_from = booking.get("date_from", "")
            date_to = booking.get("date_to", "")
            if not (date_from and date_to and date_from <= on_date <= date_to):
                continue
            if str(booking.get("holder_id", "")) == str(requester_id):
                continue
            return booking
        return None

    @router.get("/api/tools")
    def list_tools(user: dict = Depends(deps.get_current_user)):
        tools_lib = deps.load_tools_lib()
        try:
            return {"tools": tools_lib.list_tools()}
        except Exception as exc:
            # The tools sheet is an external dependency. Its temporary outage must
            # not break Mini App bootstrap or unrelated screens.
            logger.warning("tools list unavailable: %s: %s", type(exc).__name__, exc)
            return {"tools": [], "warning": "tools_unavailable"}

    @router.get("/api/tools/{serial}/history")
    def tool_history(serial: str, user: dict = Depends(deps.get_current_user)):
        tools_lib = deps.load_tools_lib()
        return {"history": tools_lib.tool_history(serial)}

    @router.get("/api/tools/bookings")
    def list_tool_bookings(
        serial: str = "",
        date_from: str = "",
        date_to: str = "",
        user: dict = Depends(deps.get_current_user),
    ):
        data = deps.safe_load_json(deps.tool_bookings_file(), tool_booking_default_store())
        today = deps.business_today_str()
        start = (date_from or today).strip()
        end = (date_to or start).strip()
        start_date = parse_tool_booking_date(start, "date_from")
        end_date = parse_tool_booking_date(end, "date_to")
        if end_date < start_date:
            raise HTTPException(400, "date_to раньше date_from")
        serial_filter = (serial or "").strip()
        bookings = []
        for booking in data.get("bookings", []):
            if booking.get("status") == "cancelled":
                continue
            if serial_filter and str(booking.get("serial", "")) != serial_filter:
                continue
            if not tool_booking_overlaps(
                start,
                end,
                booking.get("date_from", ""),
                booking.get("date_to", ""),
            ):
                continue
            bookings.append(booking)
        bookings.sort(key=lambda item: (item.get("date_from", ""), item.get("tool_name", ""), item.get("serial", "")))
        return {"bookings": bookings}

    @router.post("/api/tools/{serial}/bookings")
    def create_tool_booking(
        serial: str,
        body: ToolBookingBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        date_from = (body.date_from or "").strip()
        date_to = (body.date_to or "").strip()
        start_date = parse_tool_booking_date(date_from, "date_from")
        end_date = parse_tool_booking_date(date_to, "date_to")
        if end_date < start_date:
            raise HTTPException(400, "date_to раньше date_from")
        if start_date < deps.business_today():
            raise HTTPException(400, "Нельзя бронировать инструмент в прошлом")

        object_name = (body.object_name or "").strip()
        if not object_name:
            raise HTTPException(400, "Укажи объект")

        tools_lib = deps.load_tools_lib()
        tool = tools_lib.get_tool(serial)
        if tool is None:
            raise HTTPException(404, f"инструмент {serial} не найден")
        status = tools_lib.mapped_status(tool)
        if status in ("repair", "missing"):
            raise HTTPException(409, "Инструмент недоступен для брони")
        if start_date == deps.business_today() and status in ("in-use", "reserved"):
            raise HTTPException(409, "Инструмент сейчас занят -- бронь на сегодня недоступна")

        holder_id, holder_name = _tool_booking_holder(body, user, role)
        booking_id = uuid.uuid4().hex
        now = int(time.time())

        def _mutator(data):
            data.setdefault("bookings", [])
            for existing in data["bookings"]:
                if existing.get("status") == "cancelled":
                    continue
                if str(existing.get("serial", "")) != str(serial):
                    continue
                if tool_booking_overlaps(
                    date_from,
                    date_to,
                    existing.get("date_from", ""),
                    existing.get("date_to", ""),
                ):
                    raise HTTPException(409, "Инструмент уже забронирован на эти даты")
            booking = {
                "id": booking_id,
                "serial": str(serial),
                "tool_name": tool.get("Название Инструмента", ""),
                "category": tool.get("Категория", ""),
                "date_from": date_from,
                "date_to": date_to,
                "object_name": object_name,
                "holder_id": holder_id,
                "holder_name": holder_name,
                "note": (body.note or "").strip()[:300],
                "status": "active",
                "created_at": now,
                "created_by": str(user["id"]),
            }
            data["bookings"].append(booking)
            return booking

        booking = deps.update_json_transaction(
            deps.tool_bookings_file(),
            tool_booking_default_store,
            _mutator,
        )
        return {"booking": booking}

    @router.delete("/api/tools/{serial}/bookings/{booking_id}")
    def cancel_tool_booking(
        serial: str,
        booking_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        user_id = str(user["id"])

        def _mutator(data):
            for booking in data.get("bookings", []):
                if booking.get("id") != booking_id or str(booking.get("serial", "")) != str(serial):
                    continue
                if booking.get("status") == "cancelled":
                    return booking
                if role != "owner" and booking.get("holder_id") != user_id and booking.get("created_by") != user_id:
                    raise HTTPException(403, "Можно удалить только свою бронь")
                booking["status"] = "cancelled"
                booking["cancelled_at"] = int(time.time())
                booking["cancelled_by"] = user_id
                return booking
            raise HTTPException(404, "Бронь не найдена")

        booking = deps.update_json_transaction(
            deps.tool_bookings_file(),
            tool_booking_default_store,
            _mutator,
        )
        return {"booking": booking}

    @router.patch("/api/tools/{serial}/checkout")
    def checkout_tool(serial: str, body: CheckoutBody, user: dict = Depends(deps.get_current_user)):
        tools_lib = deps.load_tools_lib()
        if not body.object_name.strip():
            raise HTTPException(400, "Укажи объект")
        with lock_for_tool(serial):
            tool = tools_lib.get_tool(serial)
            if tool is None:
                raise HTTPException(404, f"инструмент {serial} не найден")
            if tools_lib.mapped_status(tool) != "free":
                raise HTTPException(409, "Инструмент уже выдан или недоступен")
            conflict = _active_tool_booking_conflict(serial, deps.business_today_str(), str(user["id"]))
            if conflict is not None:
                raise HTTPException(
                    409,
                    "Инструмент забронирован на сегодня: "
                    f"{conflict.get('holder_name', '')} ({conflict.get('object_name', '')})",
                )
            holder_name = holder_name_from_user(user)
            tools_lib.checkout_tool(
                serial,
                holder_name,
                body.object_name,
                holder_name,
                holder_id=str(user["id"]),
            )
        return {"status": "ok"}

    @router.patch("/api/tools/{serial}/return")
    def return_tool(
        serial: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        tools_lib = deps.load_tools_lib()
        with lock_for_tool(serial):
            tool = tools_lib.get_tool(serial)
            if tool is None:
                raise HTTPException(404, f"инструмент {serial} не найден")
            holder_id = (tool.get("ID держателя") or "").strip()
            if role != "owner" and str(user["id"]) != holder_id:
                raise HTTPException(403, "Можно вернуть только инструмент, который взят вами")
            tools_lib.return_tool(serial, user.get("first_name", str(user["id"])))
        return {"status": "ok"}

    @router.patch("/api/tools/{serial}")
    def update_tool(
        serial: str,
        body: ToolUpdateBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        tools_lib = deps.load_tools_lib()
        try:
            tools_lib.update_tool_status(
                serial,
                body.status,
                body.holder,
                body.object_name,
                user.get("first_name", str(user["id"])),
                holder_id=body.holder_id,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"status": "ok"}

    @router.post("/api/tools")
    def create_tool(
        body: NewToolBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        tools_lib = deps.load_tools_lib()
        # Serial allocation in tools_lib is read-max-write, so creation is
        # serialized across the entire operation to prevent duplicate serials.
        with _tool_create_lock:
            serial = tools_lib.add_tool(body.name, body.category, user.get("first_name", str(user["id"])))
        return {"serial": serial}

    handlers = SimpleNamespace(
        list_tools=list_tools,
        tool_history=tool_history,
        list_tool_bookings=list_tool_bookings,
        create_tool_booking=create_tool_booking,
        cancel_tool_booking=cancel_tool_booking,
        checkout_tool=checkout_tool,
        return_tool=return_tool,
        update_tool=update_tool,
        create_tool=create_tool,
        active_tool_booking_conflict=_active_tool_booking_conflict,
    )
    return router, handlers
