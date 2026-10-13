"""Phase 33 regressions for feed-photo file ownership."""
from __future__ import annotations

import os
import sys
import threading

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.activity_bridge as activity_routes  # noqa: E402
from routes.activity_bridge import ActivityBridgeDeps, create_activity_bridge_router  # noqa: E402


def _build_handlers(tmp_path, photos):
    photo_dir = tmp_path / 'photos'
    photo_dir.mkdir()
    deps = ActivityBridgeDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'worker',
        safe_load_json=lambda _path, default: default,
        atomic_write_json=lambda *_args, **_kwargs: None,
        activity_alerts_file=lambda: str(tmp_path / 'activity.json'),
        news_feed_file=lambda: str(tmp_path / 'news.json'),
        photo_dir=lambda: str(photo_dir),
        cached_get_used_range=lambda _name: [],
        load_roles=lambda: {},
        load_news_comments=lambda: {},
        photo_lock=threading.Lock(),
        load_photo_meta=lambda: photos,
        check_thread_access=lambda *_args, **_kwargs: None,
        reject_self_chat=lambda *_args, **_kwargs: None,
        validate_dm_recipient=lambda *_args, **_kwargs: None,
        chat_thread_id=lambda *_args, **_kwargs: 'group',
        load_chat_thread_meta=lambda: {},
        chat_lock=threading.Lock(),
        load_chat=lambda: [],
        save_chat=lambda _items: None,
    )
    _router, handlers = create_activity_bridge_router(deps)
    return handlers, photo_dir


def test_registered_feed_photo_file_remains_accessible(tmp_path):
    handlers, photo_dir = _build_handlers(
        tmp_path, [{'id': 'p1', 'files': ['photo.jpg']}]
    )
    photo = photo_dir / 'photo.jpg'
    photo.write_bytes(b'image')

    response = handlers.get_feed_photo_file('p1', index=0, user={'id': '42'})

    assert isinstance(response, FileResponse)
    assert response.path == str(photo)


def test_feed_photo_index_still_selects_requested_file(tmp_path):
    handlers, photo_dir = _build_handlers(
        tmp_path, [{'id': 'p1', 'files': ['one.jpg', 'two.jpg']}]
    )
    (photo_dir / 'one.jpg').write_bytes(b'one')
    second = photo_dir / 'two.jpg'
    second.write_bytes(b'two')

    response = handlers.get_feed_photo_file('p1', index=1, user={'id': '42'})

    assert response.path == str(second)


def test_legacy_single_file_field_remains_accessible(tmp_path):
    handlers, photo_dir = _build_handlers(
        tmp_path, [{'id': 'p1', 'file': 'legacy.jpg'}]
    )
    legacy = photo_dir / 'legacy.jpg'
    legacy.write_bytes(b'legacy')

    response = handlers.get_feed_photo_file('p1', index=0, user={'id': '42'})

    assert response.path == str(legacy)


def test_feed_photo_symlink_alias_is_rejected(tmp_path):
    handlers, photo_dir = _build_handlers(
        tmp_path, [{'id': 'p1', 'files': ['photo.jpg']}]
    )
    target = photo_dir / 'other.jpg'
    target.write_bytes(b'other-photo')
    alias = photo_dir / 'photo.jpg'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_feed_photo_file('p1', index=0, user={'id': '42'})

    assert exc.value.status_code == 404
    assert target.read_bytes() == b'other-photo'


def test_feed_photo_traversal_metadata_is_rejected(tmp_path):
    handlers, _photo_dir = _build_handlers(
        tmp_path, [{'id': 'p1', 'files': ['../secret.jpg']}]
    )
    secret = tmp_path / 'secret.jpg'
    secret.write_bytes(b'secret')

    with pytest.raises(HTTPException) as exc:
        handlers.get_feed_photo_file('p1', index=0, user={'id': '42'})

    assert exc.value.status_code == 404
    assert secret.read_bytes() == b'secret'


def test_feed_photo_get_structurally_uses_owned_media_resolver():
    import inspect

    source = inspect.getsource(activity_routes.create_activity_bridge_router)
    block = source.split('    def get_feed_photo_file', 1)[1].split(
        '    return router, ActivityBridgeHandlers', 1
    )[0]
    assert 'resolve_owned_media_path(deps.photo_dir()' in block
    assert 'os.path.join(deps.photo_dir()' not in block
