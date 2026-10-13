"""Phase 21 regressions for post-commit check-in idempotency persistence."""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
from copy import deepcopy
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.checkin import CheckinRouteDeps, create_checkin_router  # noqa: E402


def _build_handlers(initial_sessions=None):
    sessions = deepcopy(initial_sessions or [])
    idempotency_save_calls = []

    async def save_photos(files, object_id, date_str):
        return ['start.jpg'] if files else []

    def load_meta():
        return deepcopy(sessions)

    def save_meta(items):
        sessions[:] = deepcopy(items)

    def fail_idempotency_save(key, response, scope):
        idempotency_save_calls.append((key, deepcopy(response), deepcopy(scope)))
        raise OSError('idempotency store unavailable')

    deps = CheckinRouteDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'owner',
        checkin_photo_base=lambda: '/tmp/checkin-test',
        checkin_lock=threading.Lock(),
        finish_outbox_lock=threading.Lock(),
        load_checkin_meta=load_meta,
        save_checkin_meta=save_meta,
        save_checkin_photos=save_photos,
        cleanup_checkin_photo_files=lambda paths: None,
        checkin_file_count=lambda files: len(files),
        is_active_photo_checkin_session=lambda s: not bool(s.get('finish_at')),
        gps_suspect=lambda lat, lon: False,
        can_access_object=lambda user, role, object_id: True,
        get_active_assignment_for_checkin=lambda user_id, object_id, today: 'assignment-1',
        idempotency_scope=lambda kind, actor_id, entity_id='', payload=None: {
            'kind': kind,
            'actor_id': str(actor_id),
            'entity_id': entity_id,
            'payload': payload or {},
        },
        idempotency_claim=lambda key, scope=None: None,
        idempotency_save=fail_idempotency_save,
        idempotency_release=lambda key, scope=None: None,
        idempotent_entity_id=lambda key, scope: 'session-1',
        load_worker_profiles=lambda: {},
        sanitize_display_name=lambda raw, fallback: raw or fallback,
        cached_get_used_range=lambda tab_name: [],
        upsert_checkin_feed_post=lambda *args, **kwargs: None,
        business_now=lambda: datetime.now(timezone.utc),
        business_today_str=lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        outbox_write_pending=lambda *args, **kwargs: None,
        outbox_mark_applied=lambda *args, **kwargs: None,
        outbox_mark_failed=lambda *args, **kwargs: None,
        outbox_mark_dead_letter=lambda *args, **kwargs: None,
        clear_pending_execution_report=lambda *args, **kwargs: None,
        reject_pending_execution_report=lambda *args, **kwargs: None,
        write_zeiterfassung_row=lambda *args, **kwargs: None,
        object_history_worker_name=lambda user_id: str(user_id),
        append_object_history_best_effort=lambda *args, **kwargs: None,
        extra_works_summary_text=lambda session: '',
        load_roles=lambda: {},
        send_telegram_message=lambda *args, **kwargs: None,
        photo_pause_minutes=lambda session: 0,
        hours_from_session=lambda session: 0.0,
        get_worker_profile=lambda user_id: {},
        csv_safe=lambda value: value,
    )
    _router, handlers = create_checkin_router(deps)
    return handlers, sessions, idempotency_save_calls


def test_start_returns_success_when_secondary_idempotency_store_fails_after_commit():
    handlers, sessions, save_calls = _build_handlers()

    result = asyncio.run(handlers.checkin_start(
        object_id='OBJ-1',
        lat='50.8',
        lon='12.9',
        accuracy='',
        geo_timestamp='',
        occurred_at='',
        stage_name='',
        files=[object()],
        daily_plan_id='',
        daily_plan_version='',
        daily_plan_acceptance_id='',
        user={'id': '42', 'first_name': 'Worker'},
        role='owner',
        idempotency_key='start-key',
    ))

    assert result['id'] == 'session-1'
    assert len(sessions) == 1
    assert sessions[0]['id'] == 'session-1'
    assert len(save_calls) == 1


def test_finished_shift_replay_returns_success_when_secondary_idempotency_store_fails():
    finished = {
        'id': 'session-1',
        'object_id': 'OBJ-1',
        'date': '2026-10-13',
        'user_id': '42',
        'start_at': 1,
        'finish_at': 2,
        'manual_entry': False,
    }
    handlers, sessions, save_calls = _build_handlers([finished])

    result = asyncio.run(handlers.checkin_finish(
        session_id='session-1',
        lat='50.8',
        lon='12.9',
        accuracy='',
        geo_timestamp='',
        occurred_at='',
        done_summary='done',
        extra_work='',
        extra_works='',
        needs='',
        defects='',
        next_day_needs='',
        pause_minutes=0,
        voice_note_file_id='',
        daily_plan_report='',
        files=[],
        user={'id': '42', 'first_name': 'Worker'},
        role='owner',
        idempotency_key='finish-key',
    ))

    assert result['id'] == 'session-1'
    assert result['finish_at'] == 2
    assert sessions == [finished]
    assert len(save_calls) == 1


def test_raw_idempotency_save_is_confined_to_after_commit_helper():
    import routes.checkin as checkin_routes

    source = inspect.getsource(checkin_routes.create_checkin_router)
    assert source.count('deps.idempotency_save(') == 1
    assert source.count('_save_idempotency_response_after_commit(') == 5
