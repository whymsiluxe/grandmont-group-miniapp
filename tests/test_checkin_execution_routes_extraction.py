"""Regression coverage for the checkin/execution router split.

Checkin shift-lifecycle routes (start/pause/finish/list/export/finish-context/
photo) went to routes/checkin.py; manual time entry + AI photo analysis went
to routes/execution.py -- different coupling profile per the Execution
dependency-map decision (no lock/idempotency/outbox involvement beyond a
simple lock+idempotency pair for manual entry).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


CHECKIN_ROUTE_KEYS = {
    ('POST', '/api/checkin/start'),
    ('POST', '/api/checkin/{session_id}/pause'),
    ('POST', '/api/checkin/{session_id}/finish'),
    ('GET', '/api/checkin/stundenzettel'),
    ('GET', '/api/checkin'),
    ('GET', '/api/checkin/{session_id}/finish-context'),
    ('GET', '/api/checkin/{session_id}/photo/{which}/{index}'),
}

EXECUTION_ROUTE_KEYS = {
    ('POST', '/api/checkin/manual'),
    ('POST', '/api/checkin/{session_id}/analyze-progress'),
    ('POST', '/api/checkin/{session_id}/analyze-materials'),
    ('POST', '/api/checkin/{session_id}/analyze-defects'),
}

# Physically interleaved with the old checkin block in main.py but a
# different domain (worker profile/absence stats) -- must stay in main.py,
# not accidentally swept into either new router.
WORKER_CALENDAR_ROUTE_KEYS = {
    ('GET', '/api/workers/{target_user_id}/calendar'),
    ('GET', '/api/workers/{target_user_id}/calendar-stats'),
}


def _route_rows():
    for route in iter_app_routes(backend.app):
        for method in sorted(getattr(route, 'methods', []) or []):
            if method in {'GET', 'POST', 'PATCH', 'DELETE'}:
                yield method, getattr(route, 'path', ''), route


def test_checkin_domain_routes_are_registered_once_and_owned_by_routes_module():
    rows = list(_route_rows())
    keys = [(method, path) for method, path, _ in rows]

    for key in CHECKIN_ROUTE_KEYS:
        assert keys.count(key) == 1, key
        route = next(route for method, path, route in rows if (method, path) == key)
        assert route.endpoint.__module__.endswith('routes.checkin')


def test_execution_domain_routes_are_registered_once_and_owned_by_routes_module():
    rows = list(_route_rows())
    keys = [(method, path) for method, path, _ in rows]

    for key in EXECUTION_ROUTE_KEYS:
        assert keys.count(key) == 1, key
        route = next(route for method, path, route in rows if (method, path) == key)
        assert route.endpoint.__module__.endswith('routes.execution')


def test_worker_calendar_routes_stayed_in_main_not_swept_into_either_router():
    rows = list(_route_rows())
    by_key = {(m, p): r for m, p, r in rows}
    for key in WORKER_CALENDAR_ROUTE_KEYS:
        assert key in by_key, key
        assert by_key[key].endpoint.__module__ == 'main'


def test_no_duplicate_routes_anywhere_in_the_app():
    rows = list(_route_rows())
    keys = [(method, path) for method, path, _ in rows]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert duplicates == []


def test_route_count_invariant():
    # 186 -> 191: routes/service_bridge.py added 5 GET/POST routes for the
    # CRM Worker Operations integration (none of it was on main before this
    # branch): GET /api/bridge/daily-plan, POST /api/bridge/daily-plan/{id}/
    # accept, POST /api/bridge/daily-plan/{id}/amendments/{id}/accept (the
    # DailyPlan/acceptance bridge), plus GET /api/bridge/shift/active and
    # GET /api/bridge/shift/history (item 4's read-only canonical shift
    # bridge -- CRM never starts/stops a shift). All five are registered
    # exactly once via the single app.include_router(_service_bridge_router)
    # call in main.py.
    # 191 -> 193: routes/auth.py added 2 POST routes for the worker
    # onboarding invite flow: POST /api/invite/consume (Flow A, CRM-first --
    # consume an opaque invite token from a t.me deep-link) and POST
    # /api/onboarding/request (Flow B, Miniapp-first -- create a pending
    # onboarding request for CRM admin review). Both depend on
    # get_verified_telegram_user, not get_current_user, since the calling
    # Telegram user is by definition not in roles.json yet.
    # Bump this number again, with the same explanation discipline, the next
    # time a route is added or removed.
    assert len(list(iter_app_routes(backend.app))) == 193


def test_checkin_router_has_no_main_import():
    src = Path(backend.BACKEND_DIR, 'routes', 'checkin.py').read_text()
    assert 'import main' not in src
    assert 'from main import' not in src


def test_execution_router_has_no_main_import():
    src = Path(backend.BACKEND_DIR, 'routes', 'execution.py').read_text()
    assert 'import main' not in src
    assert 'from main import' not in src


def test_main_reexports_legacy_checkin_handlers_from_router():
    assert backend.checkin_start.__module__.endswith('routes.checkin')
    assert backend.checkin_pause.__module__.endswith('routes.checkin')
    assert backend.checkin_finish.__module__.endswith('routes.checkin')
    assert backend.list_checkins.__module__.endswith('routes.checkin')
    assert backend.export_stundenzettel.__module__.endswith('routes.checkin')
    assert backend.checkin_finish_context.__module__.endswith('routes.checkin')
    assert backend.get_checkin_photo.__module__.endswith('routes.checkin')


def test_main_reexports_legacy_execution_handlers_from_router():
    assert backend.checkin_manual.__module__.endswith('routes.execution')
    assert backend.analyze_checkin_progress.__module__.endswith('routes.execution')
    assert backend.analyze_checkin_materials.__module__.endswith('routes.execution')
    assert backend.analyze_checkin_defects.__module__.endswith('routes.execution')


def test_auth_objects_stages_daily_plan_routes_are_untouched():
    rows = list(_route_rows())
    by_key = {(m, p): r for m, p, r in rows}
    assert by_key[('GET', '/api/roles')].endpoint.__module__.endswith('routes.auth')
    assert by_key[('GET', '/api/objects')].endpoint.__module__.endswith('routes.objects')
    assert by_key[('GET', '/api/daily-plan/today')].endpoint.__module__.endswith('routes.daily_plan')
