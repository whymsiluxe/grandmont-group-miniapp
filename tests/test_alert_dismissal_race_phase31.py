"""Phase 31 regressions for alert-dismissal lost updates."""
from __future__ import annotations

import inspect
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.alerts as alerts_routes  # noqa: E402
from routes.alerts import AlertDismissBody, AlertsDeps, create_alerts_router  # noqa: E402


def _unexpected_split_io(*_args, **_kwargs):
    raise AssertionError('dismiss_alerts must not use split load/save persistence')


def _build_handlers(tmp_path):
    dismissals_file = tmp_path / 'alert_dismissals.json'
    deps = AlertsDeps(
        get_current_user=lambda: {'id': '1'},
        get_role=lambda: 'owner',
        cached_get_used_range=lambda _name: [],
        load_repo_objekte_lib=lambda: None,
        load_repo_tools_lib=lambda: None,
        load_assignments=lambda: {},
        load_abwesenheit=lambda: [],
        assignment_confirmation_alerts=lambda: [],
        overdue_task_alerts=lambda: [],
        load_critical_alerts=lambda: [],
        load_activity_alerts=lambda: [],
        safe_load_json=_unexpected_split_io,
        atomic_write_json=_unexpected_split_io,
        alert_dismissals_file=lambda: str(dismissals_file),
        alert_dismiss_ttl=3600,
    )
    _router, handlers = create_alerts_router(deps)
    return handlers, dismissals_file


def test_concurrent_dismissals_preserve_every_user_and_alert(tmp_path):
    handlers, dismissals_file = _build_handlers(tmp_path)

    def dismiss(index: int):
        return handlers.dismiss_alerts(
            AlertDismissBody(alert_ids=[f'alert-{index}', f'shared-{index % 3}']),
            user={'id': str(1000 + index % 7)},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(dismiss, range(60)))

    assert all(result == {'ok': True} for result in results)
    data = json.loads(dismissals_file.read_text(encoding='utf-8'))

    for index in range(60):
        uid = str(1000 + index % 7)
        assert f'alert-{index}' in data[uid]
        assert f'shared-{index % 3}' in data[uid]


def test_dismiss_handler_uses_single_storage_transaction_structurally():
    source = inspect.getsource(alerts_routes.create_alerts_router)
    block = source.split('    def dismiss_alerts', 1)[1].split('    return router, AlertsHandlers', 1)[0]

    assert 'update_json_transaction(deps.alert_dismissals_file(), {}, _mutate)' in block
    assert 'data = load_alert_dismissals()' not in block
    assert 'save_alert_dismissals(data)' not in block
