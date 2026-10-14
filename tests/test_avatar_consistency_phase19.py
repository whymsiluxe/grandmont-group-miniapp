"""Phase 19 regressions for avatar binary/metadata consistency."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

import main as backend


PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)
GIF_1X1 = base64.b64decode(
    'R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='
)


def _upload(name: str, raw: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        filename=name,
        file=io.BytesIO(raw),
        headers=Headers({'content-type': content_type}),
    )


def test_avatar_profile_commit_failure_restores_previous_binary_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        # Point the metadata store at an existing directory. The real
        # update_json_transaction() therefore fails with IsADirectoryError after
        # the staged avatar binary has already been installed.
        profiles_store = os.path.join(tmp, 'profiles-store')
        avatar_dir = os.path.join(tmp, 'avatars')
        os.makedirs(profiles_store)
        os.makedirs(avatar_dir)
        old_path = os.path.join(avatar_dir, '42.png')
        old_bytes = b'previous-avatar-bytes'
        with open(old_path, 'wb') as handle:
            handle.write(old_bytes)

        with (
            patch.object(backend, 'AVATAR_DIR', avatar_dir),
            patch.object(backend, 'WORKER_PROFILES_FILE', profiles_store),
        ):
            with pytest.raises(IsADirectoryError):
                asyncio.run(
                    backend.upload_my_avatar(
                        file=_upload('new.png', PNG_1X1, 'image/png'),
                        user={'id': '42', 'first_name': 'Worker'},
                    )
                )

        with open(old_path, 'rb') as handle:
            assert handle.read() == old_bytes
        assert sorted(os.listdir(avatar_dir)) == ['42.png']


def test_gif_avatar_upload_is_served_and_extension_is_persisted():
    with tempfile.TemporaryDirectory() as tmp:
        profiles_file = os.path.join(tmp, 'profiles.json')
        avatar_dir = os.path.join(tmp, 'avatars')
        os.makedirs(avatar_dir)
        with open(profiles_file, 'w', encoding='utf-8') as handle:
            json.dump({}, handle)

        with (
            patch.object(backend, 'AVATAR_DIR', avatar_dir),
            patch.object(backend, 'WORKER_PROFILES_FILE', profiles_file),
        ):
            result = asyncio.run(
                backend.upload_my_avatar(
                    file=_upload('avatar.gif', GIF_1X1, 'image/gif'),
                    user={'id': '42', 'first_name': 'Worker'},
                )
            )
            assert result == {'status': 'ok'}

            with open(profiles_file, encoding='utf-8') as handle:
                profile = json.load(handle)['42']
            assert profile['avatar'] is True
            assert profile['avatar_ext'] == 'gif'
            assert os.path.exists(os.path.join(avatar_dir, '42.gif'))

            response = backend.get_avatar('42', user={'id': '42'})
            assert os.path.realpath(response.path) == os.path.realpath(
                os.path.join(avatar_dir, '42.gif')
            )
