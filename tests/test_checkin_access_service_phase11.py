from copy import deepcopy
from pathlib import Path
from threading import RLock

import pytest

from services.checkin_access import (
    CheckinAccessDeps,
    CheckinAccessError,
    CheckinAccessService,
)


def _service(tmp_path, sessions=None, now=1000):
    store = deepcopy(sessions or [])
    saves = []

    def load_meta():
        return store

    def save_meta(items):
        saves.append(deepcopy(items))

    service = CheckinAccessService(CheckinAccessDeps(
        checkin_lock=RLock(),
        load_meta=load_meta,
        save_meta=save_meta,
        photo_base=lambda: str(tmp_path),
        resolve_media_path=lambda base, stored: str((Path(base) / stored).resolve()) if stored else None,
        now_timestamp=lambda: now,
    ))
    return service, store, saves


def test_pause_backfills_missing_accumulator_for_legacy_session(tmp_path):
    service, store, saves = _service(tmp_path, [{
        "id": "s1",
        "user_id": "42",
        "finish_at": None,
    }])

    result = service.pause_session("s1", "pause", "42", "worker")

    assert result == {
        "action": "pause",
        "changed": True,
        "paused": True,
        "pause_accumulated_seconds": 0,
        "pause_accumulated_minutes": 0,
    }
    assert store[0]["pause_accumulated_seconds"] == 0
    assert store[0]["pause_started_at"] == 1000
    assert len(saves) == 1


def test_pause_is_idempotent_for_repeated_same_action(tmp_path):
    service, store, saves = _service(tmp_path, [{
        "id": "s1",
        "user_id": "42",
        "finish_at": None,
        "pause_accumulated_seconds": 12,
        "pause_started_at": 900,
    }])

    result = service.pause_session("s1", "pause", "42", "worker")

    assert result["changed"] is False
    assert result["paused"] is True
    assert result["pause_accumulated_seconds"] == 12
    assert saves == []
    assert store[0]["pause_started_at"] == 900


def test_resume_accumulates_elapsed_pause_once(tmp_path):
    service, store, saves = _service(tmp_path, [{
        "id": "s1",
        "user_id": "42",
        "finish_at": None,
        "pause_accumulated_seconds": 20,
        "pause_started_at": 900,
    }], now=1000)

    result = service.pause_session("s1", "resume", "42", "worker")

    assert result["pause_accumulated_seconds"] == 120
    assert store[0]["pause_started_at"] is None
    assert len(saves) == 1


def test_list_sessions_ignores_corrupt_entries_and_does_not_mutate_store(tmp_path):
    original = [
        None,
        "broken",
        {
            "id": "s1",
            "user_id": "42",
            "object_id": "OBJ-1",
            "date": "2026-10-13",
            "voice_note_file_id": "voice-1",
        },
        {
            "id": "s2",
            "user_id": "99",
            "object_id": "OBJ-1",
            "date": "2026-10-13",
        },
    ]
    service, store, _ = _service(tmp_path, original)

    result = service.list_sessions("42", "worker", object_id="OBJ-1", date="2026-10-13")

    assert [item["id"] for item in result["sessions"]] == ["s1"]
    assert result["sessions"][0]["voice_note_audio_url"] == "/api/transcribe/voice-1/audio"
    assert "voice_note_audio_url" not in store[2]


def test_worker_cannot_access_foreign_photo(tmp_path):
    service, _, _ = _service(tmp_path, [{
        "id": "s1",
        "user_id": "99",
        "start_photos": ["photo.jpg"],
    }])

    with pytest.raises(CheckinAccessError) as exc_info:
        service.photo_path("s1", "start", 0, "42", "worker")

    assert exc_info.value.status_code == 403


def test_invalid_photo_side_does_not_alias_to_finish(tmp_path):
    service, _, _ = _service(tmp_path, [{
        "id": "s1",
        "user_id": "42",
        "finish_photos": ["photo.jpg"],
    }])

    with pytest.raises(CheckinAccessError) as exc_info:
        service.photo_path("s1", "anything", 0, "42", "worker")

    assert exc_info.value.status_code == 404


def test_checkin_access_service_stays_framework_agnostic():
    source = Path(__file__).parents[1] / "backend" / "services" / "checkin_access.py"
    text = source.read_text(encoding="utf-8").lower()
    assert "from fastapi" not in text
    assert "import fastapi" not in text
    assert "from pydantic" not in text
    assert "import pydantic" not in text
    assert "import main" not in text
