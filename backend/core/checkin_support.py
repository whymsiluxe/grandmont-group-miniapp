"""Check-in persistence/photo/access support used by HTTP routes."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException


@dataclass(frozen=True)
class CheckinSupportDeps:
    meta_file: Callable[[], str]
    photo_base: Callable[[], str]
    max_bytes: int
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    sniff_image: Callable[[bytes], str | None]
    allowed_image_mime_ext: dict
    load_assignments: Callable[[], dict]
    assignment_status: Callable[[dict], str]


class CheckinSupportRuntime:
    def __init__(self, deps: CheckinSupportDeps):
        self.deps = deps

    def load_meta(self) -> list:
        return self.deps.safe_load_json(self.deps.meta_file(), [])

    def save_meta(self, items: list) -> None:
        self.deps.atomic_write_json(self.deps.meta_file(), items)

    async def save_photos(self, files: list, object_id: str, date_str: str) -> list:
        object_id = os.path.basename(object_id) or 'unknown'
        day_dir = os.path.join(self.deps.photo_base(), object_id, date_str)
        os.makedirs(day_dir, exist_ok=True)
        saved = []
        for file in files:
            raw = await file.read()
            if len(raw) > self.deps.max_bytes:
                continue
            detected = self.deps.sniff_image(raw)
            if not detected:
                continue
            ext = self.deps.allowed_image_mime_ext[detected]
            fname = f'{uuid.uuid4().hex}.{ext}'
            with open(os.path.join(day_dir, fname), 'wb') as output:
                output.write(raw)
            saved.append(os.path.join(object_id, date_str, fname))
        return saved

    @staticmethod
    def file_count(files) -> int:
        try:
            return len(files or [])
        except TypeError:
            return 0

    def cleanup_photo_files(self, relative_paths: list) -> None:
        for rel_path in relative_paths:
            try:
                os.remove(os.path.join(self.deps.photo_base(), rel_path))
            except OSError:
                pass

    def get_active_assignment(self, user_id: str, object_id: str, today: str) -> str:
        candidates = [
            assignment
            for assignment in self.deps.load_assignments().get(object_id, [])
            if str(assignment.get('user_id')) == str(user_id)
        ]
        if not candidates:
            raise HTTPException(403, 'У вас нет принятого назначения на этот объект')

        accepted = [
            assignment for assignment in candidates
            if self.deps.assignment_status(assignment) == 'accepted'
        ]
        for assignment in accepted:
            date_from, date_to = assignment.get('date_from', ''), assignment.get('date_to', '')
            if not (date_from and date_to):
                return assignment.get('id', '')
            if date_from <= today <= date_to:
                return assignment.get('id', '')
        if accepted:
            assignment = accepted[0]
            date_from, date_to = assignment.get('date_from', ''), assignment.get('date_to', '')
            if today < date_from:
                raise HTTPException(403, f'Смена доступна с {date_from}')
            raise HTTPException(403, f'Период назначения завершён {date_to}')

        if any(self.deps.assignment_status(a) == 'pending' for a in candidates):
            raise HTTPException(403, 'Сначала подтвердите назначение')
        if any(self.deps.assignment_status(a) == 'declined' for a in candidates):
            raise HTTPException(403, 'Назначение отклонено')
        raise HTTPException(403, 'У вас нет принятого назначения на этот объект')
