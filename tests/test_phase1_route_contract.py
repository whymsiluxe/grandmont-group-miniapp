"""Contract guard for domains extracted from backend/main.py in Phase 1."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402


def _route(path: str, method: str):
    matches = [
        route for route in backend.app.routes
        if getattr(route, 'path', None) == path
        and method in getattr(route, 'methods', set())
    ]
    assert len(matches) == 1, f"expected exactly one {method} {path}, got {len(matches)}"
    return matches[0]


def _dependency_calls(route):
    return {dependency.call for dependency in route.dependant.dependencies}


ROUTES = [
    ('GET', '/api/tools', 'list_tools', ('user',)),
    ('GET', '/api/tools/{serial}/history', 'tool_history', ('user',)),
    ('GET', '/api/tools/bookings', 'list_tool_bookings', ('user',)),
    ('POST', '/api/tools/{serial}/bookings', 'create_tool_booking', ('user', 'role')),
    ('DELETE', '/api/tools/{serial}/bookings/{booking_id}', 'cancel_tool_booking', ('user', 'role')),
    ('PATCH', '/api/tools/{serial}/checkout', 'checkout_tool', ('user',)),
    ('PATCH', '/api/tools/{serial}/return', 'return_tool', ('user', 'role')),
    ('PATCH', '/api/tools/{serial}', 'update_tool', ('user', 'owner')),
    ('POST', '/api/tools', 'create_tool', ('user', 'owner')),
    ('POST', '/api/abwesenheit', 'create_abwesenheit', ('user',)),
    ('PATCH', '/api/abwesenheit/{entry_id}/close', 'close_abwesenheit', ('user', 'role')),
    ('PATCH', '/api/abwesenheit/{entry_id}/status', 'update_abwesenheit_status', ('user', 'owner')),
    ('PATCH', '/api/abwesenheit/{entry_id}', 'update_abwesenheit_dates', ('user', 'role')),
    ('GET', '/api/abwesenheit', 'list_my_abwesenheit', ('user',)),
    ('GET', '/api/abwesenheit/all', 'list_all_abwesenheit', ('user', 'role')),
    ('DELETE', '/api/abwesenheit/{entry_id}', 'delete_abwesenheit', ('user', 'role')),
]


def test_phase1_routes_keep_method_path_name_handler_and_auth_contract():
    dependency_map = {
        'user': backend.get_current_user,
        'role': backend.get_role,
        'owner': backend.require_owner,
    }
    for method, path, handler_name, dependency_keys in ROUTES:
        route = _route(path, method)
        handler = getattr(backend, handler_name)
        assert route.endpoint is handler
        assert route.name == handler_name
        actual_dependencies = _dependency_calls(route)
        expected_dependencies = {dependency_map[key] for key in dependency_keys}
        assert expected_dependencies <= actual_dependencies, (
            method, path, expected_dependencies, actual_dependencies
        )


def test_phase1_routes_have_no_duplicate_method_path_pairs():
    extracted = {(method, path) for method, path, *_ in ROUTES}
    seen = {}
    for route in backend.app.routes:
        path = getattr(route, 'path', None)
        for method in getattr(route, 'methods', set()):
            key = (method, path)
            if key in extracted:
                seen[key] = seen.get(key, 0) + 1
    assert seen == {key: 1 for key in extracted}
