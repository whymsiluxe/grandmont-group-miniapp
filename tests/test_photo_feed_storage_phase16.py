from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from routes.photo_feed import PhotoFeedRouteDeps, create_photo_feed_router  # noqa: E402


class FakeUpload:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def read(self):
        return self.raw


def _write_json(path: str, value) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def _build(tmp_path, *, max_count=200, atomic_write=None):
    photo_dir = tmp_path / "photos"
    checkin_dir = tmp_path / "checkin"
    meta_path = tmp_path / "feed_photos.json"
    reactions_path = tmp_path / "feed_photo_reactions.json"
    photo_dir.mkdir()
    checkin_dir.mkdir()
    _write_json(str(meta_path), [])
    _write_json(str(reactions_path), {})

    def safe_load(path, default):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            return copy.deepcopy(default)

    if atomic_write is None:
        def atomic_write(path, value):
            tmp = f"{path}.tmp"
            _write_json(tmp, value)
            os.replace(tmp, path)

    _, handlers = create_photo_feed_router(
        PhotoFeedRouteDeps(
            get_current_user=lambda: {},
            get_role=lambda: "worker",
            safe_load_json=safe_load,
            atomic_write_json=atomic_write,
            photo_dir=lambda: str(photo_dir),
            photo_meta_file=lambda: str(meta_path),
            photo_reactions_file=lambda: str(reactions_path),
            checkin_photo_base=lambda: str(checkin_dir),
            photo_max_bytes=1024,
            photo_max_count=max_count,
            photo_max_files=10,
            sniff_image=lambda raw: "image/jpeg" if raw else None,
            allowed_image_mime_ext={"image/jpeg": "jpg"},
            load_feed_saved=lambda: {},
            is_feed_saved=lambda *args: False,
            sanitize_display_name=lambda raw, fallback: str(raw or fallback),
            resolve_current_display_name=lambda uid, fallback: str(fallback or uid),
            resolve_object_name=lambda object_id: str(object_id or ""),
            emit_comment_activity_alerts=lambda *args, **kwargs: None,
            comment_alert_recipients=lambda *args, **kwargs: set(),
        )
    )
    return handlers, photo_dir, checkin_dir, meta_path, reactions_path


def test_bad_second_upload_leaves_no_partial_binary(tmp_path):
    handlers, photo_dir, _, _, _ = _build(tmp_path)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            handlers.upload_feed_photo(
                files=[FakeUpload(b"valid-image"), FakeUpload(b"")],
                object_id="OBJ-1",
                caption="",
                user={"id": 1, "first_name": "Worker"},
            )
        )

    assert exc.value.status_code == 400
    assert list(photo_dir.iterdir()) == []


def test_failed_metadata_commit_keeps_old_live_binary_and_cleans_new_one(tmp_path):
    meta_path_holder = {}

    def atomic_write(path, value):
        if path == meta_path_holder.get("meta"):
            raise OSError("metadata commit failed")
        tmp = f"{path}.tmp"
        _write_json(tmp, value)
        os.replace(tmp, path)

    handlers, photo_dir, _, meta_path, _ = _build(
        tmp_path,
        max_count=1,
        atomic_write=atomic_write,
    )
    meta_path_holder["meta"] = str(meta_path)
    old_path = photo_dir / "old.jpg"
    old_path.write_bytes(b"old")
    old_metadata = [
        {
            "id": "old",
            "files": ["old.jpg"],
            "ts": 1,
            "user_id": 1,
            "name": "old",
            "object_id": "OBJ-1",
            "caption": "",
        }
    ]
    _write_json(str(meta_path), old_metadata)

    with pytest.raises(OSError, match="metadata commit failed"):
        asyncio.run(
            handlers.upload_feed_photo(
                files=[FakeUpload(b"new-image")],
                object_id="OBJ-1",
                caption="new",
                user={"id": 2, "first_name": "New"},
            )
        )

    assert old_path.exists(), "old binary must not be deleted before metadata commit"
    assert sorted(path.name for path in photo_dir.iterdir()) == ["old.jpg"]
    with open(meta_path, encoding="utf-8") as handle:
        assert json.load(handle) == old_metadata


def test_successful_eviction_commits_metadata_before_old_binary_cleanup(tmp_path):
    handlers, photo_dir, _, meta_path, _ = _build(tmp_path, max_count=1)
    old_path = photo_dir / "old.jpg"
    old_path.write_bytes(b"old")
    _write_json(
        str(meta_path),
        [
            {
                "id": "old",
                "files": ["old.jpg"],
                "ts": 1,
                "user_id": 1,
                "name": "old",
                "object_id": "OBJ-1",
                "caption": "",
            }
        ],
    )

    result = asyncio.run(
        handlers.upload_feed_photo(
            files=[FakeUpload(b"new-image")],
            object_id="OBJ-1",
            caption="new",
            user={"id": 2, "first_name": "New"},
        )
    )

    assert not old_path.exists()
    assert len(list(photo_dir.iterdir())) == 1
    with open(meta_path, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert [item["id"] for item in stored] == [result["photo"]["id"]]


def test_checkin_copy_rejects_parent_traversal_and_absolute_escape(tmp_path):
    handlers, photo_dir, checkin_dir, _, _ = _build(tmp_path)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")

    assert handlers.copy_checkin_photos_to_feed(["../secret.jpg"], "x") == []
    assert handlers.copy_checkin_photos_to_feed([str(outside)], "x") == []
    assert list(photo_dir.iterdir()) == []

    safe = checkin_dir / "OBJ-1" / "safe.jpg"
    safe.parent.mkdir()
    safe.write_bytes(b"safe")
    copied = handlers.copy_checkin_photos_to_feed(["OBJ-1/safe.jpg"], "x")
    assert copied == ["x_0.jpg"]
    assert (photo_dir / "x_0.jpg").read_bytes() == b"safe"


def test_checkin_copy_rejects_symlink_escape(tmp_path):
    handlers, photo_dir, checkin_dir, _, _ = _build(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    link = checkin_dir / "escape.jpg"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    assert handlers.copy_checkin_photos_to_feed(["escape.jpg"], "x") == []
    assert list(photo_dir.iterdir()) == []
