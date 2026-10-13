from __future__ import annotations

import ast
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(__file__))

from chat_domain_testkit import ChatDomainHarness  # noqa: E402
from services.chat_storage import ChatStorageDeps, ChatStorageService  # noqa: E402


def _safe_load(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def _atomic_write(path: str, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.replace(tmp, path)


def make_service(tmp_path, *, writer=_atomic_write, chat_max=100):
    chat_file = tmp_path / "chat.json"
    archive_file = tmp_path / "archive.json"
    reactions_file = tmp_path / "reactions.json"
    attach_dir = tmp_path / "attachments"
    service = ChatStorageService(
        ChatStorageDeps(
            safe_load_json=_safe_load,
            atomic_write_json=writer,
            chat_file=lambda: str(chat_file),
            chat_archive_file=lambda: str(archive_file),
            chat_reactions_file=lambda: str(reactions_file),
            chat_attach_dir=lambda: str(attach_dir),
            chat_max=chat_max,
            chat_retention_seconds=90 * 24 * 3600,
        )
    )
    return service, chat_file, archive_file, reactions_file, attach_dir


def test_archive_replay_is_idempotent_by_message_id(tmp_path):
    service, _, archive_file, _, _ = make_service(tmp_path)
    message = {"id": "m1", "ts": 1, "text": "hello"}

    service.archive_messages([message])
    service.archive_messages([message])

    assert _safe_load(str(archive_file), []) == [message]


def test_attachment_is_removed_when_message_persistence_fails(tmp_path):
    service, chat_file, _, _, attach_dir = make_service(tmp_path)

    def fail_chat_write(path, value):
        if path == str(chat_file):
            raise OSError("chat store unavailable")
        _atomic_write(path, value)

    service, _, _, _, attach_dir = make_service(tmp_path, writer=fail_chat_write)

    with pytest.raises(OSError, match="chat store unavailable"):
        service.persist_attachment_and_message(
            filename="server-generated.bin",
            data=b"payload",
            message={"id": "m1", "ts": 1},
        )

    assert not (attach_dir / "server-generated.bin").exists()


def test_delete_retry_after_active_write_failure_does_not_duplicate_archive(tmp_path):
    calls = {"chat": 0}
    chat_path = tmp_path / "chat.json"

    def fail_first_chat_write(path, value):
        if path == str(chat_path):
            calls["chat"] += 1
            if calls["chat"] == 1:
                raise OSError("active write failed")
        _atomic_write(path, value)

    service, chat_file, archive_file, _, _ = make_service(
        tmp_path, writer=fail_first_chat_write
    )
    _atomic_write(str(chat_file), [{"id": "m1", "ts": 1}, {"id": "m2", "ts": 2}])

    with pytest.raises(OSError, match="active write failed"):
        service.delete_messages_by_ids({"m1"})

    # Archive succeeded first, while the active store still safely contains m1.
    assert [row["id"] for row in _safe_load(str(archive_file), [])] == ["m1"]
    assert [row["id"] for row in _safe_load(str(chat_file), [])] == ["m1", "m2"]

    assert service.delete_messages_by_ids({"m1"}) == 1
    assert [row["id"] for row in _safe_load(str(archive_file), [])] == ["m1"]
    assert [row["id"] for row in _safe_load(str(chat_file), [])] == ["m2"]


def test_reaction_cleanup_failure_does_not_false_500_after_delete(tmp_path):
    reactions_path = tmp_path / "reactions.json"

    def fail_reaction_write(path, value):
        if path == str(reactions_path):
            raise OSError("reaction cleanup failed")
        _atomic_write(path, value)

    service, chat_file, archive_file, reactions_file, _ = make_service(
        tmp_path, writer=fail_reaction_write
    )
    _atomic_write(str(chat_file), [{"id": "m1", "ts": 1}])
    _atomic_write(
        str(reactions_file),
        [{"message_id": "m1", "user_id": "u1", "reaction": "👍"}],
    )

    assert service.delete_messages_by_ids({"m1"}) == 1
    assert _safe_load(str(chat_file), []) == []
    assert [row["id"] for row in _safe_load(str(archive_file), [])] == ["m1"]
    # Stale reaction metadata may remain for a later cleanup, but durable delete
    # is not misreported as a failed request.
    assert _safe_load(str(reactions_file), [])


def test_chat_storage_service_has_no_framework_or_route_back_edge():
    path = Path(__file__).resolve().parents[1] / "backend" / "services" / "chat_storage.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_roots = {"fastapi", "pydantic"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(name.name.split(".")[0] not in forbidden_roots for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module.split(".")[0] not in forbidden_roots
            assert "routes" not in module.split(".")
            assert module not in {"main", "backend.main"}


def test_legacy_storage_handlers_remain_route_local_service_adapters():
    harness = ChatDomainHarness()
    try:
        for name in (
            "archive_chat_messages",
            "load_chat",
            "save_chat",
            "purge_old_chat",
        ):
            module = inspect.getmodule(getattr(harness.core, name))
            assert module is not None
            assert module.__name__.endswith("routes.chat")
    finally:
        harness.close()


def test_close_and_reopen_thread_meta_rmw_are_serialized_by_chat_lock():
    path = Path(__file__).resolve().parents[1] / "backend" / "routes" / "chat_actions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_chat_actions_router"
    )
    functions = {
        node.name: node
        for node in factory.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("close_chat_thread", "reopen_chat_thread"):
        handler = functions[name]
        lock_with = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Attribute)
                and isinstance(item.context_expr.value, ast.Name)
                and item.context_expr.value.id == "core"
                and item.context_expr.attr == "chat_lock"
                for item in node.items
            )
        ]
        assert lock_with, f"{name} must serialize thread-meta read-modify-write"
