"""Durable storage coordination for the chat domain.

This module deliberately has no FastAPI/Pydantic/routes/main dependency.  It
centralizes the cross-file ordering rules that make chat persistence replay-safe
when one JSON/file write succeeds and the next one fails.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatStorageDeps:
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    chat_file: Callable[[], str]
    chat_archive_file: Callable[[], str]
    chat_reactions_file: Callable[[], str]
    chat_attach_dir: Callable[[], str]
    chat_max: int
    chat_retention_seconds: int


class ChatStorageService:
    """Coordinates active chat, archive, reactions and attachment durability."""

    def __init__(self, deps: ChatStorageDeps):
        self.deps = deps
        self.lock = threading.Lock()
        os.makedirs(self.deps.chat_attach_dir(), exist_ok=True)

    def load_chat(self) -> list:
        return self.deps.safe_load_json(self.deps.chat_file(), [])

    def load_reactions(self) -> list:
        return self.deps.safe_load_json(self.deps.chat_reactions_file(), [])

    def save_reactions(self, reactions: list) -> None:
        self.deps.atomic_write_json(self.deps.chat_reactions_file(), reactions)

    def archive_messages(self, messages: list) -> None:
        """Append messages to archive idempotently by stable message id.

        Cross-file transactions are unavailable in the current JSON backend.
        Idempotent archive writes make retries safe when archive succeeds but a
        subsequent active-chat write fails.
        """
        if not messages:
            return
        archive = self.deps.safe_load_json(self.deps.chat_archive_file(), [])
        existing_ids = {
            str(item.get("id"))
            for item in archive
            if isinstance(item, dict) and item.get("id") is not None
        }
        changed = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if message_id is not None and str(message_id) in existing_ids:
                continue
            archive.append(message)
            changed = True
            if message_id is not None:
                existing_ids.add(str(message_id))
        if changed:
            self.deps.atomic_write_json(self.deps.chat_archive_file(), archive)

    def save_chat(self, messages: list) -> None:
        if len(messages) > self.deps.chat_max:
            overflow = messages[:-self.deps.chat_max]
            self.archive_messages(overflow)
            messages = messages[-self.deps.chat_max:]
        self.deps.atomic_write_json(self.deps.chat_file(), messages)

    def purge_old_chat(self, messages: list) -> list:
        cutoff = time.time() - self.deps.chat_retention_seconds
        keep = [message for message in messages if message.get("ts", 0) >= cutoff]
        expired = [message for message in messages if message.get("ts", 0) < cutoff]
        if expired:
            self.archive_messages(expired)
        return keep

    def append_message(self, message: dict) -> None:
        with self.lock:
            messages = self.load_chat()
            messages.append(message)
            self.save_chat(messages)

    def persist_attachment_and_message(
        self,
        *,
        filename: str,
        data: bytes,
        message: dict,
    ) -> None:
        """Persist attachment binary and its message without orphaning the file.

        The binary necessarily lands before the JSON message references it.  If
        the message write fails, remove the just-created binary and re-raise the
        original persistence failure.
        """
        path = os.path.join(self.deps.chat_attach_dir(), filename)
        with open(path, "wb") as output:
            output.write(data)
        try:
            self.append_message(message)
        except Exception:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "Failed to clean orphan chat attachment after message write failure",
                    extra={"filename": filename},
                )
            raise

    def delete_messages_by_ids(self, message_ids: set[str]) -> int:
        """Archive then remove active messages with retry-safe ordering.

        If archive succeeds and active save fails, the message remains active and
        retrying is safe because archive_messages is idempotent.  Reaction cleanup
        is secondary metadata, but its read-modify-write must share the same lock
        as reaction mutations so it cannot erase a concurrent reaction.
        """
        if not message_ids:
            return 0
        with self.lock:
            messages = self.load_chat()
            targets = [
                message
                for message in messages
                if str(message.get("id")) in message_ids
            ]
            if not targets:
                return 0
            self.archive_messages(targets)
            remaining = [
                message
                for message in messages
                if str(message.get("id")) not in message_ids
            ]
            self.save_chat(remaining)

            try:
                reactions = self.load_reactions()
                remaining_reactions = [
                    reaction
                    for reaction in reactions
                    if str(reaction.get("message_id")) not in message_ids
                ]
                if len(remaining_reactions) != len(reactions):
                    self.save_reactions(remaining_reactions)
            except Exception:
                # Message deletion is already durable.  Stale reaction rows are
                # secondary metadata and must not turn a completed delete into a
                # false 500/retry loop.
                logger.exception(
                    "Failed to clean chat reactions after durable message deletion",
                    extra={"message_ids": sorted(message_ids)},
                )
        return len(targets)
