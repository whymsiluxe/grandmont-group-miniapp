"""Phase 23 regressions for check-in AI analysis persistence."""
from __future__ import annotations

import inspect
import os
import sys
import threading
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.execution import ExecutionRouteDeps, create_execution_router  # noqa: E402


class TrackingLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held = False
        self._lock.release()


def _build_handlers(initial_sessions):
    lock = TrackingLock()
    sessions = deepcopy(initial_sessions)

    def load_meta():
        assert lock.held, 'analysis read must be serialized by the shared check-in lock'
        return deepcopy(sessions)

    def save_meta(items):
        assert lock.held, 'analysis write must be serialized by the shared check-in lock'
        sessions[:] = deepcopy(items)

    deps = ExecutionRouteDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'owner',
        checkin_lock=lock,
        load_checkin_meta=load_meta,
        save_checkin_meta=save_meta,
        idempotency_scope=lambda *args, **kwargs: {},
        idempotency_claim=lambda *args, **kwargs: None,
        idempotency_save=lambda *args, **kwargs: None,
        idempotency_release=lambda *args, **kwargs: None,
        idempotent_entity_id=lambda *args, **kwargs: 'manual-1',
        write_zeiterfassung_row=lambda *args, **kwargs: None,
        cached_get_used_range=lambda *args, **kwargs: [],
        load_assignments=lambda: {},
        load_roles=lambda: {'42': 'owner'},
        has_active_object_access=lambda *args, **kwargs: True,
        business_now=lambda: datetime.now(timezone.utc),
        business_today_str=lambda: datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        checkin_photo_base=lambda: '/tmp/checkin-test',
        check_ai_rate=lambda *args, **kwargs: None,
        create_mangel_ticket=lambda **kwargs: None,
    )
    _router, handlers = create_execution_router(deps)
    return handlers, sessions, lock


def test_analysis_save_uses_shared_checkin_lock_and_preserves_fresh_session_fields():
    initial = [{
        'id': 'session-1',
        'object_id': 'OBJ-1',
        'user_id': '42',
        'start_at': 100,
        'finish_at': 200,
        'finish_photos': ['finish.jpg'],
        'done_summary': 'fresh finish data',
        'analysis': {'materials': 'existing material estimate'},
    }]
    handlers, sessions, lock = _build_handlers(initial)

    handlers.save_checkin_analysis('session-1', 'progress', 'new progress result')

    assert lock.held is False
    assert sessions[0]['finish_at'] == 200
    assert sessions[0]['done_summary'] == 'fresh finish data'
    assert sessions[0]['analysis'] == {
        'materials': 'existing material estimate',
        'progress': 'new progress result',
    }


def test_analysis_save_missing_session_still_returns_404_after_locking():
    handlers, _sessions, lock = _build_handlers([])

    with pytest.raises(HTTPException) as exc:
        handlers.save_checkin_analysis('missing', 'progress', 'result')

    assert exc.value.status_code == 404
    assert lock.held is False


def test_analysis_read_modify_write_is_structurally_inside_shared_lock():
    import routes.execution as execution_routes

    source = inspect.getsource(execution_routes.create_execution_router)
    block = source.split('    def _save_checkin_analysis', 1)[1].split('    @router.post', 1)[0]
    assert 'with deps.checkin_lock:' in block
    assert block.index('with deps.checkin_lock:') < block.index('deps.load_checkin_meta()')
    assert block.index('deps.load_checkin_meta()') < block.index('deps.save_checkin_meta(items)')
