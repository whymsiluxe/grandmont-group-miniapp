"""Phase 30 regressions for chat attachment media ownership."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.chat_actions import ChatActionsRouteDeps, create_chat_actions_router  # noqa: E402


def _thread_id(user_a, user_b):
    if user_b is None:
        return 'group'
    return '-'.join(sorted((str(user_a), str(user_b))))


def _build_handlers(tmp_path, *, active=None, archived=None, roles=None):
    attach_dir = tmp_path / 'attachments'
    attach_dir.mkdir()
    archive_file = tmp_path / 'archive.json'
    active_messages = list(active or [])
    archived_messages = list(archived or [])
    role_map = dict(roles or {'42': 'worker', '99': 'worker', '1': 'owner'})

    core = SimpleNamespace(
        load_chat=lambda: list(active_messages),
        chat_thread_id=_thread_id,
        check_thread_access=lambda _thread_key, _uid, _role: None,
        message_preview=lambda msg: msg.get('text', ''),
        object_chat_participants=lambda _object_id: set(),
        load_chat_thread_meta=lambda: {},
        chat_lock=SimpleNamespace(
            __enter__=lambda self: self,
            __exit__=lambda self, exc_type, exc, tb: False,
        ),
    )
    deps = ChatActionsRouteDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'worker',
        require_owner=lambda: None,
        core=core,
        safe_load_json=lambda path, default: (
            list(archived_messages) if path == str(archive_file) else default
        ),
        chat_archive_file=lambda: str(archive_file),
        chat_attach_dir=lambda: str(attach_dir),
        chat_reaction_options=('👍',),
        load_roles=lambda: role_map,
        send_telegram_message=lambda *_args, **_kwargs: None,
        find_object_row_by_id=lambda _object_id: None,
        append_object_history_best_effort=lambda *_args, **_kwargs: None,
    )
    _router, handlers = create_chat_actions_router(deps)
    return handlers, attach_dir


def _message(fname, *, user_id='42', to_user_id='99', thread_key=None):
    return {
        'id': f'msg-{fname}-{user_id}-{to_user_id}',
        'user_id': user_id,
        'to_user_id': to_user_id,
        'thread_key': thread_key,
        'attachment': {'file': fname, 'name': fname},
    }


def test_owner_cannot_serve_unreferenced_physical_attachment(tmp_path):
    handlers, attach_dir = _build_handlers(tmp_path)
    (attach_dir / 'orphan.pdf').write_bytes(b'orphan')

    with pytest.raises(HTTPException) as exc:
        handlers.get_chat_attachment(
            'orphan.pdf', user={'id': '1'}, role='owner'
        )

    assert exc.value.status_code == 404


def test_owner_can_serve_attachment_referenced_by_message_metadata(tmp_path):
    handlers, attach_dir = _build_handlers(
        tmp_path, active=[_message('doc.pdf')]
    )
    attachment = attach_dir / 'doc.pdf'
    attachment.write_bytes(b'document')

    response = handlers.get_chat_attachment(
        'doc.pdf', user={'id': '1'}, role='owner'
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(attachment)
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_registered_attachment_symlink_alias_is_rejected(tmp_path):
    handlers, attach_dir = _build_handlers(
        tmp_path, active=[_message('doc.pdf')]
    )
    target = attach_dir / 'other.pdf'
    target.write_bytes(b'other-message')
    alias = attach_dir / 'doc.pdf'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_chat_attachment(
            'doc.pdf', user={'id': '1'}, role='owner'
        )

    assert exc.value.status_code == 404
    assert target.read_bytes() == b'other-message'


def test_worker_can_read_attachment_from_own_dm_thread(tmp_path):
    handlers, attach_dir = _build_handlers(
        tmp_path, active=[_message('doc.pdf', user_id='42', to_user_id='99')]
    )
    attachment = attach_dir / 'doc.pdf'
    attachment.write_bytes(b'document')

    response = handlers.get_chat_attachment(
        'doc.pdf', user={'id': '42'}, role='worker'
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(attachment)


def test_worker_is_denied_attachment_from_unrelated_dm_thread(tmp_path):
    handlers, attach_dir = _build_handlers(
        tmp_path,
        active=[_message('doc.pdf', user_id='77', to_user_id='88')],
        roles={'42': 'worker', '77': 'worker', '88': 'worker', '1': 'owner'},
    )
    (attach_dir / 'doc.pdf').write_bytes(b'document')

    with pytest.raises(HTTPException) as exc:
        handlers.get_chat_attachment(
            'doc.pdf', user={'id': '42'}, role='worker'
        )

    assert exc.value.status_code == 403


def test_archived_message_still_proves_attachment_membership(tmp_path):
    handlers, attach_dir = _build_handlers(
        tmp_path,
        archived=[_message('archived.pdf', user_id='42', to_user_id='99')],
    )
    attachment = attach_dir / 'archived.pdf'
    attachment.write_bytes(b'archive')

    response = handlers.get_chat_attachment(
        'archived.pdf', user={'id': '42'}, role='worker'
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(attachment)
