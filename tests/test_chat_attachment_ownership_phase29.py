"""Phase 29 regressions for chat attachment ownership and path safety."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.chat_actions import ChatActionsRouteDeps, create_chat_actions_router  # noqa: E402


def _build_handlers(tmp_path, *, live_messages=None, archived_messages=None):
    attach_dir = tmp_path / 'chat-attachments'
    attach_dir.mkdir()
    live_messages = list(live_messages or [])
    archived_messages = list(archived_messages or [])

    core = SimpleNamespace(
        load_chat=lambda: list(live_messages),
        chat_thread_id=lambda user_id, to_user_id: '-'.join(
            sorted([str(user_id), str(to_user_id)])
        ) if to_user_id else 'group',
        check_thread_access=lambda _thread_key, _uid, _role: None,
    )

    deps = ChatActionsRouteDeps(
        get_current_user=lambda: {'id': '1'},
        get_role=lambda: 'owner',
        require_owner=lambda: None,
        core=core,
        safe_load_json=lambda _path, default: list(archived_messages) if isinstance(default, list) else default,
        chat_archive_file=lambda: str(tmp_path / 'chat-archive.json'),
        chat_attach_dir=lambda: str(attach_dir),
        chat_reaction_options=('like',),
        load_roles=lambda: {'1': 'owner', '42': 'worker'},
        send_telegram_message=lambda *_args, **_kwargs: None,
        find_object_row_by_id=lambda _object_id: None,
        append_object_history_best_effort=lambda *_args, **_kwargs: None,
    )
    _router, handlers = create_chat_actions_router(deps)
    return handlers, attach_dir


def _attachment_message(filename: str, *, user_id='42', to_user_id='1', thread_key=None):
    return {
        'id': 'msg-1',
        'user_id': user_id,
        'to_user_id': to_user_id,
        'thread_key': thread_key,
        'attachment': {'file': filename, 'name': filename, 'content_type': 'image/jpeg'},
    }


def test_owner_cannot_fetch_orphan_attachment_by_filename(tmp_path):
    handlers, attach_dir = _build_handlers(tmp_path)
    (attach_dir / 'orphan.jpg').write_bytes(b'orphan')

    with pytest.raises(HTTPException) as exc:
        handlers.get_chat_attachment('orphan.jpg', user={'id': '1'}, role='owner')

    assert exc.value.status_code == 404


def test_registered_live_attachment_remains_accessible_to_owner(tmp_path):
    filename = 'registered.jpg'
    handlers, attach_dir = _build_handlers(
        tmp_path,
        live_messages=[_attachment_message(filename)],
    )
    path = attach_dir / filename
    path.write_bytes(b'image')

    response = handlers.get_chat_attachment(filename, user={'id': '1'}, role='owner')

    assert isinstance(response, FileResponse)
    assert response.path == str(path)


def test_registered_archived_attachment_remains_accessible_to_owner(tmp_path):
    filename = 'archived.jpg'
    handlers, attach_dir = _build_handlers(
        tmp_path,
        archived_messages=[_attachment_message(filename)],
    )
    path = attach_dir / filename
    path.write_bytes(b'image')

    response = handlers.get_chat_attachment(filename, user={'id': '1'}, role='owner')

    assert isinstance(response, FileResponse)
    assert response.path == str(path)


def test_registered_attachment_symlink_is_rejected(tmp_path):
    filename = 'alias.jpg'
    handlers, attach_dir = _build_handlers(
        tmp_path,
        live_messages=[_attachment_message(filename)],
    )
    target = attach_dir / 'target.jpg'
    target.write_bytes(b'secret')
    alias = attach_dir / filename
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_chat_attachment(filename, user={'id': '1'}, role='owner')

    assert exc.value.status_code == 404


def test_worker_still_needs_message_access(tmp_path):
    filename = 'dm.jpg'
    handlers, attach_dir = _build_handlers(
        tmp_path,
        live_messages=[_attachment_message(filename, user_id='42', to_user_id='1')],
    )
    path = attach_dir / filename
    path.write_bytes(b'image')

    response = handlers.get_chat_attachment(filename, user={'id': '42'}, role='worker')

    assert isinstance(response, FileResponse)
    assert response.path == str(path)
