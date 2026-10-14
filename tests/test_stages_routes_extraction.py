"""Regression coverage for the stages/roadmap router extraction."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402


STAGE_ROUTE_KEYS = {
    ('GET', '/api/objects/{object_id}/stages'),
    ('POST', '/api/objects/{object_id}/stages'),
    ('PATCH', '/api/objects/{object_id}/stages/{row_num}/description'),
    ('PATCH', '/api/objects/{object_id}/stages/{row_num}'),
    ('DELETE', '/api/objects/{object_id}/stages/{row_num}'),
    ('PATCH', '/api/objects/{object_id}/stages/{row_num}/swap'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/complete'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/blocker'),
    ('DELETE', '/api/objects/{object_id}/stages/{row_num}/blocker'),
    ('GET', '/api/objects/{object_id}/stages/{row_num}/roadmap'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/roadmap/categories'),
    ('DELETE', '/api/objects/{object_id}/stages/{row_num}/roadmap/categories/{category_id}'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/roadmap/items'),
    ('PATCH', '/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}'),
    ('DELETE', '/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/roadmap/items/{item_id}/status'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/roadmap/notes'),
    ('GET', '/api/objects/{object_id}/stages/{row_num}/roadmap/notes'),
    ('POST', '/api/objects/{object_id}/stages/{row_num}/request'),
    ('GET', '/api/objects/{object_id}/stages/requests'),
    ('POST', '/api/objects/{object_id}/stages/requests/{request_id}/decide'),
}

BLOCKER_PHOTO_ROUTE_KEYS = {
    ('POST', '/api/objects/{object_id}/blocker-photo'),
    ('GET', '/api/objects/{object_id}/blocker-photo/{fname}'),
}


def _route_rows():
    for route in iter_app_routes(backend.app):
        for method in sorted(getattr(route, 'methods', []) or []):
            if method in {'GET', 'POST', 'PATCH', 'DELETE'}:
                yield method, getattr(route, 'path', ''), route


def test_stages_domain_routes_are_registered_once_and_owned_by_routes_module():
    rows = list(_route_rows())
    keys = [(method, path) for method, path, _ in rows]

    assert not [key for key in set(keys) if keys.count(key) > 1]
    for key in STAGE_ROUTE_KEYS:
        assert keys.count(key) == 1, key
        route = next(route for method, path, route in rows if (method, path) == key)
        assert route.endpoint.__module__.endswith('routes.stages')


def test_blocker_photo_routes_are_owned_by_object_media_module():
    rows = list(_route_rows())
    keys = [(method, path) for method, path, _ in rows]

    for key in BLOCKER_PHOTO_ROUTE_KEYS:
        assert keys.count(key) == 1, key
        route = next(route for method, path, route in rows if (method, path) == key)
        assert route.endpoint.__module__.endswith('routes.object_media')


def test_stages_router_has_no_main_import_or_own_roadmap_state():
    src = Path(backend.BACKEND_DIR, 'routes', 'stages.py').read_text()
    assert 'import main' not in src
    assert 'from main import' not in src
    assert 'ROADMAP_FILE =' not in src
    assert 'STAGE_REQUESTS_FILE =' not in src
    assert 'import roadmap_lib' not in src


def test_main_reexports_legacy_stage_handlers_and_models_from_router():
    assert backend.get_stages.__module__.endswith('routes.stages')
    assert backend.update_stage.__module__.endswith('routes.stages')
    assert backend.worker_complete_stage.__module__.endswith('routes.stages')
    assert backend.create_stage_request.__module__.endswith('routes.stages')
    assert backend.decide_stage_request_endpoint.__module__.endswith('routes.stages')
    assert backend.StageStatusBody.__module__.endswith('routes.stages')
    assert backend.StageRequestBody.__module__.endswith('routes.stages')


def test_main_remains_canonical_roadmap_store_owner():
    assert backend.rl is backend._load_repo_roadmap_lib()
    assert backend.rl.ROADMAP_FILE in backend.CRITICAL_JSON_PATHS
    assert backend.rl.STAGE_REQUESTS_FILE in backend.CRITICAL_JSON_PATHS
