from __future__ import annotations

import asyncio
import copy
import os
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from routes.checkin import create_checkin_router  # noqa: E402


def _deps(*, load_meta, save_photos, cleanup, release, save_meta):
    return SimpleNamespace(
        get_current_user=lambda: {"id": 1},
        get_role=lambda: "owner",
        checkin_photo_base=lambda: "/tmp/checkin-photos-test",
        checkin_lock=threading.Lock(),
        finish_outbox_lock=threading.Lock(),
        load_checkin_meta=load_meta,
        save_checkin_meta=save_meta,
        save_checkin_photos=save_photos,
        cleanup_checkin_photo_files=cleanup,
        checkin_file_count=lambda files: len(files),
        is_active_photo_checkin_session=lambda item: (
            not item.get("manual_entry") and item.get("finish_at") is None
        ),
        gps_suspect=lambda lat, lon: False,
        can_access_object=lambda user, role, object_id: True,
        get_active_assignment_for_checkin=lambda *args: "",
        idempotency_scope=lambda *args: "scope",
        idempotency_claim=lambda *args: None,
        idempotency_save=lambda *args: None,
        idempotency_release=release,
        idempotent_entity_id=lambda *args: "session-new",
        load_worker_profiles=lambda: {},
        sanitize_display_name=lambda value, fallback="": value or fallback,
        cached_get_used_range=lambda *args: [],
        upsert_checkin_feed_post=lambda *args, **kwargs: None,
        business_now=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        business_today_str=lambda: "2026-10-13",
        outbox_write_pending=lambda *args, **kwargs: None,
        outbox_mark_applied=lambda *args, **kwargs: None,
        outbox_mark_failed=lambda *args, **kwargs: None,
        outbox_mark_dead_letter=lambda *args, **kwargs: None,
        clear_pending_execution_report=lambda *args, **kwargs: None,
        reject_pending_execution_report=lambda *args, **kwargs: None,
        write_zeiterfassung_row=lambda *args, **kwargs: None,
        object_history_worker_name=lambda worker_id: str(worker_id),
        append_object_history_best_effort=lambda *args, **kwargs: None,
        extra_works_summary_text=lambda session: "",
        load_roles=lambda: {},
        send_telegram_message=lambda *args, **kwargs: None,
        photo_pause_minutes=lambda session: 0,
        hours_from_session=lambda session: 0.0,
        get_worker_profile=lambda *args, **kwargs: {},
        csv_safe=lambda value: value,
    )


def test_start_cleans_uploaded_photos_and_releases_claim_when_metadata_write_fails():
    cleaned = []
    released = []
    uploaded = ["OBJ-1/2026-10-13/start.jpg"]

    async def save_photos(files, object_id, date_str):
        return list(uploaded)

    def fail_save(items):
        raise OSError("metadata write failed")

    deps = _deps(
        load_meta=lambda: [],
        save_photos=save_photos,
        cleanup=lambda paths: cleaned.append(list(paths)),
        release=lambda *args: released.append(args),
        save_meta=fail_save,
    )
    _, handlers = create_checkin_router(deps)

    with pytest.raises(OSError, match="metadata write failed"):
        asyncio.run(
            handlers.checkin_start(
                object_id="OBJ-1",
                lat="51.0",
                lon="7.0",
                accuracy="",
                geo_timestamp="",
                occurred_at="",
                stage_name="",
                files=[object()],
                daily_plan_id="",
                daily_plan_version="",
                daily_plan_acceptance_id="",
                user={"id": 1},
                role="owner",
                idempotency_key="start-key",
            )
        )

    assert cleaned == [uploaded]
    assert len(released) == 1
    assert released[0] == ("start-key", "scope")


def test_finish_cleans_uploaded_photos_and_releases_claim_when_metadata_write_fails():
    cleaned = []
    released = []
    uploaded = [
        "OBJ-1/2026-10-13/finish-1.jpg",
        "OBJ-1/2026-10-13/finish-2.jpg",
    ]
    canonical_session = {
        "id": "s1",
        "object_id": "OBJ-1",
        "date": "2026-10-13",
        "user_id": 1,
        "start_at": 1,
        "finish_at": None,
        "start_photos": ["OBJ-1/2026-10-13/start.jpg"],
        "finish_photos": [],
        "pause_started_at": None,
        "pause_accumulated_seconds": 0,
        "daily_plan_id": None,
    }

    async def save_photos(files, object_id, date_str):
        return list(uploaded)

    def load_meta():
        # Failed persistence must not mutate the canonical fixture between the
        # preflight read and the final commit attempt.
        return [copy.deepcopy(canonical_session)]

    def fail_save(items):
        raise OSError("metadata write failed")

    deps = _deps(
        load_meta=load_meta,
        save_photos=save_photos,
        cleanup=lambda paths: cleaned.append(list(paths)),
        release=lambda *args: released.append(args),
        save_meta=fail_save,
    )
    _, handlers = create_checkin_router(deps)

    with pytest.raises(OSError, match="metadata write failed"):
        asyncio.run(
            handlers.checkin_finish(
                session_id="s1",
                lat="51.0",
                lon="7.0",
                accuracy="",
                geo_timestamp="",
                occurred_at="",
                done_summary="done",
                extra_work="",
                extra_works="",
                needs="",
                defects="",
                next_day_needs="",
                pause_minutes=0,
                voice_note_file_id="",
                daily_plan_report="",
                files=[object(), object()],
                user={"id": 1},
                role="owner",
                idempotency_key="finish-key",
            )
        )

    assert cleaned == [uploaded]
    assert len(released) == 1
    assert released[0] == ("finish-key", "scope")
