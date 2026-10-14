"""Framework-agnostic read/access and pause operations for check-in sessions."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable


class CheckinAccessError(RuntimeError):
    """Expected application failure mapped to HTTP by the route adapter."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class CheckinAccessDeps:
    checkin_lock: object
    load_meta: Callable[[], list]
    save_meta: Callable[[list], None]
    photo_base: Callable[[], str]
    resolve_media_path: Callable[[str, str], str | None]
    now_timestamp: Callable[[], int]


@dataclass(frozen=True)
class CheckinAccessService:
    deps: CheckinAccessDeps

    def _session(self, session_id: str) -> tuple[list, dict | None]:
        raw_items = self.deps.load_meta()
        items = raw_items if isinstance(raw_items, list) else []
        session = next(
            (
                item
                for item in items
                if isinstance(item, dict) and item.get("id") == session_id
            ),
            None,
        )
        return items, session

    @staticmethod
    def _require_access(session: dict, user_id: str, role: str, detail: str) -> None:
        if role != "owner" and str(session.get("user_id")) != str(user_id):
            raise CheckinAccessError(403, detail)

    def get_accessible_session(
        self,
        session_id: str,
        user_id: str,
        role: str,
        *,
        forbidden_detail: str = "Нет доступа к этой смене",
    ) -> dict:
        _, session = self._session(session_id)
        if not session:
            raise CheckinAccessError(404, "Сессия не найдена")
        self._require_access(session, user_id, role, forbidden_detail)
        return session

    def pause_session(self, session_id: str, action: str, user_id: str, role: str) -> dict:
        action_clean = str(action or "").strip().lower()
        if action_clean not in {"pause", "resume"}:
            raise CheckinAccessError(400, "action должен быть pause или resume")

        with self.deps.checkin_lock:
            items, session = self._session(session_id)
            if not session:
                raise CheckinAccessError(404, "Сессия check-in не найдена")
            self._require_access(session, user_id, role, "Нельзя управлять чужой сменой")
            if session.get("manual_entry"):
                raise CheckinAccessError(400, "Ручная запись времени не поддерживает паузу")
            if session.get("finish_at") is not None:
                raise CheckinAccessError(400, "Смена уже завершена")

            now = int(self.deps.now_timestamp())
            accumulated = max(0, int(session.get("pause_accumulated_seconds") or 0))
            changed = False
            pause_started_at = session.get("pause_started_at")
            if action_clean == "resume" and pause_started_at:
                accumulated += max(0, now - int(pause_started_at))
                session["pause_accumulated_seconds"] = accumulated
                session["pause_started_at"] = None
                paused = False
                changed = True
            elif action_clean == "pause" and not pause_started_at:
                # Backfill the field for older sessions so the response never raises
                # KeyError after a successful state transition.
                session["pause_accumulated_seconds"] = accumulated
                session["pause_started_at"] = now
                paused = True
                changed = True
            else:
                paused = bool(pause_started_at)

            if changed:
                self.deps.save_meta(items)

        return {
            "action": action_clean,
            "changed": changed,
            "paused": paused,
            "pause_accumulated_seconds": accumulated,
            "pause_accumulated_minutes": round(accumulated / 60),
        }

    def list_sessions(self, user_id: str, role: str, object_id: str = "", date: str = "") -> dict:
        raw_items = self.deps.load_meta()
        source = raw_items if isinstance(raw_items, list) else []
        # Copy response records: decorating with an audio URL must never mutate a
        # cached/in-memory persistence object returned by a storage adapter.
        items = [dict(item) for item in source if isinstance(item, dict)]
        if role != "owner":
            items = [item for item in items if str(item.get("user_id")) == str(user_id)]
        if object_id:
            items = [item for item in items if item.get("object_id") == object_id]
        if date:
            items = [item for item in items if item.get("date") == date]
        for item in items:
            voice_note_file_id = item.get("voice_note_file_id")
            if voice_note_file_id:
                item["voice_note_audio_url"] = f"/api/transcribe/{voice_note_file_id}/audio"
        return {"sessions": items}

    def photo_path(self, session_id: str, which: str, index: int, user_id: str, role: str) -> str:
        session = self.get_accessible_session(
            session_id,
            user_id,
            role,
            forbidden_detail="Нет доступа к фото этой смены",
        )
        if which not in {"start", "finish"}:
            raise CheckinAccessError(404, "Фото не найдено")
        key = "start_photos" if which == "start" else "finish_photos"
        photos = session.get(key, [])
        if not isinstance(photos, list) or index < 0 or index >= len(photos):
            raise CheckinAccessError(404, "Фото не найдено")
        stored_path = photos[index]
        path = self.deps.resolve_media_path(self.deps.photo_base(), stored_path)
        if not path or not os.path.isfile(path):
            raise CheckinAccessError(404, "Файл отсутствует")
        return path
