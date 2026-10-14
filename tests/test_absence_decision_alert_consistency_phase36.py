"""Phase 36 regressions for absence decision post-commit side effects."""
from __future__ import annotations

import os
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.absence import (  # noqa: E402
    AbsenceRouteDeps,
    AbwesenheitStatusBody,
    create_absence_router,
)


def _build_handlers(*, alert_error=None, transaction_error=None):
    state = [{
        'id': 'absence-1',
        'user_id': '42',
        'name': 'Worker',
        'date_from': '2026-10-14',
        'date_to': '2026-10-14',
        'open_ended': False,
        'reason': 'Urlaub',
        'note': '',
        'status': 'pending',
    }]

    def update_json_transaction(_path, _default, mutator):
        nonlocal state
        if transaction_error is not None:
            raise transaction_error
        working = deepcopy(state)
        result = mutator(working)
        state = working
        return result

    def create_critical_alert(**_kwargs):
        if alert_error is not None:
            raise alert_error
        return {'id': 'alert-1'}

    deps = AbsenceRouteDeps(
        get_current_user=lambda: {'id': 'owner'},
        get_role=lambda: 'owner',
        require_owner=lambda: None,
        absence_file=lambda: '/tmp/absence-phase36.json',
        reasons={'Urlaub'},
        public_fields={'id', 'date_from', 'date_to', 'reason', 'status'},
        safe_load_json=lambda _path, default: deepcopy(state) if state else default,
        atomic_write_json=lambda _path, _value: None,
        update_json_transaction=update_json_transaction,
        load_roles=lambda: {'owner': 'owner', '42': 'worker'},
        send_telegram_message=lambda *_args, **_kwargs: None,
        business_today_str=lambda: '2026-10-14',
        shadow_create_absence=lambda *_args, **_kwargs: None,
        create_critical_alert=create_critical_alert,
        resolve_current_display_name=lambda _uid, fallback: fallback,
    )
    _router, handlers = create_absence_router(deps)
    return handlers, lambda: deepcopy(state)


def test_decision_returns_committed_status_when_critical_alert_fails(caplog):
    handlers, state = _build_handlers(
        alert_error=OSError('simulated critical alert storage failure')
    )

    result = handlers.update_abwesenheit_status(
        'absence-1',
        AbwesenheitStatusBody(status='approved'),
        user={'id': 'owner', 'first_name': 'Owner'},
        _=None,
    )

    assert result['status'] == 'approved'
    assert state()[0]['status'] == 'approved'
    assert 'Failed to create critical alert after absence decision commit' in caplog.text


def test_decision_still_propagates_primary_transaction_failure():
    handlers, state = _build_handlers(
        transaction_error=OSError('simulated absence storage failure')
    )

    with pytest.raises(OSError, match='simulated absence storage failure'):
        handlers.update_abwesenheit_status(
            'absence-1',
            AbwesenheitStatusBody(status='approved'),
            user={'id': 'owner', 'first_name': 'Owner'},
            _=None,
        )

    assert state()[0]['status'] == 'pending'
