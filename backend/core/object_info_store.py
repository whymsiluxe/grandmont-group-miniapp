"""Shared persistence helpers for the per-object info JSON document.

Documents and the Objects domain both own fields inside the same historical
OBJECT_INFO_FILE record. This store centralises that shared shape without making
either HTTP router depend on the other or on ``backend.main``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ObjectInfoStore:
    file_path: Callable[[], str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]

    @staticmethod
    def new_entry() -> dict:
        return {"items": [], "documents": [], "description": ""}

    @classmethod
    def ensure_entry(cls, data: dict, object_id: str) -> dict:
        # Preserve the legacy helper exactly: malformed existing non-dict entries
        # are not silently repaired by this refactor.
        entry = data.setdefault(object_id, cls.new_entry())
        entry.setdefault("items", [])
        entry.setdefault("documents", [])
        entry.setdefault("description", "")
        return entry

    def load(self) -> dict:
        return self.safe_load_json(self.file_path(), {})

    def save(self, data: dict) -> None:
        self.atomic_write_json(self.file_path(), data)

    def entry(self, object_id: str):
        data = self.load()
        return data.get(object_id, self.new_entry())
