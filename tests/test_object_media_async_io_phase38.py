"""Phase 38 guards for blocking object-media IO inside async handlers."""
from __future__ import annotations

import ast
import asyncio
import inspect
import io
import os
import sys
import textwrap

from fastapi import UploadFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.object_media as object_media_routes  # noqa: E402
from test_object_media_ownership_phase28 import _build_handlers  # noqa: E402


async def _run_inline_to_thread(calls, func, *args, **kwargs):
    calls.append(func)
    return func(*args, **kwargs)


def test_object_image_upload_offloads_binary_write_and_metadata_transaction(tmp_path, monkeypatch):
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        return await _run_inline_to_thread(calls, func, *args, **kwargs)

    monkeypatch.setattr(object_media_routes.asyncio, 'to_thread', fake_to_thread)
    handlers, object_root, _blocker_root, _roadmap_file, state = _build_handlers(
        tmp_path, monkeypatch, {}
    )

    result = asyncio.run(handlers.upload_object_image(
        'OBJ-A',
        UploadFile(filename='photo.bin', file=io.BytesIO(b'image')),
        user={'id': '1'},
        _=None,
    ))

    assert result['status'] == 'ok'
    assert len(result['photos']) == 1
    assert state()['OBJ-A'] == result['photos']
    assert (object_root / result['photos'][0]).read_bytes() == b'image'
    assert object_media_routes._write_binary_file in calls
    assert any(getattr(func, '__name__', '') == 'update_json_transaction' for func in calls)


def test_blocker_upload_offloads_secure_directory_and_binary_write(tmp_path, monkeypatch):
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        return await _run_inline_to_thread(calls, func, *args, **kwargs)

    monkeypatch.setattr(object_media_routes.asyncio, 'to_thread', fake_to_thread)
    handlers, _object_root, blocker_root, _roadmap_file, _state = _build_handlers(
        tmp_path, monkeypatch, {}
    )

    result = asyncio.run(handlers.upload_blocker_photo(
        'OBJ-A',
        UploadFile(filename='photo.bin', file=io.BytesIO(b'image')),
        user={'id': '1'},
        _=None,
    ))

    fname = result['photo_url'].rsplit('/', 1)[-1]
    namespace = object_media_routes._blocker_object_namespace('OBJ-A')
    assert (blocker_root / namespace / fname).read_bytes() == b'image'
    assert object_media_routes._write_owned_blocker_file in calls


def test_async_upload_handlers_do_not_call_blocking_file_primitives_directly():
    source = textwrap.dedent(inspect.getsource(object_media_routes.create_object_media_router))
    tree = ast.parse(source)
    async_handlers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {'upload_object_image', 'upload_blocker_photo'}
    }
    assert set(async_handlers) == {'upload_object_image', 'upload_blocker_photo'}

    for name, node in async_handlers.items():
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        assert any(
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == 'asyncio'
            and call.func.attr == 'to_thread'
            for call in calls
        ), f'{name} must offload blocking work with asyncio.to_thread'
        assert not any(
            isinstance(call.func, ast.Name) and call.func.id == 'open'
            for call in calls
        ), f'{name} performs direct blocking open() on the event loop'
        assert not any(
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == 'os'
            and call.func.attr == 'makedirs'
            for call in calls
        ), f'{name} performs direct os.makedirs() on the event loop'
