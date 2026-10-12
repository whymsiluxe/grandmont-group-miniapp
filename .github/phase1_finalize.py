from pathlib import Path
import re

workforce = Path('tests/test_core_workforce_shadow.py')
text = workforce.read_text()
old = '''        route = next(\n            route for route in backend.app.routes\n            if getattr(route, 'path', None) == '/api/abwesenheit'\n            and 'POST' in getattr(route, 'methods', set())\n        )\n        assert route.endpoint is backend.create_abwesenheit\n        source = inspect.getsource(route.endpoint)\n        assert 'shadow_create_absence' in source\n        tree = ast.parse(source)\n        found_try_guard = any(\n            isinstance(node, ast.Try) and 'shadow_create_absence' in ast.dump(node)\n            for node in ast.walk(tree)\n        )\n        assert found_try_guard, "registered create_abwesenheit route must guard shadow_create_absence"'''
new = '''        import textwrap\n\n        source = textwrap.dedent(inspect.getsource(backend.create_abwesenheit))\n        assert 'shadow_create_absence' in source\n        tree = ast.parse(source)\n        found_try_guard = any(\n            isinstance(node, ast.Try) and 'shadow_create_absence' in ast.dump(node)\n            for node in ast.walk(tree)\n        )\n        assert found_try_guard, "create_abwesenheit must guard shadow_create_absence"'''
if old not in text:
    raise SystemExit('workforce block not found')
workforce.write_text(text.replace(old, new, 1))

for filename, class_name in [
    ('tests/test_release_audit2.py', 'AbwesenheitRedactionTests'),
    ('tests/test_release_audit3.py', 'AbwesenheitReasonRedactionTests'),
]:
    path = Path(filename)
    text = path.read_text()
    text = text.replace("'date_from': '2026-08-01',", "'date_from': '2099-08-01',")
    text = text.replace("'date_to': '2026-08-05',", "'date_to': '2099-08-05',")
    class_pos = text.index(f'class {class_name}(unittest.TestCase):\n')
    first_test = text.index('    def test_', class_pos)
    helper = '''    def _list_from_real_temp_store(self, user, role):\n        with tempfile.TemporaryDirectory() as td, \\\n             patch.object(backend, 'ABWESENHEIT_FILE', os.path.join(td, 'abwesenheit.json')):\n            backend._save_abwesenheit([dict(self.FULL_ENTRY)])\n            return backend.list_all_abwesenheit(user=user, role=role)\n\n'''
    if '_list_from_real_temp_store' not in text[class_pos:first_test]:
        text = text[:first_test] + helper + text[first_test:]
    pattern = re.compile(
        r"        with patch\.object\(backend, '_load_abwesenheit', return_value=\[dict\(self\.FULL_ENTRY\)\]\), \\\n"
        r"             patch\.object\(backend, '_auto_close_expired_open_ended_abwesenheit'\):\n"
        r"            result = backend\.list_all_abwesenheit\(user=(WORKER_A|WORKER_B|OWNER), role='(worker|owner)'\)"
    )
    text, count = pattern.subn(
        lambda m: f"        result = self._list_from_real_temp_store(user={m.group(1)}, role='{m.group(2)}')",
        text,
    )
    if count != 3:
        raise SystemExit(f'{filename}: replaced {count} redaction blocks, expected 3')
    path.write_text(text)

Path('tests/test_phase1_route_contract.py').write_text(r'''"""Contract guard for domains extracted from backend/main.py in Phase 1."""
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
''')

for rel in [
    '.github/workflows/phase1-test-hardening-once.yml',
    '.github/workflows/phase1-route-debug.yml',
    '.github/workflows/phase1-finalize-once.yml',
    '.github/workflows/phase1-finalize-driver.yml',
    '.github/phase1_finalize.py',
]:
    path = Path(rel)
    if path.exists():
        path.unlink()
