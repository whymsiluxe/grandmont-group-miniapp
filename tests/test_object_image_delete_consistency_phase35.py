"""Regression for object-image delete post-commit cleanup semantics."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.object_media as object_media_routes  # noqa: E402
from test_object_media_ownership_phase28 import _build_handlers  # noqa: E402


def test_delete_returns_success_when_binary_cleanup_fails_after_metadata_commit(
    tmp_path, monkeypatch, caplog
):
    handlers, object_root, _blocker_root, _roadmap_file, state = _build_handlers(
        tmp_path, monkeypatch, {'OBJ-A': ['photo.jpg']}
    )
    photo = object_root / 'photo.jpg'
    photo.write_bytes(b'image')

    def fail_remove(path):
        assert path == str(photo)
        raise OSError('simulated filesystem cleanup failure')

    monkeypatch.setattr(object_media_routes.os, 'remove', fail_remove)

    result = handlers.delete_object_image(
        'OBJ-A', 'photo.jpg', user={'id': '1'}, _=None
    )

    assert result == {'status': 'ok', 'photos': []}
    assert state()['OBJ-A'] == []
    assert photo.exists(), 'failed cleanup may leave an unreachable orphan for retention'
    assert 'Object image binary cleanup failed after metadata delete' in caplog.text
