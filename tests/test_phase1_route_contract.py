"""Contract guard for domains extracted from backend/main.py in Phase 1."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402

PROBE = r"""
import sys
sys.path.insert(0, {backend_dir!r})
import main as backend

def iter_routes(app_or_router):
    for route in app_or_router.routes:
        nested = getattr(route, 'original_router', None)
        if nested is not None:
            yield from iter_routes(nested)
        else:
            yield route

routes = list(iter_routes(backend.app))
ROUTES = [
    ('GET', '/api/tools', 'list_tools', ('user',)),
    ('GET', '/api/tools/{{serial}}/history', 'tool_history', ('user',)),
    ('GET', '/api/tools/bookings', 'list_tool_bookings', ('user',)),
    ('POST', '/api/tools/{{serial}}/bookings', 'create_tool_booking', ('user', 'role')),
    ('DELETE', '/api/tools/{{serial}}/bookings/{{booking_id}}', 'cancel_tool_booking', ('user', 'role')),
    ('PATCH', '/api/tools/{{serial}}/checkout', 'checkout_tool', ('user',)),
    ('PATCH', '/api/tools/{{serial}}/return', 'return_tool', ('user', 'role')),
    ('PATCH', '/api/tools/{{serial}}', 'update_tool', ('user', 'owner')),
    ('POST', '/api/tools', 'create_tool', ('user', 'owner')),
    ('POST', '/api/abwesenheit', 'create_abwesenheit', ('user',)),
    ('PATCH', '/api/abwesenheit/{{entry_id}}/close', 'close_abwesenheit', ('user', 'role')),
    ('PATCH', '/api/abwesenheit/{{entry_id}}/status', 'update_abwesenheit_status', ('user', 'owner')),
    ('PATCH', '/api/abwesenheit/{{entry_id}}', 'update_abwesenheit_dates', ('user', 'role')),
    ('GET', '/api/abwesenheit', 'list_my_abwesenheit', ('user',)),
    ('GET', '/api/abwesenheit/all', 'list_all_abwesenheit', ('user', 'role')),
    ('DELETE', '/api/abwesenheit/{{entry_id}}', 'delete_abwesenheit', ('user', 'role')),
]
dependency_map = {{'user': backend.get_current_user, 'role': backend.get_role, 'owner': backend.require_owner}}
for method, path, handler_name, dependency_keys in ROUTES:
    matches = [r for r in routes if getattr(r, 'path', None) == path and method in getattr(r, 'methods', set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    handler = getattr(backend, handler_name)
    assert route.endpoint is handler, (method, path)
    assert route.name == handler_name, (method, path, route.name, handler_name)
    actual = {{d.call for d in route.dependant.dependencies}}
    expected = {{dependency_map[key] for key in dependency_keys}}
    assert expected <= actual, (method, path)
extracted = {{(method, path) for method, path, *_ in ROUTES}}
seen = {{}}
for route in routes:
    for method in getattr(route, 'methods', set()):
        key = (method, getattr(route, 'path', None))
        if key in extracted:
            seen[key] = seen.get(key, 0) + 1
assert seen == {{key: 1 for key in extracted}}, seen
print('phase1-route-contract-ok', len(ROUTES))
"""

def test_phase1_route_contract_in_clean_process():
    code = PROBE.format(backend_dir=os.path.dirname(backend.__file__))
    result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True, env=os.environ.copy(), check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'phase1-route-contract-ok 16' in result.stdout
