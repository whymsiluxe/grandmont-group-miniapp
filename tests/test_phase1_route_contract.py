"""Strict contract/architecture guards for domains extracted from backend/main.py."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402

PROBE = r'''
import ast
import inspect
import os
import sys
import types
from typing import get_args, get_origin

sys.path.insert(0, os.environ['PHASE1_BACKEND_DIR'])
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
    return [
        (field.name, type_name(field.type_), bool(field.required), '<required>' if field.required else field.default)
        for field in fields
    ]


def annotation_name(annotation):
    if annotation is str:
        return 'str'
    if annotation is type(None):
        return 'None'
    origin = get_origin(annotation)
    if origin is types.UnionType or get_args(annotation):
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
dependency_map = {'user': backend.get_current_user, 'role': backend.get_role, 'owner': backend.require_owner}

# Exact public route manifest copied from the pre-extraction signatures at base
# 9e67e4298a31f6c1e1a9bdb31a21d431d4aab68d.
ROUTES = [
    ('GET', '/api/tools', 'list_tools', 'list_tools_api_tools_get', [('user','user')], [], [], []),
    ('GET', '/api/tools/{serial}/history', 'tool_history', 'tool_history_api_tools__serial__history_get', [('user','user')], [('serial','str',True,'<required>')], [], []),
    ('GET', '/api/tools/bookings', 'list_tool_bookings', 'list_tool_bookings_api_tools_bookings_get', [('user','user')], [], [('serial','str',False,''),('date_from','str',False,''),('date_to','str',False,'')], []),
    ('POST', '/api/tools/{serial}/bookings', 'create_tool_booking', 'create_tool_booking_api_tools__serial__bookings_post', [('user','user'),('role','role')], [('serial','str',True,'<required>')], [], [('body','ToolBookingBody',True,'<required>')]),
    ('DELETE', '/api/tools/{serial}/bookings/{booking_id}', 'cancel_tool_booking', 'cancel_tool_booking_api_tools__serial__bookings__booking_id__delete', [('user','user'),('role','role')], [('serial','str',True,'<required>'),('booking_id','str',True,'<required>')], [], []),
    ('PATCH', '/api/tools/{serial}/checkout', 'checkout_tool', 'checkout_tool_api_tools__serial__checkout_patch', [('user','user')], [('serial','str',True,'<required>')], [], [('body','CheckoutBody',True,'<required>')]),
    ('PATCH', '/api/tools/{serial}/return', 'return_tool', 'return_tool_api_tools__serial__return_patch', [('user','user'),('role','role')], [('serial','str',True,'<required>')], [], []),
    ('PATCH', '/api/tools/{serial}', 'update_tool', 'update_tool_api_tools__serial__patch', [('user','user'),('_','owner')], [('serial','str',True,'<required>')], [], [('body','ToolUpdateBody',True,'<required>')]),
    ('POST', '/api/tools', 'create_tool', 'create_tool_api_tools_post', [('user','user'),('_','owner')], [], [], [('body','NewToolBody',True,'<required>')]),
    ('POST', '/api/abwesenheit', 'create_abwesenheit', 'create_abwesenheit_api_abwesenheit_post', [('user','user')], [], [], [('body','AbwesenheitBody',True,'<required>')]),
    ('PATCH', '/api/abwesenheit/{entry_id}/close', 'close_abwesenheit', 'close_abwesenheit_api_abwesenheit__entry_id__close_patch', [('user','user'),('role','role')], [('entry_id','str',True,'<required>')], [], []),
    ('PATCH', '/api/abwesenheit/{entry_id}/status', 'update_abwesenheit_status', 'update_abwesenheit_status_api_abwesenheit__entry_id__status_patch', [('user','user'),('_','owner')], [('entry_id','str',True,'<required>')], [], [('body','AbwesenheitStatusBody',True,'<required>')]),
    ('PATCH', '/api/abwesenheit/{entry_id}', 'update_abwesenheit_dates', 'update_abwesenheit_dates_api_abwesenheit__entry_id__patch', [('user','user'),('role','role')], [('entry_id','str',True,'<required>')], [], [('body','AbwesenheitMoveBody',True,'<required>')]),
    ('GET', '/api/abwesenheit', 'list_my_abwesenheit', 'list_my_abwesenheit_api_abwesenheit_get', [('user','user')], [], [], []),
    ('GET', '/api/abwesenheit/all', 'list_all_abwesenheit', 'list_all_abwesenheit_api_abwesenheit_all_get', [('user','user'),('role','role')], [], [], []),
    ('DELETE', '/api/abwesenheit/{entry_id}', 'delete_abwesenheit', 'delete_abwesenheit_api_abwesenheit__entry_id__delete', [('user','user'),('role','role')], [('entry_id','str',True,'<required>')], [], []),
]

for method, path, handler_name, operation_id, deps, path_params, query_params, body_params in ROUTES:
    matches = [r for r in routes if getattr(r, 'path', None) == path and method in getattr(r, 'methods', set())]
    assert len(matches) == 1, (method, path, len(matches))
    route = matches[0]
    handler = getattr(backend, handler_name)
    assert route.endpoint is handler, (method, path, 'endpoint')
    assert route.name == handler_name, (method, path, route.name)
    assert route.methods == {method}, (method, path, route.methods)
    assert route.unique_id == operation_id, (method, path, route.unique_id)
    assert route.status_code is None and route.response_model is None and route.responses == {}, (method, path)
    actual_deps = [(d.name, next(k for k,v in dependency_map.items() if d.call is v)) for d in route.dependant.dependencies]
    assert actual_deps == deps, (method, path, actual_deps, deps)
    assert field_contract(route.dependant.path_params) == path_params, (method, path, 'path')
    assert field_contract(route.dependant.query_params) == query_params, (method, path, 'query')
    assert field_contract(route.dependant.body_params) == body_params, (method, path, 'body')
    operation = backend.app.openapi()['paths'][path][method.lower()]
    assert operation['operationId'] == operation_id, (method, path, operation['operationId'])
    if body_params:
        model_name = body_params[0][1]
        request_body = operation['requestBody']
        assert request_body.get('required') is True, (method, path, request_body)
        assert request_body['content']['application/json']['schema'] == {'$ref': f'#/components/schemas/{model_name}'}, (method, path)
    else:
        assert 'requestBody' not in operation, (method, path)

extracted = {(m,p) for m,p,*_ in ROUTES}
seen = {}
for route in routes:
    for method in getattr(route, 'methods', set()):
        key = (method, getattr(route, 'path', None))
        if key in extracted:
            seen[key] = seen.get(key, 0) + 1
assert seen == {key: 1 for key in extracted}, seen

MODEL_CONTRACTS = {
    backend.ToolBookingBody: {'date_from':('str',True,'<required>'),'date_to':('str',True,'<required>'),'object_name':('str',True,'<required>'),'holder_id':('str',False,''),'holder':('str',False,''),'note':('str',False,'')},
    backend.CheckoutBody: {'object_name':('str',True,'<required>'),'holder':('str',False,'')},
    backend.ToolUpdateBody: {'status':('str',True,'<required>'),'holder':('str',False,''),'object_name':('str',False,''),'holder_id':('str',False,'')},
    backend.NewToolBody: {'name':('str',True,'<required>'),'category':('str',True,'<required>')},
    backend.AbwesenheitBody: {'date_from':('str',True,'<required>'),'date_to':('None|str',False,None),'reason':('str',True,'<required>'),'note':('str',False,''),'start_time':('None|str',False,None),'end_time':('None|str',False,None)},
    backend.AbwesenheitStatusBody: {'status':('str',True,'<required>')},
    backend.AbwesenheitMoveBody: {'date_from':('str',True,'<required>'),'date_to':('None|str',False,None)},
}
for model, expected in MODEL_CONTRACTS.items():
    assert model_contract(model) == expected, (model.__name__, model_contract(model), expected)


def assert_no_main_import(module):
    tree = ast.parse(inspect.getsource(module))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(a.name for a in node.names if a.name == 'main' or a.name.endswith('.main'))
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

main_tree = ast.parse(inspect.getsource(backend))
for node in ast.walk(main_tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.func, ast.Attribute) and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == 'app':
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    assert not first.value.startswith('/api/tools'), (node.name, first.value)
                    assert not first.value.startswith('/api/abwesenheit'), (node.name, first.value)

# Prove instance isolation: creating a second absence router cannot replace the first one's deps.
def make_absence_deps(tag):
    return absence_routes.AbsenceRouteDeps(
        get_current_user=lambda: {'id': 1}, get_role=lambda: 'worker', require_owner=lambda: None,
        absence_file=lambda: tag, reasons=(), public_fields=(),
        safe_load_json=lambda path, default: [{'source': path}], atomic_write_json=lambda path, value: None,
        update_json_transaction=lambda path, default, mutator: mutator([]), load_roles=lambda: {},
        send_telegram_message=lambda *args, **kwargs: None, business_today_str=lambda: '2099-01-01',
        shadow_create_absence=lambda *args, **kwargs: None, create_critical_alert=lambda *args, **kwargs: None,
        resolve_current_display_name=lambda user_id, fallback: fallback,
    )

_, one = absence_routes.create_absence_router(make_absence_deps('one'))
_, two = absence_routes.create_absence_router(make_absence_deps('two'))
assert one.load_abwesenheit() == [{'source':'one'}]
assert two.load_abwesenheit() == [{'source':'two'}]
assert one.load_abwesenheit() == [{'source':'one'}]

print('phase1-strict-contract-ok', len(ROUTES), len(MODEL_CONTRACTS))
'''


def test_phase1_strict_contract_in_clean_process():
    env = os.environ.copy()
    env['PHASE1_BACKEND_DIR'] = os.path.dirname(backend.__file__)
    result = subprocess.run([sys.executable, '-c', PROBE], text=True, capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'phase1-strict-contract-ok 16 7' in result.stdout
