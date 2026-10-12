"""Strict contract/architecture guards for domains extracted from backend/main.py."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


PROBE = r'''
import ast
import inspect
import sys
import types
from typing import get_args, get_origin

sys.path.insert(0, {backend_dir!r})
import main as backend
import routes.absence as absence_routes
import routes.tools as tools_routes


def iter_routes(app_or_router):
    for route in app_or_router.routes:
        nested = getattr(route, 'original_router', None)
        if nested is not None:
            yield from iter_routes(nested)
        else:
            yield route


def type_name(value):
    return getattr(value, '__name__', str(value))


def field_contract(fields):
    result = []
    for field in fields:
        result.append((
            field.name,
            type_name(field.type_),
            bool(field.required),
            '<required>' if field.required else field.default,
        ))
    return result


def annotation_name(annotation):
    if annotation is str:
        return 'str'
    if annotation is int:
        return 'int'
    if annotation is bool:
        return 'bool'
    if annotation is type(None):
        return 'None'
    origin = get_origin(annotation)
    if origin in (types.UnionType,):
        return '|'.join(sorted(annotation_name(arg) for arg in get_args(annotation)))
    # typing.Optional/typing.Union on Python versions where the origin is typing.Union
    args = get_args(annotation)
    if args:
        return '|'.join(sorted(annotation_name(arg) for arg in args))
    return type_name(annotation)


def model_contract(model):
    return {
        name: (
            annotation_name(field.annotation),
            bool(field.is_required()),
            '<required>' if field.is_required() else field.default,
        )
        for name, field in model.model_fields.items()
    }


routes = list(iter_routes(backend.app))
dependency_map = {
    'user': backend.get_current_user,
    'role': backend.get_role,
    'owner': backend.require_owner,
}

# This manifest is copied from the pre-extraction public signatures in base
# 9e67e4298a31f6c1e1a9bdb31a21d431d4aab68d.  It is intentionally exact: an
# extra dependency, parameter, request model change, route rename or duplicate
# registration must fail the guard rather than silently widening the contract.
ROUTES = [
    {
        'method': 'GET', 'path': '/api/tools', 'handler': 'list_tools',
        'operation_id': 'list_tools_api_tools_get',
        'deps': [('user', 'user')], 'path_params': [], 'query_params': [], 'body': [],
    },
    {
        'method': 'GET', 'path': '/api/tools/{serial}/history', 'handler': 'tool_history',
        'operation_id': 'tool_history_api_tools__serial__history_get',
        'deps': [('user', 'user')],
        'path_params': [('serial', 'str', True, '<required>')], 'query_params': [], 'body': [],
    },
    {
        'method': 'GET', 'path': '/api/tools/bookings', 'handler': 'list_tool_bookings',
        'operation_id': 'list_tool_bookings_api_tools_bookings_get',
        'deps': [('user', 'user')], 'path_params': [],
        'query_params': [
            ('serial', 'str', False, ''),
            ('date_from', 'str', False, ''),
            ('date_to', 'str', False, ''),
        ], 'body': [],
    },
    {
        'method': 'POST', 'path': '/api/tools/{serial}/bookings', 'handler': 'create_tool_booking',
        'operation_id': 'create_tool_booking_api_tools__serial__bookings_post',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [('serial', 'str', True, '<required>')], 'query_params': [],
        'body': [('body', 'ToolBookingBody', True, '<required>')],
    },
    {
        'method': 'DELETE', 'path': '/api/tools/{serial}/bookings/{booking_id}', 'handler': 'cancel_tool_booking',
        'operation_id': 'cancel_tool_booking_api_tools__serial__bookings__booking_id__delete',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [
            ('serial', 'str', True, '<required>'),
            ('booking_id', 'str', True, '<required>'),
        ], 'query_params': [], 'body': [],
    },
    {
        'method': 'PATCH', 'path': '/api/tools/{serial}/checkout', 'handler': 'checkout_tool',
        'operation_id': 'checkout_tool_api_tools__serial__checkout_patch',
        'deps': [('user', 'user')],
        'path_params': [('serial', 'str', True, '<required>')], 'query_params': [],
        'body': [('body', 'CheckoutBody', True, '<required>')],
    },
    {
        'method': 'PATCH', 'path': '/api/tools/{serial}/return', 'handler': 'return_tool',
        'operation_id': 'return_tool_api_tools__serial__return_patch',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [('serial', 'str', True, '<required>')], 'query_params': [], 'body': [],
    },
    {
        'method': 'PATCH', 'path': '/api/tools/{serial}', 'handler': 'update_tool',
        'operation_id': 'update_tool_api_tools__serial__patch',
        'deps': [('user', 'user'), ('_', 'owner')],
        'path_params': [('serial', 'str', True, '<required>')], 'query_params': [],
        'body': [('body', 'ToolUpdateBody', True, '<required>')],
    },
    {
        'method': 'POST', 'path': '/api/tools', 'handler': 'create_tool',
        'operation_id': 'create_tool_api_tools_post',
        'deps': [('user', 'user'), ('_', 'owner')], 'path_params': [], 'query_params': [],
        'body': [('body', 'NewToolBody', True, '<required>')],
    },
    {
        'method': 'POST', 'path': '/api/abwesenheit', 'handler': 'create_abwesenheit',
        'operation_id': 'create_abwesenheit_api_abwesenheit_post',
        'deps': [('user', 'user')], 'path_params': [], 'query_params': [],
        'body': [('body', 'AbwesenheitBody', True, '<required>')],
    },
    {
        'method': 'PATCH', 'path': '/api/abwesenheit/{entry_id}/close', 'handler': 'close_abwesenheit',
        'operation_id': 'close_abwesenheit_api_abwesenheit__entry_id__close_patch',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [('entry_id', 'str', True, '<required>')], 'query_params': [], 'body': [],
    },
    {
        'method': 'PATCH', 'path': '/api/abwesenheit/{entry_id}/status', 'handler': 'update_abwesenheit_status',
        'operation_id': 'update_abwesenheit_status_api_abwesenheit__entry_id__status_patch',
        'deps': [('user', 'user'), ('_', 'owner')],
        'path_params': [('entry_id', 'str', True, '<required>')], 'query_params': [],
        'body': [('body', 'AbwesenheitStatusBody', True, '<required>')],
    },
    {
        'method': 'PATCH', 'path': '/api/abwesenheit/{entry_id}', 'handler': 'update_abwesenheit_dates',
        'operation_id': 'update_abwesenheit_dates_api_abwesenheit__entry_id__patch',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [('entry_id', 'str', True, '<required>')], 'query_params': [],
        'body': [('body', 'AbwesenheitMoveBody', True, '<required>')],
    },
    {
        'method': 'GET', 'path': '/api/abwesenheit', 'handler': 'list_my_abwesenheit',
        'operation_id': 'list_my_abwesenheit_api_abwesenheit_get',
        'deps': [('user', 'user')], 'path_params': [], 'query_params': [], 'body': [],
    },
    {
        'method': 'GET', 'path': '/api/abwesenheit/all', 'handler': 'list_all_abwesenheit',
        'operation_id': 'list_all_abwesenheit_api_abwesenheit_all_get',
        'deps': [('user', 'user'), ('role', 'role')], 'path_params': [], 'query_params': [], 'body': [],
    },
    {
        'method': 'DELETE', 'path': '/api/abwesenheit/{entry_id}', 'handler': 'delete_abwesenheit',
        'operation_id': 'delete_abwesenheit_api_abwesenheit__entry_id__delete',
        'deps': [('user', 'user'), ('role', 'role')],
        'path_params': [('entry_id', 'str', True, '<required>')], 'query_params': [], 'body': [],
    },
]

for expected in ROUTES:
    method = expected['method']
    path = expected['path']
    matches = [
        route for route in routes
        if getattr(route, 'path', None) == path and method in getattr(route, 'methods', set())
    ]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    handler = getattr(backend, expected['handler'])

    assert route.endpoint is handler, (method, path, 'endpoint identity')
    assert route.name == expected['handler'], (method, path, route.name)
    assert route.methods == {method}, (method, path, route.methods)
    assert route.unique_id == expected['operation_id'], (method, path, route.unique_id)
    assert route.status_code is None, (method, path, route.status_code)
    assert route.response_model is None, (method, path, route.response_model)
    assert route.responses == {}, (method, path, route.responses)
    assert route.include_in_schema is True, (method, path)

    actual_deps = [
        (dependency.name, next(key for key, call in dependency_map.items() if dependency.call is call))
        for dependency in route.dependant.dependencies
    ]
    assert actual_deps == expected['deps'], (method, path, actual_deps, expected['deps'])
    assert field_contract(route.dependant.path_params) == expected['path_params'], (method, path, 'path params')
    assert field_contract(route.dependant.query_params) == expected['query_params'], (method, path, 'query params')
    assert field_contract(route.dependant.body_params) == expected['body'], (method, path, 'body params')

    operation = backend.app.openapi()['paths'][path][method.lower()]
    assert operation['operationId'] == expected['operation_id'], (method, path, operation['operationId'])
    if expected['body']:
        model_name = expected['body'][0][1]
        request_body = operation.get('requestBody')
        assert request_body is not None, (method, path, 'missing requestBody')
        assert request_body.get('required') is True, (method, path, request_body)
        schema = request_body['content']['application/json']['schema']
        assert schema == {'$ref': f'#/components/schemas/{model_name}'}, (method, path, schema)
    else:
        assert 'requestBody' not in operation, (method, path, operation.get('requestBody'))

extracted = {(item['method'], item['path']) for item in ROUTES}
seen = {}
for route in routes:
    for method in getattr(route, 'methods', set()):
        key = (method, getattr(route, 'path', None))
        if key in extracted:
            seen[key] = seen.get(key, 0) + 1
assert seen == {key: 1 for key in extracted}, seen

MODEL_CONTRACTS = {
    backend.ToolBookingBody: {
        'date_from': ('str', True, '<required>'),
        'date_to': ('str', True, '<required>'),
        'object_name': ('str', True, '<required>'),
        'holder_id': ('str', False, ''),
        'holder': ('str', False, ''),
        'note': ('str', False, ''),
    },
    backend.CheckoutBody: {
        'object_name': ('str', True, '<required>'),
        'holder': ('str', False, ''),
    },
    backend.ToolUpdateBody: {
        'status': ('str', True, '<required>'),
        'holder': ('str', False, ''),
        'object_name': ('str', False, ''),
        'holder_id': ('str', False, ''),
    },
    backend.NewToolBody: {
        'name': ('str', True, '<required>'),
        'category': ('str', True, '<required>'),
    },
    backend.AbwesenheitBody: {
        'date_from': ('str', True, '<required>'),
        'date_to': ('None|str', False, None),
        'reason': ('str', True, '<required>'),
        'note': ('str', False, ''),
        'start_time': ('None|str', False, None),
        'end_time': ('None|str', False, None),
    },
    backend.AbwesenheitStatusBody: {
        'status': ('str', True, '<required>'),
    },
    backend.AbwesenheitMoveBody: {
        'date_from': ('str', True, '<required>'),
        'date_to': ('None|str', False, None),
    },
}
for model, expected_contract in MODEL_CONTRACTS.items():
    assert model_contract(model) == expected_contract, (model.__name__, model_contract(model), expected_contract)

# Architecture guard: extracted modules must never depend back on main.py.
def assert_no_main_import(module):
    tree = ast.parse(inspect.getsource(module))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(alias.name for alias in node.names if alias.name == 'main' or alias.name.endswith('.main'))
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ''
            if name == 'main' or name.endswith('.main'):
                offenders.append(name)
    assert not offenders, (module.__name__, offenders)

assert_no_main_import(tools_routes)
assert_no_main_import(absence_routes)
absence_source = inspect.getsource(absence_routes)
assert '_configured_deps' not in absence_source
assert 'compat_' not in absence_source

# main.py may compose/include these routers and re-export aliases, but it must not
# regain direct @app.* endpoint implementations for either extracted prefix.
main_tree = ast.parse(inspect.getsource(backend))
for node in ast.walk(main_tree):
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id != 'app' or func.attr not in {'get', 'post', 'patch', 'delete', 'put'}:
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            assert not first.value.startswith('/api/tools'), (node.name, first.value)
            assert not first.value.startswith('/api/abwesenheit'), (node.name, first.value)

# Explicitly prove that two Absence router instances keep independent dependency
# closures. This fails immediately if someone reintroduces module-global deps.
def make_absence_deps(tag):
    return absence_routes.AbsenceRouteDeps(
        get_current_user=lambda: {'id': 1},
        get_role=lambda: 'worker',
        require_owner=lambda: None,
        absence_file=lambda: tag,
        reasons=(),
        public_fields=(),
        safe_load_json=lambda path, default: [{'source': path}],
        atomic_write_json=lambda path, value: None,
        update_json_transaction=lambda path, default, mutator: mutator([]),
        load_roles=lambda: {},
        send_telegram_message=lambda *args, **kwargs: None,
        business_today_str=lambda: '2099-01-01',
        shadow_create_absence=lambda *args, **kwargs: None,
        create_critical_alert=lambda *args, **kwargs: None,
        resolve_current_display_name=lambda user_id, fallback: fallback,
    )

_, handlers_one = absence_routes.create_absence_router(make_absence_deps('one'))
_, handlers_two = absence_routes.create_absence_router(make_absence_deps('two'))
assert handlers_one.load_abwesenheit() == [{'source': 'one'}]
assert handlers_two.load_abwesenheit() == [{'source': 'two'}]
assert handlers_one.load_abwesenheit() == [{'source': 'one'}]

print('phase1-strict-contract-ok', len(ROUTES), len(MODEL_CONTRACTS))
'''


def test_phase1_strict_contract_in_clean_process():
    code = PROBE.format(backend_dir=os.path.dirname(backend.__file__))
    result = subprocess.run(
        [sys.executable, '-c', code],
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'phase1-strict-contract-ok 16 7' in result.stdout
