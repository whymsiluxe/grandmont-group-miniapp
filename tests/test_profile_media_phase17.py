from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from routes.mangel import MangelRouteDeps, create_mangel_router  # noqa: E402
from routes.workforce_profiles import (  # noqa: E402
    WorkforceProfilesDeps,
    create_workforce_profiles_router,
)


class FakeUpload:
    def __init__(self, raw: bytes):
        self.raw = raw

    async def read(self):
        return self.raw


def _profiles_router(tmp_path, *, transaction):
    avatar_dir = tmp_path / "avatars"
    avatar_dir.mkdir()
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text("{}", encoding="utf-8")

    deps = WorkforceProfilesDeps(
        get_current_user=lambda: {"id": 42},
        get_role=lambda: "worker",
        require_owner=lambda: None,
        load_roles=lambda: {"42": "worker"},
        load_worker_profiles=lambda: {},
        sanitize_display_name=lambda value, fallback: str(value or fallback),
        update_json_transaction=transaction,
        atomic_write_json=lambda path, value: None,
        worker_profiles_file=lambda: str(profiles_file),
        work_types_catalog=lambda: [],
        skill_options=[],
        normalize_profile_skills=lambda profile: (profile.get("skills_v2", []), False),
        legacy_skill_names_from_v2=lambda skills: [],
        set_skill_verification=lambda skills, skill_id, verified: (skills, False),
        skill_levels={"junior", "middle", "senior"},
        business_today=lambda: date(2026, 10, 13),
        business_today_str=lambda: "2026-10-13",
        last_seen_for=lambda uid: None,
        online_threshold_seconds=120,
        load_checkin_meta=lambda: [],
        is_active_photo_checkin_session=lambda session: False,
        cached_get_used_range=lambda sheet: [],
        avatar_dir=lambda: str(avatar_dir),
        avatar_max_bytes=4 * 1024 * 1024,
        sniff_image=lambda raw: "image/jpeg" if raw else None,
        allowed_image_mime_ext={"image/jpeg": "jpg"},
    )
    _, handlers = create_workforce_profiles_router(deps)
    return handlers, avatar_dir


def test_avatar_metadata_failure_restores_previous_binary(tmp_path):
    def fail_transaction(path, default, mutator):
        profiles = {}
        mutator(profiles)
        raise OSError("profile metadata failed")

    handlers, avatar_dir = _profiles_router(tmp_path, transaction=fail_transaction)
    old = avatar_dir / "42.jpg"
    old.write_bytes(b"old-avatar")

    with pytest.raises(OSError, match="profile metadata failed"):
        asyncio.run(
            handlers.upload_my_avatar(
                file=FakeUpload(b"new-avatar"),
                user={"id": 42, "first_name": "Worker"},
            )
        )

    assert old.read_bytes() == b"old-avatar"
    assert sorted(path.name for path in avatar_dir.iterdir()) == ["42.jpg"]


def test_avatar_symlink_escape_is_not_served(tmp_path):
    def transaction(path, default, mutator):
        profiles = {}
        return mutator(profiles)

    handlers, avatar_dir = _profiles_router(tmp_path, transaction=transaction)
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    link = avatar_dir / "42.jpg"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(HTTPException) as exc:
        handlers.get_avatar("42", user={"id": 42})
    assert exc.value.status_code == 404


def test_mangel_symlink_escape_is_not_served(tmp_path):
    photo_dir = tmp_path / "feed_photos"
    photo_dir.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    link = photo_dir / "mangel_escape.jpg"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    fake_lib = type(
        "FakeMangel",
        (),
        {
            "list_tickets": lambda self, object_id=None: [],
            "count_by_status": lambda self: {},
        },
    )()
    deps = MangelRouteDeps(
        get_current_user=lambda: {"id": 1},
        get_role=lambda: "owner",
        require_owner=lambda: None,
        load_mangel_lib=lambda: fake_lib,
        load_objekte_lib=lambda: None,
        load_worker_profiles=lambda: {},
        sanitize_display_name=lambda value, fallback: str(value or fallback),
        sniff_image=lambda raw: "image/jpeg" if raw else None,
        allowed_image_mime_ext={"image/jpeg": "jpg"},
        mangel_photo_dir=lambda: str(photo_dir),
        photo_dir=lambda: str(photo_dir),
        can_access_object=lambda user, role, object_id: True,
        append_object_history_best_effort=lambda *args, **kwargs: None,
    )
    _, handlers = create_mangel_router(deps)

    with pytest.raises(HTTPException) as exc:
        handlers.get_mangel_photo_file(
            "mangel_escape.jpg",
            user={"id": 1},
            role="owner",
        )
    assert exc.value.status_code == 404
