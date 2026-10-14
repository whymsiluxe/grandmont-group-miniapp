"""Phase 28 regressions for object and blocker media ownership."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from copy import deepcopy
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.object_media as object_media_routes  # noqa: E402
from routes.object_media import ObjectMediaDeps, create_object_media_router  # noqa: E402


def _build_handlers(tmp_path, monkeypatch, images=None):
    object_root = tmp_path / 'object-photos'
    blocker_root = tmp_path / 'blocker-photos'
    object_root.mkdir()
    blocker_root.mkdir()
    object_images_file = tmp_path / 'object_images.json'
    roadmap_file = tmp_path / 'roadmap.json'
    state = deepcopy(images or {})
    roadmap_file.write_text(
        json.dumps({'categories': {}, 'items': {}, 'notes': {}, 'stage_blocks': {}}),
        encoding='utf-8',
    )
    monkeypatch.setattr(object_media_routes._roadmap_lib, 'ROADMAP_FILE', str(roadmap_file))

    def update_json_transaction(_path, default, mutator):
        nonlocal state
        working = deepcopy(state if state is not None else default)
        result = mutator(working)
        state = working
        return result

    deps = ObjectMediaDeps(
        get_current_user=lambda: {'id': '42'},
        require_owner=lambda: None,
        require_object_access=lambda: None,
        load_object_images=lambda: deepcopy(state),
        update_json_transaction=update_json_transaction,
        object_images_file=lambda: str(object_images_file),
        object_photo_dir=lambda: str(object_root),
        blocker_photo_dir=lambda: str(blocker_root),
        sniff_image=lambda _raw: 'image/jpeg',
        allowed_image_mime_ext={'image/jpeg': 'jpg'},
        object_photo_max=10,
        object_photo_max_bytes=8 * 1024 * 1024,
        blocker_photo_max_bytes=8 * 1024 * 1024,
    )
    _router, handlers = create_object_media_router(deps)
    return handlers, object_root, blocker_root, roadmap_file, lambda: deepcopy(state)


def test_object_image_normal_registered_file_remains_accessible(tmp_path, monkeypatch):
    handlers, object_root, _blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch, {'OBJ-A': ['photo.jpg']}
    )
    photo = object_root / 'photo.jpg'
    photo.write_bytes(b'image')

    response = handlers.get_object_image_file('OBJ-A', index=0, user={'id': '42'})

    assert isinstance(response, FileResponse)
    assert response.path == str(photo)


def test_object_image_symlink_alias_is_rejected(tmp_path, monkeypatch):
    handlers, object_root, _blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch, {'OBJ-A': ['photo.jpg']}
    )
    target = object_root / 'other.jpg'
    target.write_bytes(b'other-object')
    alias = object_root / 'photo.jpg'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_object_image_file('OBJ-A', index=0, user={'id': '42'})

    assert exc.value.status_code == 404


def test_object_image_delete_does_not_follow_symlink_target(tmp_path, monkeypatch):
    handlers, object_root, _blocker_root, _roadmap_file, state = _build_handlers(
        tmp_path, monkeypatch, {'OBJ-A': ['photo.jpg']}
    )
    target = object_root / 'other.jpg'
    target.write_bytes(b'keep-me')
    alias = object_root / 'photo.jpg'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    result = handlers.delete_object_image(
        'OBJ-A', 'photo.jpg', user={'id': '1'}, _=None
    )

    assert result == {'status': 'ok', 'photos': []}
    assert state()['OBJ-A'] == []
    assert target.read_bytes() == b'keep-me'


def test_new_blocker_upload_is_namespaced_to_object_and_served(tmp_path, monkeypatch):
    handlers, _object_root, blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch
    )
    upload = UploadFile(filename='blocker.jpg', file=BytesIO(b'image-bytes'))

    result = asyncio.run(
        handlers.upload_blocker_photo('OBJ-A', upload, user={'id': '42'}, _=None)
    )
    fname = result['photo_url'].rsplit('/', 1)[-1]
    namespace = hashlib.sha256(b'OBJ-A').hexdigest()
    stored = blocker_root / namespace / fname

    assert stored.read_bytes() == b'image-bytes'
    response = handlers.get_blocker_photo(
        'OBJ-A', fname, user={'id': '42'}, _=None
    )
    assert isinstance(response, FileResponse)
    assert response.path == str(stored)


def test_new_blocker_filename_cannot_be_replayed_under_another_object(tmp_path, monkeypatch):
    handlers, _object_root, _blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch
    )
    upload = UploadFile(filename='blocker.jpg', file=BytesIO(b'image-bytes'))
    result = asyncio.run(
        handlers.upload_blocker_photo('OBJ-A', upload, user={'id': '42'}, _=None)
    )
    fname = result['photo_url'].rsplit('/', 1)[-1]

    with pytest.raises(HTTPException) as exc:
        handlers.get_blocker_photo('OBJ-B', fname, user={'id': '42'}, _=None)

    assert exc.value.status_code == 404


def test_legacy_global_blocker_file_requires_matching_roadmap_url(tmp_path, monkeypatch):
    handlers, _object_root, blocker_root, roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch
    )
    fname = 'legacy.jpg'
    legacy = blocker_root / fname
    legacy.write_bytes(b'legacy')
    roadmap_file.write_text(
        json.dumps({
            'categories': {},
            'items': {},
            'notes': {},
            'stage_blocks': {
                'stage-1': {
                    'photo_url': f'/api/objects/OBJ-A/blocker-photo/{fname}'
                }
            },
        }),
        encoding='utf-8',
    )

    response = handlers.get_blocker_photo(
        'OBJ-A', fname, user={'id': '42'}, _=None
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(legacy)


def test_legacy_global_blocker_file_cannot_cross_object_boundary(tmp_path, monkeypatch):
    handlers, _object_root, blocker_root, roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch
    )
    fname = 'legacy.jpg'
    (blocker_root / fname).write_bytes(b'legacy')
    roadmap_file.write_text(
        json.dumps({
            'categories': {},
            'items': {},
            'notes': {},
            'stage_blocks': {
                'stage-1': {
                    'photo_url': f'/api/objects/OBJ-B/blocker-photo/{fname}'
                }
            },
        }),
        encoding='utf-8',
    )

    with pytest.raises(HTTPException) as exc:
        handlers.get_blocker_photo('OBJ-A', fname, user={'id': '42'}, _=None)

    assert exc.value.status_code == 404


def test_orphan_legacy_global_blocker_file_is_not_authoritative(tmp_path, monkeypatch):
    handlers, _object_root, blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch
    )
    fname = 'orphan.jpg'
    (blocker_root / fname).write_bytes(b'orphan')

    with pytest.raises(HTTPException) as exc:
        handlers.get_blocker_photo('OBJ-A', fname, user={'id': '42'}, _=None)

    assert exc.value.status_code == 404
