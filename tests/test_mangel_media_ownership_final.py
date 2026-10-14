"""Final regression for Mangel photo owned-media resolution."""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.mangel import MangelRouteDeps, create_mangel_router  # noqa: E402


def test_mangel_photo_symlink_escape_is_not_served(tmp_path):
    photo_dir = tmp_path / 'mangel-photos'
    photo_dir.mkdir()
    outside = tmp_path / 'secret.jpg'
    outside.write_bytes(b'secret')
    alias = photo_dir / 'mangel_escape.jpg'
    try:
        alias.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    fake_lib = type(
        'FakeMangel',
        (),
        {
            'list_tickets': lambda self, object_id=None: [],
            'count_by_status': lambda self: {},
        },
    )()
    deps = MangelRouteDeps(
        get_current_user=lambda: {'id': 1},
        get_role=lambda: 'owner',
        require_owner=lambda: None,
        load_mangel_lib=lambda: fake_lib,
        load_objekte_lib=lambda: None,
        load_worker_profiles=lambda: {},
        sanitize_display_name=lambda value, fallback: str(value or fallback),
        sniff_image=lambda raw: 'image/jpeg' if raw else None,
        allowed_image_mime_ext={'image/jpeg': 'jpg'},
        mangel_photo_dir=lambda: str(photo_dir),
        photo_dir=lambda: str(photo_dir),
        can_access_object=lambda user, role, object_id: True,
        append_object_history_best_effort=lambda *args, **kwargs: None,
    )
    _router, handlers = create_mangel_router(deps)

    with pytest.raises(HTTPException) as exc:
        handlers.get_mangel_photo_file(
            'mangel_escape.jpg',
            user={'id': 1},
            role='owner',
        )

    assert exc.value.status_code == 404
    assert outside.read_bytes() == b'secret'
